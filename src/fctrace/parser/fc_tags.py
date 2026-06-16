"""
fc_tags.py — ext4 Fast-Commit TLV tag definitions
===================================================
All constants are sourced from the Linux kernel header:
  fs/ext4/fast_commit.h (kernel >= 5.10)

Each fast-commit record on disk is a TLV triple:
  [tag: uint16_le] [length: uint16_le] [value: length bytes]

The fast-commit area resides at the tail of the JBD2 journal.
Records are separated by EXT4_FC_TAG_PAD entries and each
logical commit is delimited by EXT4_FC_TAG_HEAD / EXT4_FC_TAG_TAIL.
"""

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional


# ---------------------------------------------------------------------------
# Tag values (little-endian uint16 on disk)
# ---------------------------------------------------------------------------

class FCTag(IntEnum):
    """Enumeration of all known ext4 fast-commit TLV tag values."""
    ADD_RANGE    = 0x0001   # Extent added to an inode
    DEL_RANGE    = 0x0002   # Extent deleted from an inode
    CREAT        = 0x0003   # Directory entry created
    LINK         = 0x0004   # Hard link created
    UNLINK       = 0x0005   # Directory entry removed
    INODE        = 0x0006   # Raw inode update
    PAD          = 0x0007   # Padding / alignment bytes
    TAIL         = 0x0008   # End of one fast-commit
    HEAD         = 0x0009   # Start of one fast-commit


# ---------------------------------------------------------------------------
# EXT4 fast-commit feature flags — DUAL FLAG SYSTEM (verified from kernel source)
# ---------------------------------------------------------------------------
#
# Fast-commit uses TWO separate flags in TWO different locations:
#
# 1. EXT4_FEATURE_COMPAT_FAST_COMMIT = 0x0400
#    Location: s_feature_compat field of the ext4 superblock (offset 0x5C, LE-u32)
#    Meaning : "This volume may use fast commits when mounted."
#    Set by  : mkfs.ext4 -O fast_commit
#    COMPAT because: on a clean unmount the JBD2 flag is cleared, making the FS
#    mountable by older kernels even with this COMPAT flag set.
#
# 2. JBD2_FEATURE_INCOMPAT_FAST_COMMIT = 0x00000020
#    Location: s_feature_incompat field of the JBD2 journal superblock (offset 0x28 BE)
#    Meaning : "There are fast-commit blocks currently in the journal."
#    Set by  : Kernel dynamically when fast-commit blocks are written.
#    INCOMPAT because: an old kernel cannot safely replay a journal with FC blocks.
#
# Common mistake: 0x4000 in s_feature_incompat is EXT4_FEATURE_INCOMPAT_LARGEDIR
# (>2 GB or 3-level htree), NOT fast_commit. Do NOT confuse them.
#
# References:
#   fs/ext4/ext4.h: #define EXT4_FEATURE_COMPAT_FAST_COMMIT  0x0400
#   include/linux/jbd2.h: #define JBD2_FEATURE_INCOMPAT_FAST_COMMIT 0x00000020
#   Documentation/filesystems/ext4/journal.rst (kernel docs)

EXT4_FEATURE_COMPAT_FAST_COMMIT   = 0x0400   # in s_feature_COMPAT  (SB offset 0x5C)
JBD2_FEATURE_INCOMPAT_FAST_COMMIT = 0x00000020  # in JBD2 journal SB incompat field

# Ext4 superblock magic
EXT4_SUPER_MAGIC = 0xEF53

# JBD2 journal magic
JBD2_MAGIC_NUMBER = 0xC03B3998


# ---------------------------------------------------------------------------
# Struct layouts (all little-endian unless marked BE)
# ---------------------------------------------------------------------------

# TLV header: tag (2) + length of value (2)
STRUCT_FC_TL = struct.Struct('<HH')   # (tag, val_len)

# HEAD value: features (4) + transaction-id (4)
STRUCT_FC_HEAD = struct.Struct('<II')  # (fc_features, fc_tid)

# TAIL value: transaction-id (4) + crc32c (4)
STRUCT_FC_TAIL = struct.Struct('<II')  # (fc_tid, fc_crc)

# Dentry-based records (CREAT, LINK, UNLINK):
#   parent_ino (4) + ino (4) + name[fc_len - 8]
STRUCT_FC_DENTRY = struct.Struct('<II')  # (parent_ino, ino)

# ADD_RANGE / DEL_RANGE inode prefix
STRUCT_FC_RANGE_INO = struct.Struct('<I')  # (ino,)

