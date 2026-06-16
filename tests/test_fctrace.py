"""
tests/test_fctrace.py — FC-Trace unit and integration test suite
================================================================
Run with:  pytest tests/test_fctrace.py -v
Requires:  Python 3.10+, no external dependencies

Coverage targets
----------------
- TLV decoder: PAD filter, HEAD/TAIL parsing, dentry, extent, inode records
- EventBuilder: CREATE, UNLINK, inferred RENAME pairing, confidence
- DiffEngine: TP/FP/FN counts, recall, precision, F1, ordering, path rate
- JSONReporter / CSVReporter: round-trip correctness
- Image / Journal reader: error handling on bad magic, short reads
"""

import json
import os
import struct
import sys
import tempfile

import pytest

# Allow running from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from fctrace.parser.fc_tags import (
    FCTag,
    STRUCT_FC_TL, STRUCT_FC_HEAD, STRUCT_FC_TAIL,
    STRUCT_FC_DENTRY, STRUCT_FC_RANGE_INO, STRUCT_EXT4_EXTENT,
    STRUCT_FC_DEL_RANGE, STRUCT_FC_INODE_INO,
    EXT4_SUPER_MAGIC, EXT4_FEATURE_COMPAT_FAST_COMMIT,
    SB_OFF_JOURNAL_INUM, SB_OFF_INODE_SIZE,
    SB_OFF_FEATURE_COMPAT, SB_OFF_MAGIC,
    TAG_TO_EVENT,
)
from fctrace.parser.tlv_decoder import TLVDecoder, decode_fc_buffer
from fctrace.reconstruct.event_builder import (
    EventBuilder, CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW,
)
from fctrace.compare.diff_engine import DiffEngine, _safe_div, _harmonic
from fctrace.output.reporters import JSONReporter, CSVReporter, TextReporter


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def pack_tlv(tag: FCTag, value: bytes) -> bytes:
    return STRUCT_FC_TL.pack(int(tag), len(value)) + value


def make_head(tid: int) -> bytes:
    return pack_tlv(FCTag.HEAD, STRUCT_FC_HEAD.pack(0, tid))


def make_tail(tid: int, crc: int = 0) -> bytes:
    return pack_tlv(FCTag.TAIL, STRUCT_FC_TAIL.pack(tid, crc))


def make_creat(parent: int, ino: int, name: str) -> bytes:
    return pack_tlv(FCTag.CREAT, STRUCT_FC_DENTRY.pack(parent, ino) + name.encode())


def make_unlink(parent: int, ino: int, name: str) -> bytes:
    return pack_tlv(FCTag.UNLINK, STRUCT_FC_DENTRY.pack(parent, ino) + name.encode())


def make_rename(parent_s: int, ino: int, src: str,
                parent_d: int, dst: str) -> bytes:
    link = pack_tlv(FCTag.LINK, STRUCT_FC_DENTRY.pack(parent_d, ino) + dst.encode())
    unlink = pack_tlv(FCTag.UNLINK, STRUCT_FC_DENTRY.pack(parent_s, ino) + src.encode())
    return link + unlink


def make_extent(ino: int, ee_block=0, ee_len=4, hi=0, lo=100) -> bytes:
    val = STRUCT_FC_RANGE_INO.pack(ino) + STRUCT_EXT4_EXTENT.pack(ee_block, ee_len, hi, lo)
    return pack_tlv(FCTag.ADD_RANGE, val)


def make_del_range(ino: int, lblk_start=0, lblk_len=8) -> bytes:
    return pack_tlv(FCTag.DEL_RANGE, STRUCT_FC_DEL_RANGE.pack(ino, lblk_start, lblk_len))


def simple_buf(tid=1, ops: list = None) -> bytes:
    """Build a complete HEAD…TAIL buffer with arbitrary ops in between."""
    ops = ops or []
    buf = make_head(tid)
    for op in ops:
        buf += op
    buf += make_tail(tid)
    return buf


