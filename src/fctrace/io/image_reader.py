"""
image_reader.py — Raw disk-image I/O for FC-Trace
==================================================
Opens a raw or loopback disk image (or block device), reads the ext4
superblock, and exposes helper methods for arbitrary block/byte I/O.

Forensic assumption: the image is opened read-only to preserve evidence
integrity.  Callers must never open the live filesystem for writing.
"""

import logging
import os
import struct
from pathlib import Path
from typing import Optional

from fctrace.parser.fc_tags import (
    EXT4_SUPER_MAGIC,
    EXT4_FEATURE_COMPAT_FAST_COMMIT,
    EXT4_EXTENTS_FL,
    SB_OFF_MAGIC,
    SB_OFF_LOG_BLOCK_SIZE,
    SB_OFF_INODES_PER_GROUP,
    SB_OFF_FEATURE_COMPAT,
    SB_OFF_FEATURE_INCOMPAT,
    SB_OFF_JOURNAL_INUM,
    SB_OFF_INODE_SIZE,
    SB_OFF_FIRST_DATA_BLOCK,
    INODE_OFF_FLAGS,
    INODE_OFF_BLOCKS,
)

logger = logging.getLogger(__name__)

# The ext4 superblock lives at byte 1024 from the start of the partition.
EXT4_SUPERBLOCK_OFFSET = 1024
EXT4_SUPERBLOCK_SIZE   = 1024   # kernel reads up to 1024 bytes

# A group descriptor is 64 bytes in ext4 (64-bit feature)
EXT4_GROUP_DESC_SIZE_64 = 64
EXT4_GROUP_DESC_SIZE_32 = 32


class ImageReadError(Exception):
    """Raised when the image cannot be read or is structurally invalid."""


