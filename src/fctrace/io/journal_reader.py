"""
journal_reader.py — JBD2 journal locator and fast-commit extractor
===================================================================
Responsibilities
----------------
1. Locate the JBD2 journal file using the journal inode from the ext4
   superblock.
2. Parse the JBD2 journal superblock (big-endian, first journal block)
   to learn the total journal length and the number of fast-commit
   blocks (s_num_fc_blks, added in Linux 5.10).
3. Return the raw bytes of the fast-commit tail area for the TLV
   decoder to process.

Design notes
------------
- The JBD2 journal is stored as a regular inode (usually inode 8).
- For ext4 volumes with extent trees (virtually all modern images),
  the journal inode uses a single contiguous extent for the entire
  journal.  We read the first extent to find the starting physical
  block.
- Fast-commit blocks occupy the LAST s_num_fc_blks blocks of the
  journal.  We read them all and pass the concatenated bytes to the
  TLV decoder.
- JBD2 on-disk values are big-endian; ext4 values are little-endian.
"""

import logging
import struct
from typing import List, Tuple

from fctrace.io.image_reader import Ext4Image, ImageReadError
from fctrace.parser.fc_tags import (
    JBD2_MAGIC_NUMBER,
    JBD2_SB_OFF_MAGIC,
    JBD2_SB_OFF_BLOCKTYPE,
    JBD2_SB_OFF_BLOCKSIZE,
    JBD2_SB_OFF_MAXLEN,
    JBD2_SB_OFF_FIRST,
    JBD2_SB_OFF_NUM_FC_BLKS,
    JBD2_SB_OFF_HEAD,
    INODE_OFF_BLOCKS,
    EXT4_EXTENTS_FL,
    STRUCT_EXT4_EXTENT,
)

logger = logging.getLogger(__name__)

# JBD2 journal superblock block-type for V2 (the only type we handle)
JBD2_SUPERBLOCK_V2 = 4

# Minimum expected JBD2 superblock size
JBD2_SB_MIN_SIZE = 0x60  # We need at least 96 bytes


class JournalReadError(Exception):
    """Raised when the journal cannot be located or parsed."""


class JBD2SuperBlock:
    """Parsed subset of the JBD2 journal superblock."""

    def __init__(self, raw: bytes) -> None:
        if len(raw) < JBD2_SB_MIN_SIZE:
            raise JournalReadError(
                f"JBD2 superblock too short: {len(raw)} bytes"
            )

        # All JBD2 fields are big-endian (network byte order)
        magic     = struct.unpack_from('>I', raw, JBD2_SB_OFF_MAGIC)[0]
        blocktype = struct.unpack_from('>I', raw, JBD2_SB_OFF_BLOCKTYPE)[0]

        if magic != JBD2_MAGIC_NUMBER:
            raise JournalReadError(
                f"Invalid JBD2 magic: 0x{magic:08X} "
                f"(expected 0x{JBD2_MAGIC_NUMBER:08X})"
            )
        if blocktype != JBD2_SUPERBLOCK_V2:
            raise JournalReadError(
                f"Unexpected JBD2 block type: {blocktype} "
                f"(expected {JBD2_SUPERBLOCK_V2})"
            )

        self.block_size:  int = struct.unpack_from('>I', raw, JBD2_SB_OFF_BLOCKSIZE)[0]
        self.max_len:     int = struct.unpack_from('>I', raw, JBD2_SB_OFF_MAXLEN)[0]
        self.first:       int = struct.unpack_from('>I', raw, JBD2_SB_OFF_FIRST)[0]

        # s_num_fc_blks was added in kernel 5.10; older journals have 0 here.
        if len(raw) >= JBD2_SB_OFF_NUM_FC_BLKS + 4:
            self.num_fc_blks: int = struct.unpack_from(
                '>I', raw, JBD2_SB_OFF_NUM_FC_BLKS)[0]
        else:
            self.num_fc_blks = 0

        if len(raw) >= JBD2_SB_OFF_HEAD + 4:
            self.head: int = struct.unpack_from('>I', raw, JBD2_SB_OFF_HEAD)[0]
        else:
            self.head = 0

        logger.debug(
            "JBD2 superblock | block_size=%d | max_len=%d | "
            "first=%d | num_fc_blks=%d | head=%d",
            self.block_size, self.max_len, self.first,
            self.num_fc_blks, self.head,
        )