def gt_event(etype, ino=0, parent=2, name='', new_name='', new_parent=0):
    return {'event_type': etype, 'ino': ino, 'parent_ino': parent,
            'name': name, 'new_name': new_name, 'new_parent': new_parent}


# ─────────────────────────────────────────────────────────────
# 1. FC Tag constants
# ─────────────────────────────────────────────────────────────

class TestFCTags:

    def test_all_tags_in_mapping(self):
        for tag in FCTag:
            assert tag in TAG_TO_EVENT, f"Missing: {tag}"

    def test_struct_sizes(self):
        assert STRUCT_FC_TL.size        == 4
        assert STRUCT_FC_HEAD.size      == 8
        assert STRUCT_FC_TAIL.size      == 8
        assert STRUCT_FC_DENTRY.size    == 8
        assert STRUCT_EXT4_EXTENT.size  == 12
        assert STRUCT_FC_RANGE_INO.size == 4
        assert STRUCT_FC_DEL_RANGE.size == 12   # ino(4)+lblk(4)+len(4)
        assert STRUCT_FC_INODE_INO.size == 4    # ino prefix only

    def test_feature_flag_value(self):
        assert EXT4_FEATURE_COMPAT_FAST_COMMIT == 0x0400  # BUG-1 FIX verified

    def test_super_magic_value(self):
        assert EXT4_SUPER_MAGIC == 0xEF53

    def test_fast_commit_tag_values_match_kernel(self):
        assert FCTag.ADD_RANGE == 0x0001
        assert FCTag.DEL_RANGE == 0x0002
        assert FCTag.CREAT == 0x0003
        assert FCTag.LINK == 0x0004
        assert FCTag.UNLINK == 0x0005
        assert FCTag.INODE == 0x0006
        assert FCTag.PAD == 0x0007
        assert FCTag.TAIL == 0x0008
        assert FCTag.HEAD == 0x0009

    def test_superblock_offsets(self):
        assert SB_OFF_MAGIC           == 0x38   # s_magic: uint16
        assert SB_OFF_FEATURE_COMPAT  == 0x5C   # EXT4_FEATURE_COMPAT_FAST_COMMIT lives here
        assert SB_OFF_INODE_SIZE      == 0x58   # s_inode_size: uint16
        assert SB_OFF_JOURNAL_INUM    == 0xE0   # s_journal_inum: uint32


# ─────────────────────────────────────────────────────────────
# 2. TLV Decoder
# ─────────────────────────────────────────────────────────────

