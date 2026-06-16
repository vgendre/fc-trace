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
import struct
from dataclasses import dataclass, field
from typing import Generator, List, Optional

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
    """
    tag:          FCTag
    event_type:   str
    offset:       int
    tid:          int = 0
    payload:      object = None
    raw_value:    bytes = field(default=b'', repr=False)
    decode_error: Optional[str] = None


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------

class TLVDecoder:
    """
    Stateful decoder for an ext4 fast-commit byte stream.

    Instantiate once per FC area buffer; call :py:meth:`decode` to
    obtain a list of :class:`FCRecord` objects.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos: int = 0
        self._current_tid: int = 0
        self.records: List[FCRecord] = []

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

    def _iter_records(self) -> Generator[FCRecord, None, None]:
        """Yield FCRecord objects from the buffer, advancing _pos."""
        data = self._data
        dlen = len(data)

        while self._pos <= dlen - STRUCT_FC_TL.size:
            offset = self._pos

            # Peek at tag
            tag_raw, val_len = STRUCT_FC_TL.unpack_from(data, self._pos)
            self._pos += STRUCT_FC_TL.size

            # Bounds check
            if self._pos + val_len > dlen:
                logger.debug(
                    "Truncated TLV at offset 0x%X: tag=0x%X val_len=%d "
                    "(only %d bytes remain)",
                    offset, tag_raw, val_len, dlen - self._pos,
                )
                break

            raw_value = data[self._pos: self._pos + val_len]
            self._pos += val_len

            # Resolve tag
            try:
                tag = FCTag(tag_raw)
            except ValueError:
                logger.debug(
                    "Unknown FC tag 0x%X at offset 0x%X — skipping",
                    tag_raw, offset,
                )
                continue

            event_type = TAG_TO_EVENT.get(tag, 'UNKNOWN')

            # Skip pure padding
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
            yield record

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

def decode_fc_buffer(data: bytes) -> List[FCRecord]:
    """
    Decode *data* (the raw fast-commit area) and return all FCRecords.

    Filters out PAD records automatically.
    """
    decoder = TLVDecoder(data)
    all_records = decoder.decode()
    return [r for r in all_records if r.tag != FCTag.PAD]