def _first_extent_block(inode_bytes: bytes) -> Tuple[int, int]:
    """
    Parse the first leaf extent from the inode's extent tree.

    For a freshly created journal (a single contiguous extent), this
    gives us the physical start block and the block count.

    Returns (physical_start_block, block_count).
    """
    # The 60-byte i_block region starts at INODE_OFF_BLOCKS.
    # Layout of extent header (12 bytes):
    #   0x00  uint16_le  eh_magic    (0xF30A)
    #   0x02  uint16_le  eh_entries
    #   0x04  uint16_le  eh_max
    #   0x06  uint16_le  eh_depth    (0 = leaf)
    #   0x08  uint32_le  eh_generation
    iblock = inode_bytes[INODE_OFF_BLOCKS: INODE_OFF_BLOCKS + 60]

    eh_magic   = struct.unpack_from('<H', iblock, 0)[0]
    eh_entries = struct.unpack_from('<H', iblock, 2)[0]
    eh_depth   = struct.unpack_from('<H', iblock, 6)[0]

    if eh_magic != 0xF30A:
        raise JournalReadError(
            f"Extent header magic mismatch: 0x{eh_magic:04X}"
        )
    if eh_depth != 0:
        raise JournalReadError(
            "Journal inode has a non-leaf extent tree depth "
            f"({eh_depth}). Deep trees are not yet supported by FC-Trace."
        )
    if eh_entries == 0:
        raise JournalReadError("Journal inode has zero extent entries.")

    # First leaf extent starts at byte 12 within i_block
    # struct ext4_extent { ee_block(4), ee_len(2), ee_start_hi(2), ee_start_lo(4) }
    ee_block, ee_len, ee_start_hi, ee_start_lo = STRUCT_EXT4_EXTENT.unpack_from(
        iblock, 12
    )
    physical_start = (ee_start_hi << 32) | ee_start_lo
    return physical_start, ee_len


class JournalReader:
    """
    Locates and reads the JBD2 journal of an ext4 image.

    After calling :py:meth:`open`, the following attributes are available:

    * ``jbd2_sb``   — :class:`JBD2SuperBlock` instance
    * ``jnl_start_block`` — physical block number of the first journal block
    * ``fc_blocks`` — list of physical block numbers in the fast-commit area
    """

    def __init__(self, image: Ext4Image) -> None:
        self._img = image
        self.jbd2_sb: JBD2SuperBlock | None = None
        self.jnl_start_block: int = 0
        self.fc_blocks: List[int] = []

    def open(self) -> None:
        """Locate the journal and parse its superblock."""
        ino = self._img.journal_inum
        if ino == 0:
            raise JournalReadError(
                "Superblock reports journal inode 0; "
                "this image may not have a journal."
            )

        logger.info("Reading journal inode %d …", ino)
        try:
            inode_bytes = self._img.read_inode(ino)
        except ImageReadError as exc:
            raise JournalReadError(
                f"Could not read journal inode {ino}: {exc}"
            ) from exc

        flags = self._img.inode_flags(inode_bytes)
        if not (flags & EXT4_EXTENTS_FL):
            raise JournalReadError(
                "Journal inode does not use extent tree. "
                "Legacy block-map journals are not supported."
            )

        try:
            phys_start, _blk_count = _first_extent_block(inode_bytes)
        except JournalReadError:
            raise

        self.jnl_start_block = phys_start
        logger.info("Journal physical start block: %d", phys_start)

        # Read JBD2 superblock (first block of journal)
        try:
            jnl_sb_bytes = self._img.read_block(phys_start)
        except ImageReadError as exc:
            raise JournalReadError(
                f"Cannot read JBD2 superblock block {phys_start}: {exc}"
            ) from exc

        self.jbd2_sb = JBD2SuperBlock(jnl_sb_bytes)

        # Validate journal block size matches image block size
        if self.jbd2_sb.block_size != self._img.block_size:
            logger.warning(
                "JBD2 block size (%d) != ext4 block size (%d). "
                "This is unusual; results may be unreliable.",
                self.jbd2_sb.block_size, self._img.block_size,
            )

        # Identify fast-commit block range
        if self.jbd2_sb.num_fc_blks == 0:
            logger.warning(
                "s_num_fc_blks == 0: either this kernel predates 5.10 "
                "or fast_commit was not in use. FC area may still exist; "
                "attempting heuristic scan."
            )

        self._compute_fc_blocks()

    def _compute_fc_blocks(self) -> None:
        """
        Compute the list of physical blocks belonging to the FC area.

        From the kernel: fast-commit blocks are the LAST s_num_fc_blks
        blocks of the journal circular buffer.

        Journal layout (logical):
          [block 0: JBD2 sb] [block 1..max_len-1-fc: normal JBD2] [fc blocks]

        In terms of physical blocks:
          fc_start_physical = jnl_start_block + max_len - num_fc_blks
        """
        jb = self.jbd2_sb
        if jb is None:
            raise JournalReadError("JBD2 superblock not loaded.")

        num_fc = jb.num_fc_blks
        if num_fc == 0:
            # Heuristic: scan last 256 blocks for TLV magic
            num_fc = min(256, jb.max_len // 8)
            logger.warning(
                "Using heuristic FC area size: %d blocks", num_fc
            )

        fc_logical_start = jb.max_len - num_fc
        self.fc_blocks = [
            self.jnl_start_block + fc_logical_start + i
            for i in range(num_fc)
        ]
        logger.info(
            "Fast-commit area: %d blocks starting at physical block %d",
            num_fc, self.fc_blocks[0] if self.fc_blocks else -1,
        )

    def read_fc_area(self) -> bytes:
        """
        Read and return the raw bytes of all fast-commit blocks.

        The caller (TLV decoder) is responsible for parsing the byte
        stream into individual TLV records.
        """
        if not self.fc_blocks:
            return b''

        chunks: List[bytes] = []
        for phys_blk in self.fc_blocks:
            try:
                chunks.append(self._img.read_block(phys_blk))
            except ImageReadError as exc:
                logger.warning(
                    "Could not read FC block %d: %s — skipping", phys_blk, exc
                )
        return b''.join(chunks)