class TestTLVDecoder:

    def test_empty_buffer(self):
        assert decode_fc_buffer(b'') == []

    def test_pad_only_filtered(self):
        buf = pack_tlv(FCTag.PAD, b'\x00' * 8)
        assert decode_fc_buffer(buf) == []

    def test_single_head(self):
        buf = make_head(42)
        recs = decode_fc_buffer(buf)
        assert len(recs) == 1
        assert recs[0].tag == FCTag.HEAD
        assert recs[0].payload.tid == 42
        assert recs[0].tid == 42

    def test_head_tail_pair(self):
        buf = make_head(5) + make_tail(5, 0xDEAD)
        recs = decode_fc_buffer(buf)
        assert len(recs) == 2
        assert recs[0].tag == FCTag.HEAD
        assert recs[1].tag == FCTag.TAIL
        assert recs[1].payload.crc == 0xDEAD

    def test_creat_record(self):
        buf = simple_buf(tid=1, ops=[make_creat(2, 15, 'hello.txt')])
        recs = decode_fc_buffer(buf)
        creat = next(r for r in recs if r.tag == FCTag.CREAT)
        assert creat.payload.ino == 15
        assert creat.payload.parent_ino == 2
        assert creat.payload.name == 'hello.txt'
        assert creat.tid == 1

    def test_unlink_record(self):
        buf = simple_buf(tid=2, ops=[make_unlink(2, 20, 'bye.txt')])
        recs = decode_fc_buffer(buf)
        ul = next(r for r in recs if r.tag == FCTag.UNLINK)
        assert ul.payload.name == 'bye.txt'
        assert ul.payload.ino  == 20

    def test_rename_pair(self):
        buf = simple_buf(tid=3, ops=[
            make_rename(2, 11, 'old.py', 2, 'new.py')
        ])
        recs = decode_fc_buffer(buf)
        tags = [r.tag for r in recs]
        assert FCTag.LINK in tags
        assert FCTag.UNLINK in tags

    def test_extent_add(self):
        buf = simple_buf(tid=4, ops=[make_extent(ino=30)])
        recs = decode_fc_buffer(buf)
        ext = next(r for r in recs if r.tag == FCTag.ADD_RANGE)
        assert ext.payload.ino == 30
        assert ext.payload.physical_block == 100   # lo=100, hi=0

    def test_extent_del(self):
        # DEL_RANGE uses struct ext4_fc_del_range {ino, lblk, len}, NOT ext4_extent
        buf = simple_buf(tid=5, ops=[make_del_range(ino=40, lblk_start=10, lblk_len=8)])
        recs = decode_fc_buffer(buf)
        dr = next(r for r in recs if r.tag == FCTag.DEL_RANGE)
        assert dr.payload.ino        == 40
        assert dr.payload.lblk_start == 10
        assert dr.payload.lblk_len   == 8
        assert dr.decode_error is None

    def test_truncated_buffer_no_crash(self):
        # A buffer that cuts off mid-value — should decode what it can
        buf = make_head(9)[:3]   # incomplete TL header
        result = decode_fc_buffer(buf)
        assert isinstance(result, list)   # must not raise

    def test_unknown_tag_skipped(self):
        # Tag 0xFFFF is not a valid FCTag
        bad = STRUCT_FC_TL.pack(0xFFFF, 0)
        buf = bad + simple_buf(tid=7, ops=[make_creat(2, 12, 'x.txt')])
        recs = decode_fc_buffer(buf)
        # Unknown tag skipped; valid records still decoded
        creat = [r for r in recs if r.tag == FCTag.CREAT]
        assert len(creat) == 1

    def test_multiple_transactions_tid_tracking(self):
        buf  = simple_buf(tid=1, ops=[make_creat(2, 11, 'a')])
        buf += simple_buf(tid=2, ops=[make_creat(2, 12, 'b')])
        recs = decode_fc_buffer(buf)
        creats = [r for r in recs if r.tag == FCTag.CREAT]
        assert creats[0].tid == 1
        assert creats[1].tid == 2

    def test_pad_mixed_in_buffer(self):
        buf  = make_head(1)
        buf += pack_tlv(FCTag.PAD, b'\x00' * 8)
        buf += make_creat(2, 13, 'file.dat')
        buf += pack_tlv(FCTag.PAD, b'\x00' * 4)
        buf += make_tail(1)
        recs = decode_fc_buffer(buf)
        tags = [r.tag for r in recs]
        assert FCTag.PAD    not in tags
        assert FCTag.CREAT  in tags
        assert FCTag.HEAD   in tags
        assert FCTag.TAIL   in tags


# ─────────────────────────────────────────────────────────────
# 3. Event Builder
# ─────────────────────────────────────────────────────────────

