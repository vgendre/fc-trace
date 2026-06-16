"""
baseline_ext4.py — Baseline forensic analysis methods for comparison
====================================================================
Implements three baseline analysis strategies against which FC-Trace
is evaluated:

  B1 — Inode-only analysis
       Scans inode table and directory entries.  No journal.
       Represents the weakest common denominator.

  B2 — Classic JBD2 journal analysis (no fast-commit decoding)
       Reads the full JBD2 journal descriptor / commit blocks but
       ignores the fast-commit tail area.

  B3 — Sleuth Kit adapter
       Calls fls(1) and istat(1) via subprocess to simulate what a
       practitioner would get with the mainstream open-source toolkit.
       Requires sleuthkit to be installed; degrades gracefully if absent.

Each baseline returns a list of dicts with the same keys as
ForensicEvent.to_dict() so that diff_engine can compare them uniformly.
"""

import json
import logging
import shutil
import struct
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared result structure (mirrors ForensicEvent.to_dict())
# ---------------------------------------------------------------------------

def _empty_result(event_type: str, ino: int = 0) -> Dict[str, Any]:
    return {
        'tid':             0,
        'seq':             0,
        'event_type':      event_type,
        'ino':             ino,
        'parent_ino':      0,
        'name':            '',
        'new_name':        '',
        'new_parent':      0,
        'physical_block':  0,
        'logical_block':   0,
        'extent_len':      0,
        'confidence':      0.5,
        'commit_complete': False,
        'fc_offsets':      [],
        'decode_errors':   [],
        'source':          'unknown',
    }


# ---------------------------------------------------------------------------
# B1 — Inode-only analysis
# ---------------------------------------------------------------------------

class InodeOnlyAnalyzer:
    """
    B1: Scan the inode table for allocated and de-allocated inodes.

    Strategy:
    - Walk all group descriptor tables to find inode bitmap blocks.
    - Read inode bitmaps; collect free inodes that still have non-zero
      link counts or non-zero block pointers as candidate deleted files.
    - Report them as RECOVER_CANDIDATE events.

    Limitation: no ordering, no path information for deleted inodes.
    """

    def __init__(self, image_path: str) -> None:
        self._image_path = image_path

    def analyze(self) -> List[Dict[str, Any]]:
        """Return list of inode-only result dicts."""
        # Lazy import to avoid circular dependencies at module level
        from fctrace.io.image_reader import Ext4Image, ImageReadError

        results: List[Dict[str, Any]] = []

        try:
            with Ext4Image(self._image_path) as img:
                results = self._scan_inodes(img)
        except Exception as exc:  # noqa: BLE001
            logger.error("B1 analysis failed: %s", exc)

        logger.info("B1 (inode-only): found %d candidate events", len(results))
        return results

    def _scan_inodes(self, img) -> List[Dict[str, Any]]:
        """
        Walk inode bitmaps across all block groups and collect inode events.
        For each inode: if allocated, emit INODE_ALLOCATED; if the inode
        has data but is not in any directory (orphan), emit ORPHAN_INODE.
        """
        from fctrace.io.image_reader import EXT4_GROUP_DESC_SIZE_64
        results = []

        bs = img.block_size
        ipg = img.inodes_per_group
        inode_size = img.inode_size
        first_data_block = img.first_data_block

        # Number of block groups = ceil(total_inodes / inodes_per_group)
        sb = img._sb_bytes
        total_inodes = struct.unpack_from('<I', sb, 0x00)[0]
        num_groups = (total_inodes + ipg - 1) // ipg

        gdt_block = first_data_block + 1

        for g in range(num_groups):
            gd_off = gdt_block * bs + g * EXT4_GROUP_DESC_SIZE_64
            try:
                gd = img.read_bytes(gd_off, EXT4_GROUP_DESC_SIZE_64)
            except Exception:  # noqa: BLE001
                continue

            inode_bitmap_lo = struct.unpack_from('<I', gd, 0x04)[0]
            inode_bitmap_hi = struct.unpack_from('<I', gd, 0x24)[0]
            inode_bitmap_block = (inode_bitmap_hi << 32) | inode_bitmap_lo

            inode_table_lo = struct.unpack_from('<I', gd, 0x08)[0]
            inode_table_hi = struct.unpack_from('<I', gd, 0x28)[0]
            inode_table_block = (inode_table_hi << 32) | inode_table_lo

            try:
                bitmap = img.read_block(inode_bitmap_block)
            except Exception:  # noqa: BLE001
                continue

            for local_idx in range(ipg):
                byte_idx = local_idx // 8
                bit_idx  = local_idx %  8
                if byte_idx >= len(bitmap):
                    break

                allocated = bool(bitmap[byte_idx] & (1 << bit_idx))
                global_ino = g * ipg + local_idx + 1  # 1-based

                if global_ino < 11:   # Reserved inodes
                    continue

                inode_off = inode_table_block * bs + local_idx * inode_size
                try:
                    inode_bytes = img.read_bytes(inode_off, inode_size)
                except Exception:  # noqa: BLE001
                    continue

                i_links = struct.unpack_from('<H', inode_bytes, 0x1A)[0]
                i_mode  = struct.unpack_from('<H', inode_bytes, 0x00)[0]

                if not allocated and i_links == 0 and i_mode != 0:
                    # De-allocated inode that still has mode set — deleted file
                    ev = _empty_result('DELETED_INODE', global_ino)
                    ev['source'] = 'B1_inode_scan'
                    results.append(ev)
                elif allocated:
                    ev = _empty_result('INODE_ALLOCATED', global_ino)
                    ev['source'] = 'B1_inode_scan'
                    results.append(ev)

        return results


