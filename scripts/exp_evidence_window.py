#!/usr/bin/env python3
"""
exp_evidence_window.py — fast-commit evidence-window characterisation
======================================================================
Measures how much file-system activity the fast-commit area retains before
its circular buffer wraps and the earliest evidence is destroyed.

This is the axis along which FC-Trace's recall actually varies. Repeating an
identical scripted workload yields zero variance, because every operation is
forced with O_SYNC and the resulting fast-commit content is deterministic.
What moves recall is workload *intensity* relative to fast-commit capacity.

Two sweeps:

  --mode capacity   journal size x operation count. Reports how many scripted
                    CREATE operations remain recoverable, and the index of the
                    oldest surviving one (which exposes FIFO eviction).

  --mode fcsize     s_num_fc_blks as a function of -J fast_commit_size, then
                    a fixed workload against several settings to show that
                    enlarging the area restores full recall.

Requires root (losetup, mount) and a Linux host with ext4 fast-commit support.

Example::

    sudo python3 scripts/exp_evidence_window.py --mode capacity \\
        --output results/measured/evidence_window.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from fctrace.io.image_reader import Ext4Image
from fctrace.io.journal_reader import JournalReader
from fctrace.parser.tlv_decoder import decode_fc_buffer
from fctrace.reconstruct.event_builder import EventBuilder

logging.disable(logging.CRITICAL)

IMAGE_SIZE_MB = 512
JBD2_COMMIT_WAIT_S = 6


def sh(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f'{cmd}\n{r.stderr}')
    return r


def ensure_loop_nodes(count: int = 8) -> None:
    """Some environments (notably WSL2) pre-create only /dev/loop0."""
    subprocess.run('modprobe loop', shell=True, capture_output=True)
    for i in range(count):
        if not os.path.exists(f'/dev/loop{i}'):
            subprocess.run(f'mknod /dev/loop{i} b 7 {i}',
                           shell=True, capture_output=True)


def make_image(workdir: Path, journal_mb: int,
               fast_commit_kb: int | None = None) -> Path:
    img = workdir / 'window.img'
    if img.exists():
        img.unlink()
    sh(f'dd if=/dev/zero of={img} bs=1M count={IMAGE_SIZE_MB} status=none')
    jopt = f'size={journal_mb}'
    if fast_commit_kb:
        jopt += f',fast_commit_size={fast_commit_kb}'
    sh(f'mkfs.ext4 -O fast_commit -b 4096 -F -J {jopt} {img} -q')
    return img


def probe_journal(img: Path) -> tuple:
    """Return (num_fc_blks, max_len, block_size) from the JBD2 superblock."""
    with Ext4Image(str(img)) as im:
        jr = JournalReader(im)
        jr.open()
        return jr.jbd2_sb.num_fc_blks, jr.jbd2_sb.max_len, im.block_size


def run_workload(img: Path, workdir: Path, n_ops: int) -> dict:
    """Create n_ops files with O_SYNC, snapshot, then run FC-Trace."""
    snap = workdir / 'window_snap.img'
    loop = sh(f'losetup -fP --show {img}').stdout.strip()
    mnt = tempfile.mkdtemp(prefix='fcwin_')
    sh(f'mount {loop} {mnt}')
    try:
        # Absorb the first-mount full JBD2 commit before the real workload.
        fd = os.open(f'{mnt}/.warmup', os.O_WRONLY | os.O_CREAT, 0o644)
        os.write(fd, b'w' * 512)
        os.fsync(fd)
        os.close(fd)
        time.sleep(JBD2_COMMIT_WAIT_S)

        expected = []
        for i in range(n_ops):
            name = f'ev_{i:05d}.bin'
            path = f'{mnt}/{name}'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_SYNC, 0o644)
            os.write(fd, b'x' * 256)
            os.close(fd)
            expected.append((os.stat(path).st_ino, name))

        # Snapshot while still mounted, which models live acquisition.
        # Measured across kernels 5.10-6.18: records also survive a clean
        # unmount, so this ordering is conservative rather than required.
        sh(f'blockdev --flushbufs {loop}')
        sh(f'dd if={loop} of={snap} bs=4096 status=none')
    finally:
        sh(f'umount -l {mnt}', check=False)
        sh(f'losetup -d {loop}', check=False)

    with Ext4Image(str(snap)) as im:
        jr = JournalReader(im)
        jr.open()
        raw = jr.read_fc_area()
        bs = jr.jbd2_sb.block_size or im.block_size
        num_fc = jr.jbd2_sb.num_fc_blks

    events = [e.to_dict() for e in
              EventBuilder(decode_fc_buffer(raw, block_size=bs)).build()]
    recovered_set = {(e['ino'], e['name'])
                     for e in events if e['event_type'] == 'CREATE'}
    recovered = sum(1 for pair in expected if pair in recovered_set)
    oldest = next((i for i, p in enumerate(expected)
                   if p in recovered_set), None)

    return {
        'num_fc_blks': num_fc,
        'fc_area_kib': len(raw) // 1024,
        'ops': n_ops,
        'recovered': recovered,
        'recall': recovered / n_ops if n_ops else 0.0,
        'oldest_surviving_index': oldest,
    }


def mode_capacity(workdir: Path, journals, op_counts) -> list:
    rows = []
    hdr = (f'{"journal":>8} {"fc_blks":>8} {"fc_KiB":>7} {"ops":>6} '
           f'{"recovered":>10} {"recall":>7} {"oldest_kept":>12}')
    print(hdr)
    print('-' * len(hdr))
    for jmb in journals:
        for n in op_counts:
            img = make_image(workdir, jmb)
            r = run_workload(img, workdir, n)
            r['journal_mb'] = jmb
            rows.append(r)
            oldest = ('-' if r['oldest_surviving_index'] is None
                      else str(r['oldest_surviving_index']))
            print(f'{jmb:>7}M {r["num_fc_blks"]:>8} {r["fc_area_kib"]:>7} '
                  f'{n:>6} {r["recovered"]:>10} {r["recall"]:>7.3f} '
                  f'{oldest:>12}', flush=True)
    return rows


def mode_fcsize(workdir: Path, n_ops: int) -> list:
    print('s_num_fc_blks as configured')
    hdr = (f'{"-J size":>8} {"fast_commit_size":>17} {"max_len":>8} '
           f'{"num_fc_blks":>12} {"FC area KiB":>12}')
    print(hdr)
    print('-' * len(hdr))
    configs = [(16, None), (32, None), (64, None), (128, None),
               (64, 256), (64, 1024), (64, 4096), (64, 16384)]
    rows = []
    for jmb, fckb in configs:
        img = make_image(workdir, jmb, fckb)
        nfc, maxlen, bs = probe_journal(img)
        rows.append({'journal_mb': jmb, 'fast_commit_kb': fckb,
                     'max_len': maxlen, 'num_fc_blks': nfc,
                     'fc_area_kib': nfc * bs // 1024})
        print(f'{jmb:>7}M {str(fckb or "default"):>17} {maxlen:>8} '
              f'{nfc:>12} {nfc * bs // 1024:>12}', flush=True)

    print(f'\n{n_ops} fsync operations, how many CREATEs survive?')
    hdr2 = (f'{"-J size":>8} {"fast_commit_size":>17} {"num_fc_blks":>12} '
            f'{"recovered":>10} {"recall":>7}')
    print(hdr2)
    print('-' * len(hdr2))
    for jmb, fckb in [(64, None), (64, 1024), (64, 16384)]:
        img = make_image(workdir, jmb, fckb)
        r = run_workload(img, workdir, n_ops)
        r['journal_mb'] = jmb
        r['fast_commit_kb'] = fckb
        rows.append(r)
        print(f'{jmb:>7}M {str(fckb or "default"):>17} {r["num_fc_blks"]:>12} '
              f'{r["recovered"]:>10} {r["recall"]:>7.3f}', flush=True)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--mode', choices=['capacity', 'fcsize'], default='capacity')
    ap.add_argument('--journals', default='16,64',
                    help='comma-separated journal sizes in MiB (capacity mode)')
    ap.add_argument('--ops', default='10,25,50,100,200,400',
                    help='comma-separated operation counts (capacity mode)')
    ap.add_argument('--fcsize-ops', type=int, default=200,
                    help='operation count for fcsize mode')
    ap.add_argument('--workdir', default='/tmp/fctrace_window')
    ap.add_argument('--output', default='results/measured/evidence_window.json')
    args = ap.parse_args()

    if os.geteuid() != 0:
        print('This script must be run as root (needs losetup + mount).',
              file=sys.stderr)
        return 1

    ensure_loop_nodes()
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    print(f'kernel: {os.uname().release}')
    print(f'image:  {IMAGE_SIZE_MB} MiB, 4 KiB blocks\n')

    if args.mode == 'capacity':
        rows = mode_capacity(
            workdir,
            [int(x) for x in args.journals.split(',')],
            [int(x) for x in args.ops.split(',')],
        )
    else:
        rows = mode_fcsize(workdir, args.fcsize_ops)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2))
    print(f'\nResults written: {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
