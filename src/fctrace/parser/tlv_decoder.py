"""
tlv_decoder.py — ext4 Fast-Commit TLV stream decoder
=====================================================
Parses the raw byte stream from the fast-commit area into a sequence
of :class:`FCRecord` objects.  Each record corresponds to one TLV
triple on disk.

The decoder is stateful: it tracks which logical commit (transaction)
each record belongs to by watching HEAD / TAIL delimiters.

Algorithm
---------
1. Advance byte-by-byte until a non-PAD TLV header is found.
2. Decode tag + length.
3. Dispatch to the appropriate value parser.
4. Yield an FCRecord.
5. Repeat until the buffer is exhausted or a fatal error occurs.

Reference: fs/ext4/fast_commit.c  (ext4_fc_replay, ext4_fc_parse_*)
"""

import logging
from dataclasses import dataclass, field
from typing import Generator, List, Optional

from fctrace.parser.crc32c import crc32c
from fctrace.parser.fc_tags import (
    FCTag,
    TAG_TO_EVENT,
    STRUCT_FC_TL,
    STRUCT_FC_HEAD,
    STRUCT_FC_TAIL,
    STRUCT_FC_DENTRY,
    STRUCT_FC_RANGE_INO,
    STRUCT_EXT4_EXTENT,
    STRUCT_FC_DEL_RANGE,
    STRUCT_FC_INODE_INO,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes — one per TLV record type
# ---------------------------------------------------------------------------

@dataclass
class FCDentry:
    """Parsed dentry payload (CREAT, UNLINK, LINK)."""
    parent_ino: int
    ino: int
    name: str        # directory entry name (UTF-8 best-effort)


@dataclass
class FCExtent:
    """
    Parsed ADD_RANGE payload.
    ADD_RANGE: ino(4) + struct ext4_extent(12) — physical extent.
    """
    ino: int
    ee_block:    int   # logical start block
    ee_len:      int   # length in blocks
    ee_start_hi: int   # physical start (high 16 bits)
    ee_start_lo: int   # physical start (low 32 bits)

    @property
    def physical_block(self) -> int:
        return (self.ee_start_hi << 32) | self.ee_start_lo


@dataclass
class FCDelRange:
    """
    Parsed DEL_RANGE payload.
    DEL_RANGE: ino(4) + lblk_start(4) + lblk_len(4) — logical block range.
    This is struct ext4_fc_del_range, NOT struct ext4_extent.
    The kernel stores a logical range for deletion, not a physical address.
    """
    ino:        int
    lblk_start: int   # logical block start
    lblk_len:   int   # number of logical blocks


@dataclass
class FCInodeUpdate:
    """Parsed INODE record payload."""
    ino: int
    raw_inode: bytes   # raw inode bytes (variable length)


@dataclass
class FCCommitHead:
    """Parsed HEAD record payload."""
    features: int
    tid: int           # transaction ID


@dataclass
class FCCommitTail:
    """Parsed TAIL record payload."""
    tid: int
    crc: int


@dataclass
class FCRecord:
    """
    A single decoded fast-commit TLV record.

    Attributes
    ----------
    tag        : FCTag value
    event_type : Human-readable event name from TAG_TO_EVENT
    offset     : Byte offset of this TLV within the FC area buffer
    tid        : Transaction ID assigned by the HEAD seen most recently
    payload    : One of FCDentry, FCExtent, FCInodeUpdate,
                 FCCommitHead, FCCommitTail, or None for PAD
    raw_value  : The undecoded value bytes (for debugging / future use)
    decode_error : Non-None if value parsing failed (tag still recorded)
    crc_verified : TAIL records only. True if the stored CRC-32C matches the
                 value accumulated over the fast commit, False if it does not
                 (truncation, overwrite, or tampering), None if not checked.
    crc_expected : CRC stored in the TAIL record (TAIL only)
    crc_computed : CRC accumulated by the decoder (TAIL only)
    """
    tag:          FCTag
    event_type:   str
    offset:       int
    tid:          int = 0
    payload:      object = None
    raw_value:    bytes = field(default=b'', repr=False)
    decode_error: Optional[str] = None
    crc_verified: Optional[bool] = None
    crc_expected: Optional[int] = None
    crc_computed: Optional[int] = None


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------

class TLVDecoder:
    """
    Stateful decoder for an ext4 fast-commit byte stream.

    Instantiate once per FC area buffer; call :py:meth:`decode` to
    obtain a list of :class:`FCRecord` objects.
    """

    def __init__(self, data: bytes, block_size: Optional[int] = None) -> None:
        """
        Parameters
        ----------
        data
            Raw bytes of the fast-commit area.
        block_size
            Journal block size. Supply this whenever *data* came from a real
            image: the kernel's on-disk layout is block-structured and cannot
            be walked as a flat TLV stream (see :py:meth:`_iter_records`).
            ``None`` decodes the buffer linearly, which is correct only for
            densely-packed synthetic buffers such as those used in tests.
        """
        self._data = data
        self._pos: int = 0
        self._current_tid: int = 0
        self._block_size = block_size
        self._fc_crc: int = 0
        self.records: List[FCRecord] = []
        self.resync_count: int = 0
        self.crc_failures: int = 0
        self.crc_checked: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def decode(self) -> List[FCRecord]:
        """
        Decode the entire buffer and return all FCRecord objects.

        Records from PAD tags are included but can be filtered by the
        caller.  Returns an empty list if no valid HEAD is found.
        """
        self.records = list(self._iter_records())
        logger.info(
            "TLV decode complete: %d records, %d transactions",
            len(self.records),
            len({r.tid for r in self.records if r.tid > 0}),
        )
        return self.records

    # ------------------------------------------------------------------
    # Internal iteration
    # ------------------------------------------------------------------

    def _next_block_boundary(self, offset: int) -> int:
        """Byte offset of the first block strictly after the one holding *offset*."""
        bs = self._block_size
        return (offset // bs + 1) * bs

    def _iter_records(self) -> Generator[FCRecord, None, None]:
        """
        Yield FCRecord objects from the buffer, advancing _pos.

        Block structure
        ---------------
        A real fast-commit area is not a flat TLV stream. Verified against
        ``ext4_fc_replay_scan`` and ``ext4_fc_write_tail`` in
        ``fs/ext4/fast_commit.c``:

        * Each fast commit begins at a block boundary with a HEAD record.
        * The kernel replays one block at a time (``end = start + j_blocksize``).
        * TAIL is written with ``fc_len = bsize - off + sizeof(ext4_fc_tail)``,
          deliberately oversized so the per-block scan loop terminates. Only
          the leading ``sizeof(ext4_fc_tail)`` bytes are meaningful.

        Trusting the TAIL length verbatim therefore advances 12 bytes past the
        block boundary — exactly the size of the next commit's HEAD record —
        which silently swallows every HEAD after the first and mis-attributes
        every subsequent record to the wrong transaction. When *block_size* is
        known we skip to the next block boundary instead, and resynchronise
        there after an unknown or malformed tag.
        """
        data = self._data
        dlen = len(data)

        while self._pos <= dlen - STRUCT_FC_TL.size:
            offset = self._pos

            # Peek at tag
            tag_raw, val_len = STRUCT_FC_TL.unpack_from(data, self._pos)
            value_start = offset + STRUCT_FC_TL.size

            # Resolve tag before consuming the value: TAIL's declared length is
            # not the length of its payload.
            try:
                tag = FCTag(tag_raw)
            except ValueError:
                logger.debug(
                    "Unknown FC tag 0x%X at offset 0x%X", tag_raw, offset,
                )
                if not self._resync(offset, dlen):
                    break
                continue

            if tag == FCTag.TAIL:
                # Only ext4_fc_tail (tid + crc) is real payload; the rest of
                # the declared length is block padding.
                if value_start + STRUCT_FC_TAIL.size > dlen:
                    logger.debug("Truncated TAIL at offset 0x%X", offset)
                    break
                raw_value = data[value_start: value_start + STRUCT_FC_TAIL.size]
                # The kernel folds in the TL header plus fc_tid, stopping short
                # of fc_crc itself (offsetof(struct ext4_fc_tail, fc_crc)).
                self._accumulate_crc(
                    data[offset: value_start + STRUCT_FC_TAIL.size // 2]
                )
                if self._block_size:
                    self._pos = self._next_block_boundary(offset)
                else:
                    self._pos = value_start + val_len
            else:
                # Bounds check
                if value_start + val_len > dlen:
                    logger.debug(
                        "Truncated TLV at offset 0x%X: tag=0x%X val_len=%d "
                        "(only %d bytes remain)",
                        offset, tag_raw, val_len, dlen - value_start,
                    )
                    break
                raw_value = data[value_start: value_start + val_len]
                # A HEAD starts a new fast commit, so the accumulator restarts
                # there; every other tag folds in its whole TLV.
                if tag == FCTag.HEAD:
                    self._fc_crc = 0
                self._accumulate_crc(data[offset: value_start + val_len])
                self._pos = value_start + val_len

            event_type = TAG_TO_EVENT.get(tag, 'UNKNOWN')

            # PAD contributes to the CRC (folded in above) but carries no
            # forensic content.
            if tag == FCTag.PAD:
                continue

            record = FCRecord(
                tag=tag,
                event_type=event_type,
                offset=offset,
                tid=self._current_tid,
                raw_value=raw_value,
            )

            self._decode_value(tag, raw_value, record)
            if tag == FCTag.TAIL:
                self._verify_tail_crc(record)
            yield record

    # ------------------------------------------------------------------
    # Integrity
    # ------------------------------------------------------------------

    def _accumulate_crc(self, chunk: bytes) -> None:
        """Fold *chunk* into the running fast-commit CRC."""
        if self._block_size:
            self._fc_crc = crc32c(self._fc_crc, chunk)

    def _verify_tail_crc(self, record: FCRecord) -> None:
        """
        Compare the CRC stored in a TAIL against the accumulated value.

        A mismatch means the fast commit is not intact: it was truncated
        mid-write, partially overwritten by a later wrap, or deliberately
        altered. Either way the enclosing transaction should not be treated
        as trustworthy evidence.

        Only performed when the journal block size is known, because the CRC
        is computed over the real block-structured layout.
        """
        if not self._block_size or not isinstance(record.payload, FCCommitTail):
            self._fc_crc = 0
            return

        record.crc_expected = record.payload.crc
        record.crc_computed = self._fc_crc
        record.crc_verified = record.crc_expected == record.crc_computed

        self.crc_checked += 1
        if not record.crc_verified:
            self.crc_failures += 1
            logger.warning(
                "Fast-commit CRC mismatch at offset 0x%X (tid=%d): "
                "stored 0x%08X, computed 0x%08X — commit is not intact",
                record.offset, record.payload.tid,
                record.crc_expected, record.crc_computed,
            )

        # The kernel resets the accumulator after each TAIL.
        self._fc_crc = 0

    def _resync(self, offset: int, dlen: int) -> bool:
        """
        Recover from an undecodable tag.

        Stale fast-commit blocks left over from earlier wraps contain arbitrary
        bytes, so a garbage length field would desynchronise a linear walk for
        the remainder of the buffer. With a known block size we restart at the
        next block boundary, where a fast commit may legitimately begin.
        Returns False when no further block is available.
        """
        if not self._block_size:
            # No block structure to fall back on; skip the header and hope the
            # stream realigns.
            self._pos = offset + STRUCT_FC_TL.size
            return self._pos <= dlen - STRUCT_FC_TL.size

        nxt = self._next_block_boundary(offset)
        if nxt > dlen - STRUCT_FC_TL.size:
            return False
        self._pos = nxt
        self.resync_count += 1
        logger.debug("Resynchronised to block boundary at offset 0x%X", nxt)
        return True

    def _decode_value(
        self, tag: FCTag, raw: bytes, record: FCRecord
    ) -> None:
        """
        Attempt to decode the value bytes and attach a typed payload.
        Sets record.decode_error on failure (record is still yielded).
        """
        try:
            if tag == FCTag.HEAD:
                record.payload = self._decode_head(raw)
                self._current_tid = record.payload.tid
                record.tid = self._current_tid

            elif tag == FCTag.TAIL:
                record.payload = self._decode_tail(raw)

            elif tag in (FCTag.CREAT, FCTag.UNLINK, FCTag.LINK):
                record.payload = self._decode_dentry(raw)

            elif tag == FCTag.ADD_RANGE:
                record.payload = self._decode_add_range(raw)
            elif tag == FCTag.DEL_RANGE:
                record.payload = self._decode_del_range(raw)

            elif tag == FCTag.INODE:
                record.payload = self._decode_inode(raw)

        except Exception as exc:  # noqa: BLE001
            record.decode_error = str(exc)
            logger.warning(
                "Value decode failed for tag %s at offset 0x%X: %s",
                tag.name, record.offset, exc,
            )

    # ------------------------------------------------------------------
    # Per-type value decoders
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_head(raw: bytes) -> FCCommitHead:
        if len(raw) < STRUCT_FC_HEAD.size:
            raise ValueError(
                f"HEAD value too short: {len(raw)} < {STRUCT_FC_HEAD.size}"
            )
        features, tid = STRUCT_FC_HEAD.unpack_from(raw)
        return FCCommitHead(features=features, tid=tid)

    @staticmethod
    def _decode_tail(raw: bytes) -> FCCommitTail:
        if len(raw) < STRUCT_FC_TAIL.size:
            raise ValueError(
                f"TAIL value too short: {len(raw)} < {STRUCT_FC_TAIL.size}"
            )
        tid, crc = STRUCT_FC_TAIL.unpack_from(raw)
        return FCCommitTail(tid=tid, crc=crc)

    @staticmethod
    def _decode_dentry(raw: bytes) -> FCDentry:
        min_len = STRUCT_FC_DENTRY.size  # 8 bytes (parent_ino + ino)
        if len(raw) < min_len:
            raise ValueError(
                f"Dentry value too short: {len(raw)} < {min_len}"
            )
        parent_ino, ino = STRUCT_FC_DENTRY.unpack_from(raw)
        name_bytes = raw[min_len:]
        # Names are not NUL-terminated in FC records; use entire remainder.
        name = name_bytes.rstrip(b'\x00').decode('utf-8', errors='replace')
        return FCDentry(parent_ino=parent_ino, ino=ino, name=name)

    @staticmethod
    def _decode_add_range(raw: bytes) -> FCExtent:
        """ADD_RANGE: ino(4) + struct ext4_extent(12) = 16 bytes."""
        ino_size = STRUCT_FC_RANGE_INO.size          # 4
        ext_size = STRUCT_EXT4_EXTENT.size           # 12
        if len(raw) < ino_size + ext_size:
            raise ValueError(
                f"ADD_RANGE value too short: {len(raw)} < {ino_size + ext_size}"
            )
        (ino,) = STRUCT_FC_RANGE_INO.unpack_from(raw, 0)
        ee_block, ee_len, ee_start_hi, ee_start_lo = (
            STRUCT_EXT4_EXTENT.unpack_from(raw, ino_size)
        )
        return FCExtent(
            ino=ino,
            ee_block=ee_block,
            ee_len=ee_len,
            ee_start_hi=ee_start_hi,
            ee_start_lo=ee_start_lo,
        )

    @staticmethod
    def _decode_del_range(raw: bytes) -> FCDelRange:
        """
        DEL_RANGE: struct ext4_fc_del_range = {ino(4), lblk(4), len(4)} = 12 bytes.
        Verified from kernel source: fs/ext4/fast_commit.h struct ext4_fc_del_range.
        """
        if len(raw) < STRUCT_FC_DEL_RANGE.size:
            raise ValueError(
                f"DEL_RANGE value too short: {len(raw)} < {STRUCT_FC_DEL_RANGE.size}"
            )
        ino, lblk_start, lblk_len = STRUCT_FC_DEL_RANGE.unpack_from(raw)
        return FCDelRange(ino=ino, lblk_start=lblk_start, lblk_len=lblk_len)

    @staticmethod
    def _decode_inode(raw: bytes) -> FCInodeUpdate:
        if len(raw) < STRUCT_FC_INODE_INO.size:
            raise ValueError(
                f"INODE value too short: {len(raw)} < {STRUCT_FC_INODE_INO.size}"
            )
        (ino,) = STRUCT_FC_INODE_INO.unpack_from(raw)
        raw_inode = raw[STRUCT_FC_INODE_INO.size:]
        return FCInodeUpdate(ino=ino, raw_inode=raw_inode)


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------

def decode_fc_buffer(
    data: bytes, block_size: Optional[int] = None
) -> List[FCRecord]:
    """
    Decode *data* (the raw fast-commit area) and return all FCRecords.

    Pass *block_size* whenever the buffer came from a real image; see
    :py:class:`TLVDecoder`. Filters out PAD records automatically.
    """
    decoder = TLVDecoder(data, block_size=block_size)
    all_records = decoder.decode()
    return [r for r in all_records if r.tag != FCTag.PAD]