class TestEventBuilder:

    def _build(self, buf: bytes):
        recs = decode_fc_buffer(buf)
        return EventBuilder(recs).build()

    def test_create_event(self):
        evs = self._build(simple_buf(1, [make_creat(2, 15, 'doc.pdf')]))
        creates = [e for e in evs if e.event_type == 'CREATE']
        assert len(creates) == 1
        e = creates[0]
        assert e.ino == 15
        assert e.name == 'doc.pdf'
        assert e.parent_ino == 2

    def test_unlink_event(self):
        evs = self._build(simple_buf(1, [make_unlink(2, 15, 'doc.pdf')]))
        assert any(e.event_type == 'UNLINK' and e.name == 'doc.pdf' for e in evs)

    def test_rename_paired(self):
        evs = self._build(simple_buf(1, [
            make_rename(2, 20, 'src.py', 2, 'dst.py')
        ]))
        renames = [e for e in evs if e.event_type == 'RENAME']
        assert len(renames) == 1
        r = renames[0]
        assert r.name     == 'src.py'
        assert r.new_name == 'dst.py'

    def test_unpaired_unlink_stays_unlink(self):
        buf  = make_head(1)
        buf += pack_tlv(FCTag.UNLINK, STRUCT_FC_DENTRY.pack(2, 20) + b'orphan.txt')
        buf += make_tail(1)
        evs = self._build(buf)
        assert any(e.event_type == 'UNLINK' for e in evs)

    def test_confidence_complete_tx(self):
        evs = self._build(simple_buf(1, [make_creat(2, 15, 'ok.txt')]))
        creates = [e for e in evs if e.event_type == 'CREATE']
        assert creates[0].confidence == CONFIDENCE_HIGH
        assert creates[0].commit_complete is True

    def test_confidence_incomplete_tx(self):
        # HEAD present, TAIL absent
        buf = make_head(1) + make_creat(2, 15, 'partial.txt')
        # no TAIL
        evs = self._build(buf)
        creates = [e for e in evs if e.event_type == 'CREATE']
        assert creates[0].confidence == CONFIDENCE_MEDIUM
        assert creates[0].commit_complete is False

    def test_reserved_inode_low_confidence(self):
        # ino=5 is a reserved ext4 inode → confidence LOW
        buf = simple_buf(1, [make_creat(2, 5, 'reserved')])
        evs = self._build(buf)
        creates = [e for e in evs if e.event_type == 'CREATE']
        assert creates[0].confidence == CONFIDENCE_LOW

    def test_sequence_numbering(self):
        buf  = simple_buf(1, [make_creat(2, 11, 'a')])
        buf += simple_buf(2, [make_creat(2, 12, 'b'), make_unlink(2, 11, 'a')])
        evs = self._build(buf)
        seqs = [e.seq for e in evs]
        assert seqs == list(range(len(evs)))

    def test_extent_event(self):
        evs = self._build(simple_buf(1, [make_extent(50)]))
        exts = [e for e in evs if e.event_type == 'EXTENT_ADD']
        assert len(exts) == 1
        assert exts[0].ino == 50
        assert exts[0].physical_block == 100

    def test_del_range_event(self):
        # DEL_RANGE → EXTENT_DEL; logical_block and extent_len set; physical_block=0
        evs = self._build(simple_buf(1, [make_del_range(ino=60, lblk_start=5, lblk_len=12)]))
        dels = [e for e in evs if e.event_type == 'EXTENT_DEL']
        assert len(dels) == 1
        d = dels[0]
        assert d.ino            == 60
        assert d.logical_block  == 5
        assert d.extent_len     == 12
        assert d.physical_block == 0   # not available in DEL_RANGE

    def test_multiple_events_same_transaction(self):
        ops = [
            make_creat(2,  11, 'a.txt'),
            make_creat(2,  12, 'b.txt'),
            make_unlink(2, 11, 'a.txt'),
        ]
        evs = self._build(simple_buf(1, ops))
        etypes = [e.event_type for e in evs]
        assert etypes.count('CREATE') == 2
        assert etypes.count('UNLINK') == 1


# ─────────────────────────────────────────────────────────────
# 4. Diff Engine (metrics)
# ─────────────────────────────────────────────────────────────

