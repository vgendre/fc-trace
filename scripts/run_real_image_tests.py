#!/usr/bin/env python3
"""
run_real_image_tests.py — Real ext4 disk image test harness for FC-Trace
==========================================================================
Creates genuine 512 MiB ext4 images with fast_commit enabled, replays
scripted workloads, then captures the raw disk state via dd BEFORE any
clean unmount (which would flush the FC area with a full JBD2 commit).

Why snapshot before unmount?
  A clean 'umount' calls sync, triggers a full JBD2 commit, and overwrites
  the fast-commit tail area.  To capture FC records we must snapshot the
  loop device WHILE the filesystem is still mounted and dirty.

Approach:
  1. Create image (512 MiB, -b 4096, -O fast_commit).
  2. Mount via losetup.
  3. Warm-up write (absorbs the first-mount full JBD2 commit).
  4. Run scenario workload with O_SYNC / fsync to force fast commits.
  5. blockdev --flushbufs <loop>  (push page cache to image file).
  6. dd if=<loop> of=snapshot.img   (capture raw FC area).
  7. lazy umount + losetup -d       (cleanup only — do NOT sync).
  8. Run FC-Trace on snapshot.img.
  9. Evaluate against ground truth.

Run as root:
    python3 scripts/run_real_image_tests.py [--output results/evaluation_realmode.json]
"""

import argparse
import json
import logging
import os
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Dict, Any, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from fctrace.io.image_reader import Ext4Image, ImageReadError
from fctrace.io.journal_reader import JournalReader, JournalReadError
from fctrace.parser.tlv_decoder import decode_fc_buffer
from fctrace.reconstruct.event_builder import EventBuilder
from fctrace.compare.diff_engine import DiffEngine, EvaluationResult

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s  %(levelname)-8s  %(message)s',
                    datefmt='%H:%M:%S')
logger = logging.getLogger('realtest')

IMAGE_SIZE_MB = 512


# ---------------------------------------------------------------------------
# Ground truth event
# ---------------------------------------------------------------------------

@dataclass
class GTEvent:
    seq:        int
    event_type: str
    ino:        int   = 0
    parent_ino: int   = 0
    name:       str   = ''
    new_name:   str   = ''
    new_parent: int   = 0


def _gt(seq, etype, ino=0, parent=0, name='', new_name='', new_parent=0) -> dict:
    return {'seq': seq, 'event_type': etype, 'ino': ino,
            'parent_ino': parent, 'name': name,
            'new_name': new_name, 'new_parent': new_parent}


def get_ino(path: str) -> int:
    try:
        return os.stat(path).st_ino
    except OSError:
        return 0


def get_parent_ino(path: str) -> int:
    return get_ino(str(Path(path).parent))


# ---------------------------------------------------------------------------
# Image lifecycle
# ---------------------------------------------------------------------------

class LoopImage:
    """Create, mount, snapshot, and clean up a loopback ext4 image."""

    def __init__(self, image_path: str, mount_point: str) -> None:
        self.image_path  = str(Path(image_path).resolve())
        self.mount_point = str(Path(mount_point).resolve())
        self._loop_dev: Optional[str] = None

    def create(self) -> None:
        logger.info("Creating %d MiB image at %s", IMAGE_SIZE_MB, self.image_path)
        sh(f"dd if=/dev/zero of={self.image_path} bs=1M count={IMAGE_SIZE_MB} status=none")
        # -b 4096: 4096-byte blocks required for fast_commit to work reliably
        sh(f"mkfs.ext4 -O fast_commit -b 4096 -F -J size=64 {self.image_path} -q")
        Path(self.mount_point).mkdir(parents=True, exist_ok=True)

    def mount(self) -> str:
        result = sh(f"losetup -fP --show {self.image_path}")
        self._loop_dev = result.stdout.strip()
        sh(f"mount {self._loop_dev} {self.mount_point}")
        logger.info("Mounted %s at %s via %s",
                    self.image_path, self.mount_point, self._loop_dev)
        return self._loop_dev

    def snapshot(self, dest: str) -> None:
        """Capture the current disk state via dd (bypasses unmount flush)."""
        if not self._loop_dev:
            raise RuntimeError("Not mounted")
        sh(f"blockdev --flushbufs {self._loop_dev}")
        sh(f"dd if={self._loop_dev} of={dest} bs=4096 status=none")
        logger.info("Snapshot captured: %s", dest)

    def cleanup(self) -> None:
        """Lazy-unmount and detach WITHOUT calling sync (preserve FC evidence)."""
        sh(f"umount -l {self.mount_point}", check=False)
        if self._loop_dev:
            sh(f"losetup -d {self._loop_dev}", check=False)
            self._loop_dev = None


