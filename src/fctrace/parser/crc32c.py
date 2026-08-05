"""
crc32c.py — CRC-32C (Castagnoli) for fast-commit integrity verification
========================================================================
ext4 protects each fast commit with a CRC stored in the TAIL record.
Verifying it lets FC-Trace distinguish an intact commit from one that has
been truncated, partially overwritten, or deliberately altered.

Why not :func:`zlib.crc32`
--------------------------
``zlib.crc32`` implements CRC-32/IEEE (polynomial 0x04C11DB7). ext4 uses
CRC-32C (Castagnoli, polynomial 0x1EDC6F41, reflected 0x82F63B78) via
``ext4_chksum()``. They are different functions and are not interchangeable.

Kernel semantics
----------------
``crc32c(crc, address, length)`` in Linux is a *raw* reflected CRC: the
supplied ``crc`` is the initial register value and there is no final
inversion. Callers that want the conventional CRC-32C check value pass
``~0`` and invert the result themselves. ``ext4_fc_replay_scan`` seeds with
literal ``0`` and compares the accumulated value directly against
``fc_crc``, so :func:`crc32c` here reproduces the raw form.

Reference: ``fs/ext4/fast_commit.c`` (``ext4_fc_replay_scan``,
``ext4_fc_write_tail``); ``lib/crc32c.c``.
"""


# Reflected CRC-32C polynomial.
_POLY_REFLECTED = 0x82F63B78

# Conventional CRC-32C check value for the ASCII string "123456789",
# computed with init=0xFFFFFFFF and a final inversion. Used by
# :func:`self_test` to prove the table is correct.
CRC32C_CHECK_VECTOR = 0xE3069283


def _build_table() -> tuple:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ (_POLY_REFLECTED & -(crc & 1))
        table.append(crc & 0xFFFFFFFF)
    return tuple(table)


_TABLE = _build_table()


def crc32c(crc: int, data: bytes) -> int:
    """
    Raw CRC-32C with *crc* as the initial register value.

    Matches the kernel's ``crc32c(crc, address, length)``: no initial or
    final inversion is applied, so results chain across successive calls the
    way ``ext4_fc_replay_scan`` accumulates them.
    """
    crc &= 0xFFFFFFFF
    for byte in data:
        crc = (crc >> 8) ^ _TABLE[(crc ^ byte) & 0xFF]
    return crc & 0xFFFFFFFF


def crc32c_standard(data: bytes) -> int:
    """
    Conventional CRC-32C: init 0xFFFFFFFF, final inversion.

    Provided so the implementation can be checked against the published
    test vector; ext4 fast commit does not use this form.
    """
    return crc32c(0xFFFFFFFF, data) ^ 0xFFFFFFFF


def self_test() -> bool:
    """Return True if this implementation reproduces the CRC-32C check vector."""
    return crc32c_standard(b"123456789") == CRC32C_CHECK_VECTOR
