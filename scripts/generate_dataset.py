#!/usr/bin/env python3
"""
generate_dataset.py — Ground-truth dataset generator for FC-Trace evaluation
=============================================================================
Creates a set of labelled ext4 disk-image snapshots with known file-system
operations. Each scenario:
  1. Creates a 512 MiB loopback image with fast_commit enabled.
  2. Mounts it and performs a warm-up write so the first full commit is
     checkpointed before the workload begins.
  3. Replays a scripted workload and records ground-truth events.
  4. Captures a raw dd snapshot of the mounted loop device before unmount.
  5. Saves the snapshot image + ground truth to --output-dir / --gt-dir.

Scenarios
---------
  S1  normal_workload     — create / rename / unlink / hard-link sequences
  S2  crash_before_commit — crash right after fast commits, before full commit
  S3  antiforensic_burst  — rapid create/truncate/unlink to simulate cleanup
  S4  shortlived_files    — ephemeral files within single commit window
  S5  deep_rename_tree    — directory tree restructuring with deep renames

Requirements
------------
  sudo / root  (for losetup, mount, umount, blockdev)
  mkfs.ext4 with fast_commit support  (e2fsprogs >= 1.46.3)
  Linux kernel >= 5.10

Usage::

    sudo python3 generate_dataset.py \
        --output-dir ./data/raw_images \
        --gt-dir ./data/ground_truth \
        --scenarios S1,S2,S3
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(message)s',
    datefmt='%H:%M:%S',
)

IMAGE_SIZE_MB = 512


@dataclass
class GTEvent:
    """One ground-truth file-system operation."""
    seq: int
    event_type: str
    ino: int = 0
    parent_ino: int = 0
    name: str = ''
    new_name: str = ''
    new_parent: int = 0
    extra: dict = field(default_factory=dict)


def sh(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command with logging."""
    logger.debug("$ %s", cmd)
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed (rc={result.returncode}):\n"
            f"  CMD : {cmd}\n"
            f"  STDERR: {result.stderr.strip()}"
        )
    return result


def require_tool(tool: str) -> None:
    if shutil.which(tool) is None:
        raise RuntimeError(f"Required tool not found on PATH: {tool}")


def preflight_requirements() -> None:
    """Validate host prerequisites for real-image generation."""
    require_tool('dd')
    require_tool('blockdev')
    require_tool('mkfs.ext4')
    require_tool('losetup')
    require_tool('mount')
    require_tool('umount')

    if os.geteuid() != 0:
        raise RuntimeError(
            'Dataset generation requires root privileges (run with sudo/root).'
        )

    if not (Path('/dev/loop-control').exists() or list(Path('/dev').glob('loop*'))):
        raise RuntimeError(
            'No loop devices detected (/dev/loop-control or /dev/loopN missing). '
            'Enable loop support on host and retry.'
        )


def get_ino(path: str) -> int:
    """Return the inode number of path, or 0 on failure."""
    try:
        return os.stat(path).st_ino
    except OSError:
        return 0