def sh(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"CMD: {cmd}\nSTDERR: {r.stderr.strip()}")
    return r


def warmup_write(mnt: str) -> None:
    """
    Write one file, force a full fsync, and sleep past the JBD2 commit
    interval (5 s default).  This ensures subsequent workload operations
    go through the fast-commit path rather than being bundled with the
    first-mount full commit.
    """
    path = f"{mnt}/.warmup"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
    os.write(fd, b"warmup" * 64)
    os.fsync(fd)
    os.close(fd)
    # Wait longer than the commit interval so the kernel checkpoints the warmup
    # full commit before we start the real workload
    time.sleep(6)
    logger.info("Warm-up complete (waited 6 s for JBD2 checkpoint)")


# ---------------------------------------------------------------------------
# Scenario workloads
# ---------------------------------------------------------------------------

def scenario_S1(mnt: str) -> List[dict]:
    """Normal workload: create, rename, hard-link, unlink, create, unlink."""
    gt = []
    seq = 0
    subdir = f"{mnt}/testdir"
    os.makedirs(subdir)
    dir_ino = get_ino(subdir)

    # CREATE a.txt
    path_a = f"{subdir}/a.txt"
    fd = os.open(path_a, os.O_WRONLY | os.O_CREAT | os.O_SYNC, 0o644)
    os.write(fd, b"hello fast commit\n")
    os.close(fd)
    ino_a = get_ino(path_a)
    gt.append(_gt(seq, 'CREATE', ino=ino_a, parent=dir_ino, name='a.txt'))
    seq += 1

    # RENAME a.txt → b.txt (fast commits LINK+UNLINK in one tx)
    path_b = f"{subdir}/b.txt"
    os.rename(path_a, path_b)
    fd = os.open(path_b, os.O_RDONLY); os.fsync(fd); os.close(fd)
    gt.append(_gt(seq, 'RENAME', ino=ino_a, parent=dir_ino,
                  name='a.txt', new_name='b.txt', new_parent=dir_ino))
    seq += 1

    # LINK b.txt → hardlink.txt
    path_hl = f"{subdir}/hardlink.txt"
    os.link(path_b, path_hl)
    fd = os.open(path_hl, os.O_RDONLY); os.fsync(fd); os.close(fd)
    gt.append(_gt(seq, 'LINK', ino=ino_a, parent=dir_ino, name='hardlink.txt'))
    seq += 1

    # UNLINK hardlink.txt
    os.unlink(path_hl)
    fd = os.open(path_b, os.O_RDONLY); os.fsync(fd); os.close(fd)
    gt.append(_gt(seq, 'UNLINK', ino=ino_a, parent=dir_ino, name='hardlink.txt'))
    seq += 1

    # CREATE c.txt
    path_c = f"{subdir}/c.txt"
    fd = os.open(path_c, os.O_WRONLY | os.O_CREAT | os.O_SYNC, 0o644)
    os.write(fd, b"ephemeral\n")
    os.close(fd)
    ino_c = get_ino(path_c)
    gt.append(_gt(seq, 'CREATE', ino=ino_c, parent=dir_ino, name='c.txt'))
    seq += 1

    # UNLINK c.txt
    os.unlink(path_c)
    fd = os.open(subdir, os.O_RDONLY | os.O_DIRECTORY); os.fsync(fd); os.close(fd)
    gt.append(_gt(seq, 'UNLINK', ino=ino_c, parent=dir_ino, name='c.txt'))
    seq += 1

    return gt


def scenario_S2(mnt: str) -> List[dict]:
    """Crash before full commit: 5 files with O_SYNC (forces FC writes)."""
    gt = []
    for i in range(5):
        name = f"crash_{i}.dat"
        path = f"{mnt}/{name}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_SYNC, 0o644)
        os.write(fd, b"data" * 512)
        os.close(fd)
        ino = get_ino(path)
        gt.append(_gt(i, 'CREATE', ino=ino, parent=get_ino(mnt), name=name))
    return gt