class Ext4Image:
    """
    Read-only accessor for a raw ext4 disk image.

    Usage::

        with Ext4Image('/path/to/image.img') as img:
            if img.has_fast_commit:
                journal_ino = img.journal_inum
    """

    def __init__(self, image_path: str | Path) -> None:
        self._path = Path(image_path)
        self._fh: Optional[object] = None
        self._sb_bytes: bytes = b''

        # Parsed superblock fields
        self.block_size: int = 0
        self.inodes_per_group: int = 0
        self.journal_inum: int = 0
        self.inode_size: int = 128
        self.first_data_block: int = 0
        self.feature_incompat: int = 0
        self.has_fast_commit: bool = False
        self.has_extents: bool = False

    # ------------------------------------------------------------------
    # Context-manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> 'Ext4Image':
        self.open()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Open / close
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open the image file (read-only) and parse the superblock."""
        if not self._path.exists():
            raise ImageReadError(f"Image not found: {self._path}")
        try:
            self._fh = open(self._path, 'rb')
        except PermissionError as exc:
            raise ImageReadError(
                f"Cannot open {self._path} for reading (try sudo): {exc}"
            ) from exc
        self._parse_superblock()

    def close(self) -> None:
        """Close the underlying file handle."""
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    # ------------------------------------------------------------------
    # Block / byte I/O
    # ------------------------------------------------------------------

    def read_bytes(self, offset: int, length: int) -> bytes:
        """
        Read *length* bytes starting at byte *offset* from image start.

        Raises ImageReadError on short reads.
        """
        if self._fh is None:
            raise ImageReadError("Image is not open.")
        self._fh.seek(offset)
        data = self._fh.read(length)
        if len(data) < length:
            raise ImageReadError(
                f"Short read at offset 0x{offset:08X}: "
                f"expected {length}, got {len(data)} bytes"
            )
        return data

    def read_block(self, block_number: int) -> bytes:
        """Read one filesystem block by block number."""
        offset = block_number * self.block_size
        return self.read_bytes(offset, self.block_size)

    def read_block_range(self, start_block: int, count: int) -> bytes:
        """Read *count* contiguous blocks starting at *start_block*."""
        offset = start_block * self.block_size
        return self.read_bytes(offset, count * self.block_size)

    # ------------------------------------------------------------------
    # Superblock parsing
    # ------------------------------------------------------------------

    def _parse_superblock(self) -> None:
        """
        Read and validate the ext4 superblock.

        The superblock is always located at byte offset 1024 from the
        partition start, regardless of block size.
        """
        try:
            self._sb_bytes = self.read_bytes(EXT4_SUPERBLOCK_OFFSET,
                                             EXT4_SUPERBLOCK_SIZE)
        except ImageReadError as exc:
            raise ImageReadError(f"Cannot read superblock: {exc}") from exc

        # Validate magic number
        magic = struct.unpack_from('<H', self._sb_bytes, SB_OFF_MAGIC)[0]
        if magic != EXT4_SUPER_MAGIC:
            raise ImageReadError(
                f"Not an ext4 filesystem: magic 0x{magic:04X} "
                f"(expected 0x{EXT4_SUPER_MAGIC:04X})"
            )

        # Block size: 1024 << s_log_block_size
        log_bs = struct.unpack_from('<I', self._sb_bytes, SB_OFF_LOG_BLOCK_SIZE)[0]
        self.block_size = 1024 << log_bs

        self.inodes_per_group = struct.unpack_from(
            '<I', self._sb_bytes, SB_OFF_INODES_PER_GROUP)[0]

        self.feature_incompat = struct.unpack_from(
            '<I', self._sb_bytes, SB_OFF_FEATURE_INCOMPAT)[0]

        # fast_commit is EXT4_FEATURE_COMPAT_FAST_COMMIT (0x0400)
        # in s_feature_COMPAT at offset 0x5C, not in s_feature_incompat.
        # 0x4000 in s_feature_incompat = EXT4_FEATURE_INCOMPAT_LARGEDIR (>2GB htree).
        self.feature_compat = struct.unpack_from(
            '<I', self._sb_bytes, SB_OFF_FEATURE_COMPAT)[0]

        self.journal_inum = struct.unpack_from(
            '<I', self._sb_bytes, SB_OFF_JOURNAL_INUM)[0]

        # inode_size is a uint16 in the dynamic ext2/3/4 superblock area.
        self.inode_size = struct.unpack_from(
            '<H', self._sb_bytes, SB_OFF_INODE_SIZE)[0]
        if self.inode_size == 0:
            self.inode_size = 128   # fallback for very old images

        self.first_data_block = struct.unpack_from(
            '<I', self._sb_bytes, SB_OFF_FIRST_DATA_BLOCK)[0]

        self.has_fast_commit = bool(
            self.feature_compat & EXT4_FEATURE_COMPAT_FAST_COMMIT
        )

        logger.info(
            "Superblock OK | block_size=%d | journal_inum=%d | "
            "fast_commit=%s | inode_size=%d",
            self.block_size, self.journal_inum,
            self.has_fast_commit, self.inode_size,
        )

        if not self.has_fast_commit:
            logger.warning(
                "EXT4_FEATURE_COMPAT_FAST_COMMIT (0x0400) is NOT set in s_feature_compat. "
                "FC-Trace requires an ext4 volume formatted with "
                "'mkfs.ext4 -O fast_commit'."
            )

    # ------------------------------------------------------------------
    # Inode resolution
    # ------------------------------------------------------------------

    def inode_offset(self, ino: int) -> int:
        """
        Return the byte offset of inode *ino* in the image.

        ext4 uses 1-based inode numbers.  The formula is:
          group   = (ino - 1) // inodes_per_group
          local   = (ino - 1) %  inodes_per_group
          offset  = inode_table_start + local * inode_size

        We locate the inode table via the group descriptor table, which
        starts at block (first_data_block + 1) for small block sizes or
        block 1 for 4 KiB+ block sizes.
        """
        if self.block_size == 0:
            raise ImageReadError("Superblock not parsed yet.")

        ino_idx = ino - 1   # 0-based
        group = ino_idx // self.inodes_per_group
        local = ino_idx % self.inodes_per_group

        # Group descriptor table starts right after the superblock block.
        gdt_block = self.first_data_block + 1
        # Each 64-bit group descriptor is 64 bytes
        gd_off = gdt_block * self.block_size + group * EXT4_GROUP_DESC_SIZE_64

        # Group descriptor (64-bit ext4):
        #   0x00  uint32  bg_inode_table_lo
        #   0x04  uint32  bg_block_bitmap_lo
        #   0x08  uint32  bg_inode_bitmap_lo
        #   0x0C  uint32  bg_inode_table_lo  <-- actually this is the table
        # Wait — correct layout (ext4 64-bit group descriptor):
        #   0x00  uint32  bg_block_bitmap_lo
        #   0x04  uint32  bg_inode_bitmap_lo
        #   0x08  uint32  bg_inode_table_lo
        #   0x0C  uint16  bg_free_blocks_count_lo
        #   0x0E  uint16  bg_free_inodes_count_lo
        #   0x10  uint16  bg_used_dirs_count_lo
        #   0x12  uint16  bg_flags
        #   0x14  uint32  bg_exclude_bitmap_lo
        #   0x18  uint16  bg_block_bitmap_csum_lo
        #   0x1A  uint16  bg_inode_bitmap_csum_lo
        #   0x1C  uint16  bg_itable_unused_lo
        #   0x1E  uint16  bg_checksum
        #   0x20  uint32  bg_block_bitmap_hi
        #   0x24  uint32  bg_inode_bitmap_hi
        #   0x28  uint32  bg_inode_table_hi

        gd_data = self.read_bytes(gd_off, EXT4_GROUP_DESC_SIZE_64)
        inode_table_lo = struct.unpack_from('<I', gd_data, 0x08)[0]
        inode_table_hi = struct.unpack_from('<I', gd_data, 0x28)[0]
        inode_table_block = (inode_table_hi << 32) | inode_table_lo

        byte_offset = (inode_table_block * self.block_size
                       + local * self.inode_size)
        return byte_offset

    def read_inode(self, ino: int) -> bytes:
        """Return the raw inode bytes for inode *ino*."""
        off = self.inode_offset(ino)
        return self.read_bytes(off, self.inode_size)

    def inode_flags(self, inode_bytes: bytes) -> int:
        """Extract i_flags from raw inode bytes."""
        return struct.unpack_from('<I', inode_bytes, INODE_OFF_FLAGS)[0]

    def inode_uses_extents(self, inode_bytes: bytes) -> bool:
        """Return True if the inode uses the extent tree (normal for ext4)."""
        return bool(self.inode_flags(inode_bytes) & EXT4_EXTENTS_FL)

    def inode_i_block(self, inode_bytes: bytes) -> bytes:
        """Return the 60-byte i_block field (extent tree or legacy blocks)."""
        return inode_bytes[INODE_OFF_BLOCKS: INODE_OFF_BLOCKS + 60]

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"Ext4Image(path={self._path!r}, "
            f"block_size={self.block_size}, "
            f"fast_commit={self.has_fast_commit})"
        )