def fsync_path(path: str) -> None:
    """Flush a file or directory without forcing a global journal commit."""
    flags = os.O_RDONLY
    if hasattr(os, 'O_DIRECTORY') and Path(path).is_dir():
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class LoopImage:
    """Create, mount, snapshot, and clean up a loopback image."""

    def __init__(self, image_path: str, mount_point: str) -> None:
        self.image_path = str(Path(image_path).resolve())
        self.mount_point = str(Path(mount_point).resolve())
        self._loop_dev: Optional[str] = None

    def create(self) -> None:
        logger.info('Creating %d MiB image at %s', IMAGE_SIZE_MB, self.image_path)
        sh(f'dd if=/dev/zero of={self.image_path} bs=1M count={IMAGE_SIZE_MB} status=none')
        sh(f'mkfs.ext4 -O fast_commit -b 4096 -F -J size=64 {self.image_path} -q')
        Path(self.mount_point).mkdir(parents=True, exist_ok=True)

    def mount(self) -> str:
        result = sh(f'losetup -fP --show {self.image_path}', check=True)
        self._loop_dev = result.stdout.strip()
        sh(f'mount {self._loop_dev} {self.mount_point}')
        logger.info('Mounted %s at %s via %s',
                    self.image_path, self.mount_point, self._loop_dev)
        return self._loop_dev

    def snapshot(self, dest: str) -> None:
        """Capture the mounted device state before unmount flushes FC records."""
        if not self._loop_dev:
            raise RuntimeError('Loop device not attached')
        sh(f'blockdev --flushbufs {self._loop_dev}')
        sh(f'dd if={self._loop_dev} of={dest} bs=4096 status=none')
        logger.info('Snapshot captured: %s', dest)

    def cleanup(self) -> None:
        sh(f'umount -l {self.mount_point}', check=False)
        if self._loop_dev:
            sh(f'losetup -d {self._loop_dev}', check=False)
            self._loop_dev = None


def warmup_write(mnt: str) -> None:
    """
    Trigger the mount-time full commit once and wait past the default ext4
    commit interval so later workload operations use the FC path.
    """
    path = f'{mnt}/.warmup'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        os.write(fd, b'warmup' * 64)
        os.fsync(fd)
    finally:
        os.close(fd)
    time.sleep(6)


def scenario_S1_normal(mnt: str) -> List[GTEvent]:
    """Normal workload: create, rename, hard-link, unlink, create, unlink."""
    events: List[GTEvent] = []
    seq = 0
    subdir = f'{mnt}/testdir'
    os.makedirs(subdir, exist_ok=True)
    dir_ino = get_ino(subdir)

    path_a = f'{subdir}/a.txt'
    fd = os.open(path_a, os.O_WRONLY | os.O_CREAT | os.O_SYNC, 0o644)
    os.write(fd, b'hello fast commit\n')
    os.close(fd)
    ino_a = get_ino(path_a)
    events.append(GTEvent(seq=seq, event_type='CREATE', ino=ino_a, parent_ino=dir_ino, name='a.txt'))
    seq += 1

    path_b = f'{subdir}/b.txt'
    os.rename(path_a, path_b)
    fsync_path(path_b)
    events.append(GTEvent(seq=seq, event_type='RENAME', ino=ino_a, parent_ino=dir_ino,
                          name='a.txt', new_name='b.txt', new_parent=dir_ino))
    seq += 1

    path_hl = f'{subdir}/hardlink.txt'
    os.link(path_b, path_hl)
    fsync_path(path_hl)
    events.append(GTEvent(seq=seq, event_type='LINK', ino=ino_a, parent_ino=dir_ino, name='hardlink.txt'))
    seq += 1

    os.unlink(path_hl)
    fsync_path(path_b)
    events.append(GTEvent(seq=seq, event_type='UNLINK', ino=ino_a, parent_ino=dir_ino, name='hardlink.txt'))
    seq += 1

    path_c = f'{subdir}/c.txt'
    fd = os.open(path_c, os.O_WRONLY | os.O_CREAT | os.O_SYNC, 0o644)
    os.write(fd, b'ephemeral\n')
    os.close(fd)
    ino_c = get_ino(path_c)
    events.append(GTEvent(seq=seq, event_type='CREATE', ino=ino_c, parent_ino=dir_ino, name='c.txt'))
    seq += 1

    os.unlink(path_c)
    fsync_path(subdir)
    events.append(GTEvent(seq=seq, event_type='UNLINK', ino=ino_c, parent_ino=dir_ino, name='c.txt'))
    seq += 1

    return events