# ext4_extent: ee_block (4) + ee_len (2) + ee_start_hi (2) + ee_start_lo (4)
# Used by ADD_RANGE only.
STRUCT_EXT4_EXTENT = struct.Struct('<IHHI')  # (block, len, start_hi, start_lo)

# ext4_fc_del_range value, verified from kernel struct ext4_fc_del_range:
#   fc_ino  (4) + fc_lblk (4) + fc_len (4)  = 12 bytes total
# DEL_RANGE records a logical block range deletion, NOT a physical extent.
# It does NOT use struct ext4_extent.
STRUCT_FC_DEL_RANGE = struct.Struct('<III')  # (ino, lblk_start, lblk_len)

# INODE value: ino (4) + raw ext4 inode bytes (variable)
STRUCT_FC_INODE_INO = struct.Struct('<I')  # (ino,)

# Ext4 superblock (partial — only fields FC-Trace needs)
STRUCT_EXT4_SB_PARTIAL = struct.Struct('<'
    'I'   # 0x00  s_inodes_count
    'I'   # 0x04  s_blocks_count_lo
    'I'   # 0x08  s_r_blocks_count_lo
    'I'   # 0x0C  s_free_blocks_count_lo
    'I'   # 0x10  s_free_inodes_count
    'I'   # 0x14  s_first_data_block
    'I'   # 0x18  s_log_block_size
    'I'   # 0x1C  s_log_cluster_size
    'I'   # 0x20  s_blocks_per_group
    'I'   # 0x24  s_clusters_per_group
    'I'   # 0x28  s_inodes_per_group
    'I'   # 0x2C  s_mtime
    'I'   # 0x30  s_wtime
    'H'   # 0x34  s_mnt_count
    'H'   # 0x36  s_max_mnt_count
    'H'   # 0x38  s_magic
    # --- skip to 0x60 manually ---
)
# We read select fields individually; using fixed offsets is more robust
# given that the superblock has many variable-length regions after magic.

# Superblock field offsets (byte offset from start of superblock)
SB_OFF_MAGIC            = 0x38   # uint16 — must equal EXT4_SUPER_MAGIC
SB_OFF_LOG_BLOCK_SIZE   = 0x18   # uint32
SB_OFF_INODES_PER_GROUP = 0x28   # uint32
SB_OFF_FEATURE_COMPAT   = 0x5C   # uint32 — EXT4_FEATURE_COMPAT_FAST_COMMIT is here
SB_OFF_FEATURE_INCOMPAT = 0x60   # uint32 — NOT where fast_commit lives (0x4000=LARGEDIR)
SB_OFF_JOURNAL_INUM     = 0xE0   # uint32
SB_OFF_INODE_SIZE       = 0x58   # uint16
SB_OFF_FIRST_DATA_BLOCK = 0x14   # uint32

# JBD2 journal superblock offsets (big-endian)
JBD2_SB_OFF_MAGIC       = 0x00   # uint32 BE
JBD2_SB_OFF_BLOCKTYPE   = 0x04   # uint32 BE
JBD2_SB_OFF_BLOCKSIZE   = 0x0C   # uint32 BE
JBD2_SB_OFF_MAXLEN      = 0x10   # uint32 BE
JBD2_SB_OFF_FIRST       = 0x14   # uint32 BE  (first valid log block)
JBD2_SB_OFF_NUM_FC_BLKS = 0x54   # uint32 BE  (number of FC blocks, kernel >= 5.10)
JBD2_SB_OFF_HEAD        = 0x58   # uint32 BE  (log head block)

# Ext4 inode offsets (byte offset from start of the inode)
INODE_OFF_SIZE_LO   = 0x04   # uint32
INODE_OFF_FLAGS     = 0x20   # uint32
INODE_OFF_BLOCKS    = 0x28   # 60 bytes (block pointers or extent tree)
INODE_OFF_SIZE_HI   = 0x6C   # uint32

EXT4_EXTENTS_FL = 0x00080000   # inode uses extent tree, not legacy block map


# ---------------------------------------------------------------------------
# Human-readable forensic event type mapping
# ---------------------------------------------------------------------------

TAG_TO_EVENT: dict = {
    FCTag.CREAT:       'CREATE',
    FCTag.UNLINK:      'UNLINK',
    FCTag.LINK:        'LINK',
    FCTag.ADD_RANGE:   'EXTENT_ADD',
    FCTag.DEL_RANGE:   'EXTENT_DEL',
    FCTag.INODE:       'INODE_UPDATE',
    FCTag.HEAD:        'COMMIT_BEGIN',
    FCTag.TAIL:        'COMMIT_END',
    FCTag.PAD:         'PAD',
}