class TestDiffEngine:

    def _eval(self, gt, pred, **kw):
        return DiffEngine(gt, pred, **kw).evaluate()

    def test_perfect_match(self):
        gt   = [gt_event('CREATE', 11, name='a')]
        pred = [gt_event('CREATE', 11, name='a')]
        r = self._eval(gt, pred)
        assert r.tp == 1
        assert r.fp == 0
        assert r.fn == 0
        assert r.recall    == 1.0
        assert r.precision == 1.0
        assert r.f1        == 1.0

    def test_all_false_negative(self):
        gt   = [gt_event('CREATE', 11), gt_event('UNLINK', 12)]
        pred = []
        r = self._eval(gt, pred)
        assert r.tp == 0
        assert r.fn == 2
        assert r.recall == 0.0

    def test_all_false_positive(self):
        gt   = []
        pred = [gt_event('CREATE', 11), gt_event('UNLINK', 12)]
        r = self._eval(gt, pred)
        assert r.tp == 0
        assert r.fp == 2
        assert r.precision == 0.0

    def test_partial_match(self):
        gt   = [gt_event('CREATE', 11, name='a'),
                gt_event('UNLINK', 12, name='b'),
                gt_event('RENAME', 13, name='c')]
        pred = [gt_event('CREATE', 11, name='a'),
                gt_event('UNLINK', 12, name='b')]
        r = self._eval(gt, pred)
        assert r.tp == 2
        assert r.fn == 1
        assert r.fp == 0
        assert abs(r.recall - 2/3) < 1e-6
        assert r.precision == 1.0

    def test_f1_harmonic_mean(self):
        r = self._eval(
            [gt_event('CREATE', i) for i in range(10)],
            [gt_event('CREATE', i) for i in range(6)]
        )
        assert abs(r.recall - 0.6)   < 1e-6
        assert abs(r.precision - 1.0) < 1e-6
        expected_f1 = 2 * 0.6 * 1.0 / (0.6 + 1.0)
        assert abs(r.f1 - expected_f1) < 1e-6

    def test_ordering_perfect(self):
        gt = [gt_event('CREATE', i) for i in [11, 12, 13]]
        pd = [gt_event('CREATE', i) for i in [11, 12, 13]]
        r = self._eval(gt, pd)
        assert r.ordering_acc == 1.0

    def test_ordering_reversed(self):
        gt = [gt_event('CREATE', i) for i in [11, 12, 13]]
        pd = [gt_event('CREATE', i) for i in [13, 12, 11]]
        r = self._eval(gt, pd)
        assert r.ordering_acc == 0.0

    def test_path_rate_full(self):
        gt   = [gt_event('CREATE', 11, name='file.txt'),
                gt_event('UNLINK', 12, name='other.log')]
        pred = [gt_event('CREATE', 11, name='file.txt'),
                gt_event('UNLINK', 12, name='other.log')]
        r = self._eval(gt, pred)
        assert r.path_rate == 1.0

    def test_path_rate_partial(self):
        gt   = [gt_event('CREATE', 11, name='correct.txt'),
                gt_event('UNLINK', 12, name='correct.log')]
        pred = [gt_event('CREATE', 11, name='wrong.txt'),   # name mismatch
                gt_event('UNLINK', 12, name='correct.log')]
        r = self._eval(gt, pred)
        assert abs(r.path_rate - 0.5) < 1e-6

    def test_path_rate_rename_checks_destination(self):
        gt = [gt_event('RENAME', 11, name='old.txt',
                       new_name='new.txt', new_parent=2)]
        pred = [gt_event('RENAME', 11, name='old.txt',
                         new_name='wrong.txt', new_parent=2)]
        r = self._eval(gt, pred)
        assert r.tp == 1
        assert r.path_rate == 0.0

    def test_path_rate_rename_checks_new_parent(self):
        gt = [gt_event('RENAME', 11, name='old.txt',
                       new_name='new.txt', new_parent=3)]
        pred = [gt_event('RENAME', 11, name='old.txt',
                         new_name='new.txt', new_parent=2)]
        r = self._eval(gt, pred)
        assert r.tp == 1
        assert r.path_rate == 0.0

    def test_name_fallback_requires_unknown_inode(self):
        gt = [gt_event('CREATE', 11, name='same.txt')]
        pred = [gt_event('CREATE', 99, name='same.txt')]
        r = self._eval(gt, pred)
        assert r.tp == 0
        assert r.fp == 1
        assert r.fn == 1

    def test_empty_both(self):
        r = self._eval([], [])
        assert r.tp == r.fp == r.fn == 0

    def test_safe_div(self):
        assert _safe_div(0, 0) == 0.0
        assert _safe_div(3, 4) == 0.75

    def test_harmonic_mean(self):
        assert _harmonic(0.0, 0.0) == 0.0
        assert abs(_harmonic(1.0, 1.0) - 1.0) < 1e-9
        assert abs(_harmonic(0.5, 0.5) - 0.5) < 1e-9