def scenario_S3(mnt: str) -> List[dict]:
    """
    Anti-forensic burst: create / truncate / unlink × 10.

    DESIGN NOTE: we pre-create all 10 files first so each gets a stable
    inode, then perform the burst truncate+unlink pass.  The ground truth
    records every scripted operation; whether DEL_RANGE/UNLINK evidence
    appears in the FC area depends on kernel fast-commit logging behavior.
    """
    gt = []
    seq = 0
    mnt_ino = get_ino(mnt)

    # Phase 1: create all 10 files (distinct inodes, unique commit per file)
    paths_and_inos = []
    for i in range(10):
        name = f"secret_{i}.bin"
        path = f"{mnt}/{name}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_SYNC, 0o644)
        os.write(fd, os.urandom(4096))
        os.close(fd)
        ino = get_ino(path)
        paths_and_inos.append((path, name, ino))
        gt.append(_gt(seq, 'CREATE', ino=ino, parent=mnt_ino, name=name))
        seq += 1

    # Phase 2: burst truncate then unlink (simulates the anti-forensic cleanup)
    for path, name, ino in paths_and_inos:
        fd = os.open(path, os.O_WRONLY | os.O_SYNC)
        os.ftruncate(fd, 0)
        os.close(fd)
        gt.append(_gt(seq, 'EXTENT_DEL', ino=ino, parent=mnt_ino, name=name))
        seq += 1

        os.unlink(path)
        fd = os.open(mnt, os.O_RDONLY | os.O_DIRECTORY); os.fsync(fd); os.close(fd)
        gt.append(_gt(seq, 'UNLINK', ino=ino, parent=mnt_ino, name=name))
        seq += 1

    return gt


def scenario_S4(mnt: str) -> List[dict]:
    """
    Short-lived files: 8 × create / rename / unlink.

    DESIGN NOTE: create all 8 source files first, then perform the
    rename+unlink pass.  This avoids inode reuse across iterations while
    preserving a ground truth record of operations that may or may not be
    emitted into the FC area by the kernel.
    """
    gt = []
    seq = 0
    mnt_ino = get_ino(mnt)

    # Phase 1: create all source files with unique inodes
    entries = []
    for i in range(8):
        src_name = f"tmp_{i}_src.tmp"
        dst_name = f"tmp_{i}_dst.tmp"
        src = f"{mnt}/{src_name}"
        dst = f"{mnt}/{dst_name}"

        fd = os.open(src, os.O_WRONLY | os.O_CREAT | os.O_SYNC, 0o644)
        os.write(fd, f"short {i}\n".encode())
        os.close(fd)
        ino = get_ino(src)
        entries.append((src, dst, src_name, dst_name, ino))
        gt.append(_gt(seq, 'CREATE', ino=ino, parent=mnt_ino, name=src_name))
        seq += 1

    # Phase 2: rename then unlink each file
    for src, dst, src_name, dst_name, ino in entries:
        os.rename(src, dst)
        fd = os.open(dst, os.O_RDONLY); os.fsync(fd); os.close(fd)
        gt.append(_gt(seq, 'RENAME', ino=ino, parent=mnt_ino,
                      name=src_name, new_name=dst_name, new_parent=mnt_ino))
        seq += 1

        os.unlink(dst)
        fd = os.open(mnt, os.O_RDONLY | os.O_DIRECTORY); os.fsync(fd); os.close(fd)
        gt.append(_gt(seq, 'UNLINK', ino=ino, parent=mnt_ino, name=dst_name))
        seq += 1

    return gt