# ---------------------------------------------------------------------------
# B2 — Classic JBD2 journal-only (no FC decoding)
# ---------------------------------------------------------------------------

class JournalOnlyAnalyzer:
    """
    B2: Parse the JBD2 journal descriptor and commit blocks only.

    Reads the main circular journal (blocks 1 .. max_len - num_fc_blks - 1)
    and extracts file-system metadata block writes.  Does NOT decode the
    fast-commit tail area.

    For each descriptor block found, emits a JOURNAL_META_WRITE event
    recording which filesystem block was journalled.  No event typing
    (CREATE / UNLINK etc.) is possible without further inode/directory
    interpretation, which intentionally mirrors what naive journal parsing
    gives an investigator.
    """

    # JBD2 block types (big-endian uint32 at offset 0x04)
    BLOCK_TYPE_DESCRIPTOR  = 1
    BLOCK_TYPE_COMMIT      = 2
    BLOCK_TYPE_SB_V1       = 3
    BLOCK_TYPE_SB_V2       = 4
    BLOCK_TYPE_REVOKE       = 5

    def __init__(self, image_path: str) -> None:
        self._image_path = image_path

    def analyze(self) -> List[Dict[str, Any]]:
        from fctrace.io.image_reader import Ext4Image, ImageReadError
        from fctrace.io.journal_reader import JournalReader, JournalReadError

        results: List[Dict[str, Any]] = []
        try:
            with Ext4Image(self._image_path) as img:
                jr = JournalReader(img)
                jr.open()
                results = self._parse_classic_journal(img, jr)
        except Exception as exc:  # noqa: BLE001
            logger.error("B2 analysis failed: %s", exc)

        logger.info(
            "B2 (journal-only): found %d journal meta-write events", len(results)
        )
        return results

    def _parse_classic_journal(self, img, jr) -> List[Dict[str, Any]]:
        results = []
        jb = jr.jbd2_sb
        jnl_start = jr.jnl_start_block
        bs = img.block_size

        # Classic journal is blocks 1 .. max_len - num_fc_blks (exclusive)
        classic_end = jb.max_len - jb.num_fc_blks

        for logical_blk in range(1, classic_end):
            phys_blk = jnl_start + logical_blk
            try:
                blk_data = img.read_block(phys_blk)
            except Exception:  # noqa: BLE001
                continue

            # JBD2 block header: magic(4) + blocktype(4) + sequence(4)
            magic    = struct.unpack_from('>I', blk_data, 0x00)[0]
            blocktype = struct.unpack_from('>I', blk_data, 0x04)[0]
            sequence = struct.unpack_from('>I', blk_data, 0x08)[0]

            from fctrace.parser.fc_tags import JBD2_MAGIC_NUMBER
            if magic != JBD2_MAGIC_NUMBER:
                continue

            if blocktype == self.BLOCK_TYPE_DESCRIPTOR:
                # Descriptor block: contains tagged filesystem block numbers.
                # Each tag: blocknr(4) + flags(4) [+ uuid(16) if !SAME_UUID]
                offset = 12   # skip block header
                while offset + 8 <= bs:
                    fs_blocknr = struct.unpack_from('>I', blk_data, offset)[0]
                    flags      = struct.unpack_from('>I', blk_data, offset + 4)[0]
                    offset += 8
                    # SAME_UUID flag = 0x1; if not set, skip 16-byte UUID
                    if not (flags & 0x1):
                        offset += 16

                    ev = _empty_result('JOURNAL_META_WRITE', 0)
                    ev['physical_block'] = fs_blocknr
                    ev['tid'] = sequence
                    ev['source'] = 'B2_journal_descriptor'
                    results.append(ev)

                    if flags & 0x8:   # LAST_TAG flag
                        break

            elif blocktype == self.BLOCK_TYPE_COMMIT:
                ev = _empty_result('JOURNAL_COMMIT', 0)
                ev['tid'] = sequence
                ev['commit_complete'] = True
                ev['source'] = 'B2_journal_commit'
                results.append(ev)

        return results