# ─────────────────────────────────────────────────────────────
# 5. Reporters
# ─────────────────────────────────────────────────────────────

class TestReporters:

    def _sample_events(self):
        buf = simple_buf(1, [
            make_creat(2, 15, 'report.pdf'),
            make_unlink(2, 15, 'report.pdf'),
        ])
        recs = decode_fc_buffer(buf)
        return [e.to_dict() for e in EventBuilder(recs).build()]

    def test_json_round_trip(self, tmp_path):
        events = self._sample_events()
        path = tmp_path / 'out.json'
        JSONReporter(path).write(events)
        loaded = json.loads(path.read_text())
        assert len(loaded) == len(events)
        assert loaded[0]['event_type'] == 'CREATE'
        assert loaded[1]['event_type'] == 'UNLINK'
        assert loaded[0]['name'] == 'report.pdf'

    def test_csv_header_and_rows(self, tmp_path):
        events = self._sample_events()
        path = tmp_path / 'out.csv'
        CSVReporter(path).write(events)
        lines = path.read_text().splitlines()
        assert 'event_type' in lines[0]
        assert len(lines) == len(events) + 1   # header + data rows

    def test_json_empty(self, tmp_path):
        path = tmp_path / 'empty.json'
        JSONReporter(path).write([])
        assert json.loads(path.read_text()) == []

    def test_text_reporter_stdout(self, capsys):
        events = self._sample_events()
        TextReporter().write(events, image_path='inputs/test.img')
        out = capsys.readouterr().out
        assert 'CREATE' in out
        assert 'UNLINK' in out
        assert 'report.pdf' in out

    def test_json_confidence_preserved(self, tmp_path):
        events = self._sample_events()
        path = tmp_path / 'conf.json'
        JSONReporter(path).write(events)
        loaded = json.loads(path.read_text())
        for ev in loaded:
            assert 0.0 <= ev['confidence'] <= 1.0


# ─────────────────────────────────────────────────────────────
# 6. Image Reader — error path testing (no real disk needed)
# ─────────────────────────────────────────────────────────────

class TestImageReaderErrors:

    def test_missing_image_raises(self):
        from fctrace.io.image_reader import Ext4Image, ImageReadError
        with pytest.raises(ImageReadError):
            with Ext4Image('/nonexistent/path/to/image.img') as img:
                pass

    def test_wrong_magic_raises(self, tmp_path):
        from fctrace.io.image_reader import Ext4Image, ImageReadError
        # Create a 2KB file with zeroes (magic = 0x0000, not 0xEF53)
        fake = tmp_path / 'bad.img'
        fake.write_bytes(b'\x00' * 2048)
        with pytest.raises(ImageReadError, match="Not an ext4 filesystem"):
            with Ext4Image(str(fake)) as img:
                pass

    def test_valid_magic_accepted(self, tmp_path):
        from fctrace.io.image_reader import Ext4Image, ImageReadError, EXT4_SUPERBLOCK_OFFSET
        # Craft a minimal superblock with correct magic
        sb = bytearray(1024)
        struct.pack_into('<H', sb, 0x38, 0xEF53)   # s_magic
        struct.pack_into('<I', sb, 0x18, 2)         # log_block_size=2 → 4096
        struct.pack_into('<I', sb, 0x28, 256)       # inodes_per_group
        # s_feature_compat at 0x5C — NOT 0x60 (that is s_feature_incompat)
        data = b'\x00' * EXT4_SUPERBLOCK_OFFSET + bytes(sb)
        fake = tmp_path / 'ok.img'
        fake.write_bytes(data + b'\x00' * 4096)
        with Ext4Image(str(fake)) as img:
            assert img.block_size == 4096
            assert img.has_fast_commit is False   # flag not set

    def test_fast_commit_flag_detected(self, tmp_path):
        from fctrace.io.image_reader import Ext4Image, EXT4_SUPERBLOCK_OFFSET
        # Same minimal superblock, but set EXT4_FEATURE_COMPAT_FAST_COMMIT (0x0400)
        # in s_feature_compat at offset 0x5C — verifies the correct field is read.
        sb = bytearray(1024)
        struct.pack_into('<H', sb, 0x38, 0xEF53)   # s_magic
        struct.pack_into('<I', sb, 0x18, 2)         # log_block_size=2 → 4096
        struct.pack_into('<I', sb, 0x28, 256)       # inodes_per_group
        struct.pack_into('<I', sb, 0x5C, 0x0400)   # s_feature_compat: FAST_COMMIT
        data = b'\x00' * EXT4_SUPERBLOCK_OFFSET + bytes(sb)
        fake = tmp_path / 'fc.img'
        fake.write_bytes(data + b'\x00' * 4096)
        with Ext4Image(str(fake)) as img:
            assert img.has_fast_commit is True
            # Confirm 0x4000 in s_feature_incompat (LARGEDIR) does NOT set flag
            assert not bool(img.feature_incompat & 0x0400)