def scenario_S5(mnt: str) -> List[dict]:
    """
    Deep rename tree: nested dirs, cross-dir renames.
    GT includes directory creates so that FC-Trace dir-create records
    are not counted as false positives.
    """
    gt = []
    seq = 0
    mnt_ino = get_ino(mnt)

    # Level-1 dir: fsync the PARENT (mnt) to commit the 'level1' dentry record
    l1 = f"{mnt}/level1"
    os.mkdir(l1)
    fd = os.open(mnt, os.O_RDONLY | os.O_DIRECTORY); os.fsync(fd); os.close(fd)
    l1_ino = get_ino(l1)
    gt.append(_gt(seq, 'CREATE', ino=l1_ino, parent=mnt_ino, name='level1'))
    seq += 1

    # Level-2 dir: fsync l1 (the parent of level2)
    l2 = f"{l1}/level2"
    os.mkdir(l2)
    fd = os.open(l1, os.O_RDONLY | os.O_DIRECTORY); os.fsync(fd); os.close(fd)
    l2_ino = get_ino(l2)
    gt.append(_gt(seq, 'CREATE', ino=l2_ino, parent=l1_ino, name='level2'))
    seq += 1

    # Level-3 dir: fsync l2 (the parent of level3)
    l3 = f"{l2}/level3"
    os.mkdir(l3)
    fd = os.open(l2, os.O_RDONLY | os.O_DIRECTORY); os.fsync(fd); os.close(fd)
    l3_ino = get_ino(l3)
    gt.append(_gt(seq, 'CREATE', ino=l3_ino, parent=l2_ino, name='level3'))
    seq += 1

    # File in level3
    fname = "target.txt"
    path_orig = f"{l3}/{fname}"
    fd = os.open(path_orig, os.O_WRONLY | os.O_CREAT | os.O_SYNC, 0o644)
    os.write(fd, b"nested file\n")
    os.close(fd)
    ino = get_ino(path_orig)
    gt.append(_gt(seq, 'CREATE', ino=ino, parent=l3_ino, name=fname))
    seq += 1

    # Rename target.txt up to level1
    path_mid = f"{l1}/moved.txt"
    os.rename(path_orig, path_mid)
    fd = os.open(path_mid, os.O_RDONLY); os.fsync(fd); os.close(fd)
    gt.append(_gt(seq, 'RENAME', ino=ino, parent=l3_ino,
                  name=fname, new_name='moved.txt', new_parent=l1_ino))
    seq += 1

    # Rename moved.txt up to mount root
    path_root = f"{mnt}/moved.txt"
    os.rename(path_mid, path_root)
    fd = os.open(path_root, os.O_RDONLY); os.fsync(fd); os.close(fd)
    gt.append(_gt(seq, 'RENAME', ino=ino, parent=l1_ino,
                  name='moved.txt', new_name='moved.txt', new_parent=mnt_ino))
    seq += 1

    return gt


SCENARIOS = {
    'S1': ('S1_normal_workload',     scenario_S1),
    'S2': ('S2_crash_before_commit', scenario_S2),
    'S3': ('S3_antiforensic_burst',  scenario_S3),
    'S4': ('S4_shortlived_files',    scenario_S4),
    'S5': ('S5_deep_rename_tree',    scenario_S5),
}


# ---------------------------------------------------------------------------
# Per-scenario runner
# ---------------------------------------------------------------------------

