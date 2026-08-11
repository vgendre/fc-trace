#!/usr/bin/env python3
"""
exp_invalidation.py — what actually destroys fast-commit evidence?
===================================================================
This harness separates candidate causes of fast-commit evidence loss and reports how many scripted CREATE events survive each condition.

Conditions
----------
  baseline        snapshot immediately, no interference
  sync            sync(1), then snapshot
  sync_wait       sync(1), then wait past the JBD2 commit interval
  idle_wait       no sync, just idle past the commit interval
  drop_caches     sync + echo 3 > /proc/sys/vm/drop_caches
  remount         mount -o remount (forces a commit, keeps it mounted)
  clean_umount    umount cleanly, then snapshot the backing FILE
  churn           64 further fsync operations (circular buffer wrap)

The point of separating them is that "a full commit happened" and "the
fast-commit area was overwritten" are different events, and conflating them
produces a limitation statement that does not hold.

Requires root. Example::

    sudo python3 scripts/exp_invalidation.py \\
        --output /tmp/fc-trace-invalidation.json
"""

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
from fctrace.parser.tlv_decoder import TLVDecoder
from fctrace.parser.fc_tags import FCTag

logging.disable(logging.CRITICAL)

IMAGE_MB = 256
N_FILES = 12
JBD2_COMMIT_INTERVAL = 5


def sh(cmd, check=True):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f'{cmd}\n{r.stderr}')
    return r


def ensure_loops(n=8):
    subprocess.run('modprobe loop', shell=True, capture_output=True)
    for i in range(n):
        if not os.path.exists(f'/dev/loop{i}'):
            subprocess.run(f'mknod /dev/loop{i} b 7 {i}', shell=True,
                           capture_output=True)


def analyse(path):
    try:
        with Ext4Image(str(path)) as im:
            jr = JournalReader(im)
            jr.open()
            raw = jr.read_fc_area()
            bs = jr.jbd2_sb.block_size or im.block_size
        dec = TLVDecoder(raw, block_size=bs)
        recs = dec.decode()
    except Exception as exc:
        return {'error': str(exc), 'creates': 0, 'names': [],
                'heads': 0, 'tails': 0, 'crc_ok': 0}
    creates = [r.payload.name for r in recs
               if r.tag == FCTag.CREAT and r.payload and not r.decode_error]
    return {
        'records': len(recs),
        'creates': len(creates),
        'names': sorted(creates)[:4],
        'heads': sum(1 for r in recs if r.tag == FCTag.HEAD),
        'tails': sum(1 for r in recs if r.tag == FCTag.TAIL),
        'crc_ok': dec.crc_checked - dec.crc_failures,
        'crc_bad': dec.crc_failures,
    }


def run_condition(name, workdir):
    """Build a fresh volume, run the workload, apply one condition, snapshot."""
    img = workdir / f'inv_{name}.img'
    snap = workdir / f'inv_{name}_snap.img'
    for p in (img, snap):
        if p.exists():
            p.unlink()
    sh(f'dd if=/dev/zero of={img} bs=1M count={IMAGE_MB} status=none')
    sh(f'mkfs.ext4 -O fast_commit -b 4096 -F -J size=16 {img} -q')

    loop = sh(f'losetup -fP --show {img}').stdout.strip()
    mnt = tempfile.mkdtemp(prefix=f'inv_{name}_')
    sh(f'mount {loop} {mnt}')
    snapshot_taken = False
    try:
        fd = os.open(f'{mnt}/.warm', os.O_WRONLY | os.O_CREAT, 0o644)
        os.write(fd, b'w' * 512)
        os.fsync(fd)
        os.close(fd)
        time.sleep(JBD2_COMMIT_INTERVAL + 1)

        for i in range(N_FILES):
            p = f'{mnt}/ev_{i:02d}.dat'
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_SYNC, 0o644)
            os.write(fd, b'E' * 2048)
            os.close(fd)

        # ---- apply the condition under test -------------------------------
        if name == 'baseline':
            pass
        elif name == 'sync':
            sh('sync')
        elif name == 'sync_wait':
            sh('sync')
            time.sleep(JBD2_COMMIT_INTERVAL * 3)
        elif name == 'idle_wait':
            time.sleep(JBD2_COMMIT_INTERVAL * 3)
        elif name == 'drop_caches':
            sh('sync')
            sh('echo 3 > /proc/sys/vm/drop_caches', check=False)
        elif name == 'remount':
            sh(f'mount -o remount {mnt}', check=False)
            time.sleep(2)
        elif name == 'clean_umount':
            sh(f'umount {mnt}')
            sh(f'losetup -d {loop}', check=False)
            # The filesystem is gone; the backing file IS the evidence now.
            sh(f'cp {img} {snap}')
            snapshot_taken = True
        elif name == 'churn':
            for i in range(64):
                p = f'{mnt}/churn_{i:03d}.tmp'
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_SYNC, 0o644)
                os.write(fd, b'C' * 256)
                os.close(fd)

        if not snapshot_taken:
            sh(f'blockdev --flushbufs {loop}')
            sh(f'dd if={loop} of={snap} bs=4096 status=none')
    finally:
        if not snapshot_taken:
            sh(f'umount -l {mnt}', check=False)
            sh(f'losetup -d {loop}', check=False)

    r = analyse(snap)
    r['condition'] = name
    img.unlink(missing_ok=True)
    snap.unlink(missing_ok=True)
    return r


CONDITIONS = ['baseline', 'sync', 'sync_wait', 'idle_wait',
              'drop_caches', 'remount', 'clean_umount', 'churn']


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--workdir', default='/tmp/fctrace_inv')
    ap.add_argument('--output', default='/tmp/fc-trace-invalidation.json')
    ap.add_argument('--only', default=None)
    args = ap.parse_args()

    if os.geteuid() != 0:
        print('must run as root', file=sys.stderr)
        return 1

    ensure_loops()
    wd = Path(args.workdir)
    wd.mkdir(parents=True, exist_ok=True)

    print(f'kernel: {os.uname().release}')
    print(f'workload: {N_FILES} files with O_SYNC, then the condition\n')
    hdr = (f'{"condition":<15} {"CREATEs":>8} {"of":>3} {"records":>8} '
           f'{"HEAD":>5} {"TAIL":>5} {"crc ok":>7}  verdict')
    print(hdr)
    print('-' * len(hdr))

    rows = []
    for c in ([x.strip() for x in args.only.split(',')]
              if args.only else CONDITIONS):
        try:
            r = run_condition(c, wd)
        except Exception as exc:
            print(f'{c:<15} FAILED: {exc}')
            continue
        rows.append(r)
        lost = N_FILES - r['creates']
        verdict = ('evidence intact' if lost == 0
                   else f'LOST {lost}/{N_FILES}')
        print(f'{c:<15} {r["creates"]:>8} {N_FILES:>3} {r.get("records", 0):>8} '
              f'{r.get("heads", 0):>5} {r.get("tails", 0):>5} '
              f'{r.get("crc_ok", 0):>7}  {verdict}', flush=True)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2))
    print(f'\nResults written: {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