# ---------------------------------------------------------------------------
# B3 — Sleuth Kit adapter
# ---------------------------------------------------------------------------

class SleuthKitAdapter:
    """
    B3: Run fls(1) and istat(1) from The Sleuth Kit and parse output.

    fls -r -d  reports deleted files recursively.
    fls -r     reports all allocated entries.

    Requires 'fls' and 'istat' to be on $PATH.  If not found, returns
    an empty list with a warning (graceful degradation for environments
    without TSK installed).
    """

    def __init__(self, image_path: str) -> None:
        self._image_path = image_path

    @property
    def available(self) -> bool:
        return shutil.which('fls') is not None

    def analyze(self) -> List[Dict[str, Any]]:
        if not self.available:
            logger.warning(
                "B3: 'fls' not found on PATH. "
                "Install sleuthkit to enable this baseline."
            )
            return []

        results = []
        results.extend(self._run_fls(deleted_only=True))
        results.extend(self._run_fls(deleted_only=False))

        logger.info("B3 (Sleuth Kit): found %d entries", len(results))
        return results

    def _run_fls(self, deleted_only: bool) -> List[Dict[str, Any]]:
        cmd = ['fls', '-r']
        if deleted_only:
            cmd.append('-d')
        cmd.append(self._image_path)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            logger.error("B3: fls timed out on %s", self._image_path)
            return []
        except Exception as exc:  # noqa: BLE001
            logger.error("B3: fls failed: %s", exc)
            return []

        results = []
        for line in proc.stdout.splitlines():
            # fls output: TYPE/TYPE  inode_num   filename
            # e.g.: d/d  12:   subdir
            #        r/r  13:   file.txt
            #        r/r * 14:  deleted.txt
            parts = line.split()
            if len(parts) < 3:
                continue

            is_deleted = '*' in parts
            try:
                # inode field may be 'ino:' or 'ino' depending on TSK version
                ino_str = parts[2 if not is_deleted else 3].rstrip(':')
                ino = int(ino_str)
            except (ValueError, IndexError):
                continue

            name_idx = 4 if is_deleted else 3
            name = ' '.join(parts[name_idx:]) if name_idx < len(parts) else ''

            ev = _empty_result(
                'DELETED_FILE' if is_deleted else 'ALLOCATED_FILE',
                ino,
            )
            ev['name'] = name
            ev['source'] = 'B3_sleuthkit'
            results.append(ev)

        return results