def scenario_S2_crash(mnt: str) -> List[GTEvent]:
    """Crash scenario: operations logged but full commit not forced."""
    events: List[GTEvent] = []
    seq = 0
    parent_ino = get_ino(mnt)

    for i in range(5):
        name = f'crash_{i}.dat'
        path = f'{mnt}/{name}'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_SYNC, 0o644)
        os.write(fd, b'data' * 512)
        os.close(fd)
        events.append(GTEvent(seq=seq, event_type='CREATE',
                              ino=get_ino(path), parent_ino=parent_ino, name=name))
        seq += 1

    return events


def scenario_S3_antiforensic(mnt: str) -> List[GTEvent]:
    """Anti-forensic burst: create all files, then truncate/unlink them."""
    events: List[GTEvent] = []
    seq = 0
    mnt_ino = get_ino(mnt)
    paths_and_inos = []

    for i in range(10):
        name = f'secret_{i}.bin'
        path = f'{mnt}/{name}'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_SYNC, 0o644)
        os.write(fd, os.urandom(4096))
        os.close(fd)
        ino = get_ino(path)
        paths_and_inos.append((path, name, ino))
        events.append(GTEvent(seq=seq, event_type='CREATE', ino=ino, parent_ino=mnt_ino, name=name))
        seq += 1

    for path, name, ino in paths_and_inos:
        fd = os.open(path, os.O_WRONLY | os.O_SYNC)
        os.ftruncate(fd, 0)
        os.close(fd)
        events.append(GTEvent(seq=seq, event_type='EXTENT_DEL', ino=ino, parent_ino=mnt_ino, name=name))
        seq += 1

        os.unlink(path)
        fsync_path(mnt)
        events.append(GTEvent(seq=seq, event_type='UNLINK', ino=ino, parent_ino=mnt_ino, name=name))
        seq += 1

    return events


def scenario_S4_short_lived(mnt: str) -> List[GTEvent]:
    """Short-lived files: create all, then rename/unlink with stable inodes."""
    events: List[GTEvent] = []
    seq = 0
    mnt_ino = get_ino(mnt)
    entries = []

    for i in range(8):
        src_name = f'tmp_{i}_src.tmp'
        dst_name = f'tmp_{i}_dst.tmp'
        src = f'{mnt}/{src_name}'
        dst = f'{mnt}/{dst_name}'

        fd = os.open(src, os.O_WRONLY | os.O_CREAT | os.O_SYNC, 0o644)
        os.write(fd, f'short {i}\n'.encode())
        os.close(fd)
        ino = get_ino(src)
        entries.append((src, dst, src_name, dst_name, ino))
        events.append(GTEvent(seq=seq, event_type='CREATE', ino=ino, parent_ino=mnt_ino, name=src_name))
        seq += 1

    for src, dst, src_name, dst_name, ino in entries:
        os.rename(src, dst)
        fsync_path(dst)
        events.append(GTEvent(seq=seq, event_type='RENAME', ino=ino, parent_ino=mnt_ino,
                              name=src_name, new_name=dst_name, new_parent=mnt_ino))
        seq += 1

        os.unlink(dst)
        fsync_path(mnt)
        events.append(GTEvent(seq=seq, event_type='UNLINK', ino=ino, parent_ino=mnt_ino, name=dst_name))
        seq += 1

    return events