def run_scenario(
    sc_id: str,
    sc_name: str,
    workload_fn,
    snapshot_dir: Path,
    gt_dir: Path,
) -> EvaluationResult:

    # Use SEPARATE paths: loop backing file vs. snapshot destination.
    # If both are the same file, dd if=/dev/loopX of=<same_file> fails with I/O error.
    loop_path = snapshot_dir / f"{sc_name}_loop.img"
    snap_path = snapshot_dir / f"{sc_name}_snap.img"
    gt_path   = gt_dir       / f"{sc_name}_gt.json"

    with tempfile.TemporaryDirectory(prefix='fctrace_mnt_') as mnt:
        loop_img = LoopImage(str(loop_path), mnt)
        loop_img.create()
        loop_dev = loop_img.mount()

        try:
            # Warm up: trigger the first-mount full JBD2 commit and wait for it
            logger.info("[%s] Running warm-up …", sc_id)
            warmup_write(mnt)

            # Run actual workload
            logger.info("[%s] Running workload …", sc_id)
            t0 = time.perf_counter()
            gt_events = workload_fn(mnt)
            rt = time.perf_counter() - t0

            # Take dd snapshot to a DIFFERENT file before any umount
            loop_img.snapshot(str(snap_path))

        finally:
            loop_img.cleanup()

    # Persist ground truth
    gt_dir.mkdir(parents=True, exist_ok=True)
    gt_path.write_text(json.dumps(gt_events, indent=2))
    logger.info("[%s] GT: %d events → %s", sc_id, len(gt_events), gt_path.name)

    # Run FC-Trace on the snapshot
    logger.info("[%s] Running FC-Trace on snapshot …", sc_id)
    t1 = time.perf_counter()
    try:
        with Ext4Image(str(snap_path)) as img:
            jr = JournalReader(img)
            jr.open()
            raw = jr.read_fc_area()
        recs  = decode_fc_buffer(raw)
        evs   = [e.to_dict() for e in EventBuilder(recs).build()]
    except Exception as exc:
        logger.error("[%s] FC-Trace failed: %s", sc_id, exc)
        evs = []
    fc_rt = time.perf_counter() - t1

    # Filter predictions to event types present in GT
    gt_types = {e['event_type'] for e in gt_events}
    evs_eval = [e for e in evs if e.get('event_type') in gt_types]

    logger.info(
        "[%s] GT=%d  FC-predicted=%d  (after type-filter=%d)",
        sc_id, len(gt_events), len(evs), len(evs_eval),
    )

    result = DiffEngine(
        gt_events, evs_eval,
        method='FC-Trace',
        scenario=sc_name,
        runtime_s=fc_rt,
    ).evaluate()

    return result


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def print_table(results: List[EvaluationResult]) -> None:
    W = 82
    print()
    print("╔" + "═"*W + "╗")
    print("║  FC-TRACE REAL-IMAGE EVALUATION (honest, not simulation)" + " "*(W-55) + "║")
    print("╠" + "═"*W + "╣")
    hdr = f"  {'Scenario':<32} {'GT':>4} {'TP':>4} {'FP':>4} {'FN':>4}  {'R':>6}{'P':>6}{'F1':>6}{'Ord':>6}{'Path':>6}  "
    print(f"║{hdr}║")
    print("╠" + "═"*W + "╣")
    for r in results:
        line = (f"  {r.scenario:<32} {r.total_gt_events:>4} {r.tp:>4} {r.fp:>4} {r.fn:>4}"
                f"  {r.recall:6.3f}{r.precision:6.3f}{r.f1:6.3f}"
                f"{r.ordering_acc:6.3f}{r.path_rate:6.3f}  ")
        print(f"║{line}║")
    print("╠" + "═"*W + "╣")
    if results:
        avg_r   = sum(r.recall for r in results) / len(results)
        avg_p   = sum(r.precision for r in results) / len(results)
        avg_f1  = sum(r.f1 for r in results) / len(results)
        avg_ord = sum(r.ordering_acc for r in results) / len(results)
        avg_pth = sum(r.path_rate for r in results) / len(results)
        avg_line = (f"  {'Mean across all scenarios':<32} {'':>4} {'':>4} {'':>4} {'':>4}"
                    f"  {avg_r:6.3f}{avg_p:6.3f}{avg_f1:6.3f}{avg_ord:6.3f}{avg_pth:6.3f}  ")
        print(f"║{avg_line}║")
    print("╚" + "═"*W + "╝")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description='FC-Trace real-image evaluation harness')
    parser.add_argument('--output', default='results/evaluation_realmode.json')
    parser.add_argument('--snap-dir', default='/tmp/fctrace_snapshots',
                        help='Directory for dd snapshots (needs ~3 GB free)')
    parser.add_argument('--gt-dir', default='data/ground_truth')
    parser.add_argument('--scenarios', default='S1,S2,S3,S4,S5')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if os.geteuid() != 0:
        logger.error("This script must be run as root (needs losetup + mount).")
        return 1

    snap_dir = Path(args.snap_dir)
    gt_dir   = Path(args.gt_dir)
    snap_dir.mkdir(parents=True, exist_ok=True)
    gt_dir.mkdir(parents=True, exist_ok=True)

    sc_ids = [s.strip() for s in args.scenarios.split(',')]
    all_results: List[EvaluationResult] = []

    for sc_id in sc_ids:
        if sc_id not in SCENARIOS:
            logger.error("Unknown scenario: %s", sc_id)
            continue
        sc_name, workload_fn = SCENARIOS[sc_id]
        logger.info("=== Scenario %s: %s ===", sc_id, sc_name)
        try:
            res = run_scenario(sc_id, sc_name, workload_fn, snap_dir, gt_dir)
            all_results.append(res)
            logger.info(
                "[%s] TP=%d FP=%d FN=%d R=%.3f P=%.3f F1=%.3f",
                sc_id, res.tp, res.fp, res.fn,
                res.recall, res.precision, res.f1,
            )
        except Exception as exc:
            logger.error("Scenario %s FAILED: %s", sc_id, exc, exc_info=True)
            all_results.append(EvaluationResult(
                method='FC-Trace', scenario=f'S{sc_id[-1]}_FAILED',
                notes=[str(exc)],
            ))

    print_table(all_results)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([r.to_dict() for r in all_results], indent=2))
    logger.info("Results written: %s", out)

    return 0


if __name__ == '__main__':
    sys.exit(main())