# ─────────────────────────────────────────────────────────────
# 7. Integration — full pipeline on synthetic buffer
# ─────────────────────────────────────────────────────────────

class TestIntegration:

    def test_full_pipeline_create_rename_unlink(self, tmp_path):
        """Simulate a full investigation: create → rename → unlink."""
        buf  = simple_buf(1, [make_creat(2,  11, 'evidence.docx')])
        buf += simple_buf(2, [make_rename(2, 11, 'evidence.docx', 2, 'deleted.docx')])
        buf += simple_buf(3, [make_unlink(2, 11, 'deleted.docx')])

        recs = decode_fc_buffer(buf)
        evs  = EventBuilder(recs).build()
        dicts = [e.to_dict() for e in evs]

        # Write and reload via JSON
        out = tmp_path / 'timeline.json'
        JSONReporter(out).write(dicts)
        loaded = json.loads(out.read_text())

        etypes = [e['event_type'] for e in loaded]
        assert 'CREATE' in etypes
        assert 'RENAME' in etypes
        assert 'UNLINK' in etypes

        # Ground truth
        gt = [
            gt_event('CREATE', 11, name='evidence.docx'),
            gt_event('RENAME', 11, name='evidence.docx', new_name='deleted.docx'),
            gt_event('UNLINK', 11, name='deleted.docx'),
        ]
        result = DiffEngine(gt, loaded, method='FC-Trace', scenario='integration').evaluate()
        assert result.tp == 3
        assert result.recall    == 1.0
        assert result.precision == 1.0
        assert result.f1        == 1.0

    def test_antiforensic_pattern_detected(self):
        """Rapid create/unlink burst: FC-Trace sees all; baseline B1 sees none."""
        ops_fc = []
        gt = []
        for i in range(5):
            ino  = 100 + i
            name = f'secret_{i}.bin'
            ops_fc.append(make_creat(2, ino, name))
            ops_fc.append(make_unlink(2, ino, name))
            gt.append(gt_event('CREATE', ino, name=name))
            gt.append(gt_event('UNLINK', ino, name=name))

        buf  = simple_buf(10, ops_fc)
        recs = decode_fc_buffer(buf)
        evs  = [e.to_dict() for e in EventBuilder(recs).build()]

        result_fc = DiffEngine(gt, evs, method='FC-Trace', scenario='S3').evaluate()
        result_b1 = DiffEngine(gt, [],  method='B1',       scenario='S3').evaluate()

        assert result_fc.recall > result_b1.recall
        assert result_fc.tp     > result_b1.tp


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