def scenario_S5_deep_rename(mnt: str) -> List[GTEvent]:
    """Deep directory tree restructuring with directory-create ground truth."""
    events: List[GTEvent] = []
    seq = 0
    mnt_ino = get_ino(mnt)

    l1 = f'{mnt}/level1'
    os.mkdir(l1)
    fsync_path(mnt)
    l1_ino = get_ino(l1)
    events.append(GTEvent(seq=seq, event_type='CREATE', ino=l1_ino, parent_ino=mnt_ino, name='level1'))
    seq += 1

    l2 = f'{l1}/level2'
    os.mkdir(l2)
    fsync_path(l1)
    l2_ino = get_ino(l2)
    events.append(GTEvent(seq=seq, event_type='CREATE', ino=l2_ino, parent_ino=l1_ino, name='level2'))
    seq += 1

    l3 = f'{l2}/level3'
    os.mkdir(l3)
    fsync_path(l2)
    l3_ino = get_ino(l3)
    events.append(GTEvent(seq=seq, event_type='CREATE', ino=l3_ino, parent_ino=l2_ino, name='level3'))
    seq += 1

    path_orig = f'{l3}/target.txt'
    fd = os.open(path_orig, os.O_WRONLY | os.O_CREAT | os.O_SYNC, 0o644)
    os.write(fd, b'nested file\n')
    os.close(fd)
    ino = get_ino(path_orig)
    events.append(GTEvent(seq=seq, event_type='CREATE', ino=ino, parent_ino=l3_ino, name='target.txt'))
    seq += 1

    path_mid = f'{l1}/moved.txt'
    os.rename(path_orig, path_mid)
    fsync_path(path_mid)
    events.append(GTEvent(seq=seq, event_type='RENAME', ino=ino, parent_ino=l3_ino,
                          name='target.txt', new_name='moved.txt', new_parent=l1_ino))
    seq += 1

    path_root = f'{mnt}/moved.txt'
    os.rename(path_mid, path_root)
    fsync_path(path_root)
    events.append(GTEvent(seq=seq, event_type='RENAME', ino=ino, parent_ino=l1_ino,
                          name='moved.txt', new_name='moved.txt', new_parent=mnt_ino))
    seq += 1

    return events


SCENARIOS = {
    'S1': ('normal_workload', scenario_S1_normal),
    'S2': ('crash_before_commit', scenario_S2_crash),
    'S3': ('antiforensic_burst', scenario_S3_antiforensic),
    'S4': ('shortlived_files', scenario_S4_short_lived),
    'S5': ('deep_rename_tree', scenario_S5_deep_rename),
}


def generate_scenario(scenario_id: str, output_dir: Path, gt_dir: Path) -> None:
    name, workload_fn = SCENARIOS[scenario_id]
    loop_path = output_dir / f'{scenario_id}_{name}_loop.img'
    img_path = output_dir / f'{scenario_id}_{name}.img'
    gt_path = gt_dir / f'{scenario_id}_{name}_gt.json'

    with tempfile.TemporaryDirectory(prefix='fctrace_mnt_') as mnt:
        loop = LoopImage(str(loop_path), mnt)
        loop.create()
        loop.mount()

        try:
            logger.info('Running warm-up for %s', name)
            warmup_write(mnt)
            logger.info('Running workload: %s', name)
            events = workload_fn(mnt)
            loop.snapshot(str(img_path))
        finally:
            loop.cleanup()

    with open(gt_path, 'w') as fh:
        json.dump([asdict(ev) for ev in events], fh, indent=2)

    logger.info(
        'Scenario %s complete: image=%s  gt=%s  events=%d',
        scenario_id, img_path.name, gt_path.name, len(events),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description='FC-Trace dataset generator')
    parser.add_argument('--output-dir', default='./data/raw_images',
                        help='Directory to write generated *.img files')
    parser.add_argument('--gt-dir', default=None,
                        help='Directory to write *_gt.json files (default: same as --output-dir)')
    parser.add_argument('--scenarios', default='S1,S2,S3,S4,S5',
                        help='Comma-separated scenario IDs to generate')
    args = parser.parse_args()

    preflight_requirements()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    gt_dir = Path(args.gt_dir) if args.gt_dir else output_dir
    gt_dir.mkdir(parents=True, exist_ok=True)

    ids = [s.strip() for s in args.scenarios.split(',')]
    for sid in ids:
        if sid not in SCENARIOS:
            logger.error('Unknown scenario ID: %s. Valid: %s', sid, list(SCENARIOS.keys()))
            continue
        generate_scenario(sid, output_dir, gt_dir)


if __name__ == '__main__':
    main()
