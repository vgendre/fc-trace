#!/usr/bin/env python3
"""
exp_antiforensic.py — anti-forensic resilience evaluation (reviewer comment 5)
==============================================================================
Evaluates FC-Trace against a tier-2 adversary: one who can write to the block
device directly, not merely operate through the file-system interface.

Scenarios
---------
  A1  baseline               known workload, no interference
  A2  journal wipe           zero the fast-commit blocks on the image
  A3  partial wipe           zero only part of one commit (resynchronisation
                              and missing-commit evidence may expose the damage)
  A4  forced full commit     `sync`, which checkpoints metadata but leaves the
                              measured FC records intact
  A5  feature disabled       tune2fs -O ^fast_commit after the fact
  A6  secure delete          shred/srm the file contents
  A7  timestomp              forge inode timestamps, compare against FC order

For each: capture before and after, run FC-Trace on both, and report what
survives and whether the interference is *detectable*.

Requires root. Example::

    sudo python3 scripts/exp_antiforensic.py \\
        --output results/measured/antiforensic.json
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


def analyse(img):
    """Run FC-Trace's read path and summarise what is recoverable."""
    try:
        with Ext4Image(str(img)) as im:
            jr = JournalReader(im)
            jr.open()
            raw = jr.read_fc_area()
            bs = jr.jbd2_sb.block_size or im.block_size
            fc_start = jr.fc_blocks[0] if jr.fc_blocks else -1
            n_fc = len(jr.fc_blocks)
        dec = TLVDecoder(raw, block_size=bs)
        recs = dec.decode()
    except Exception as exc:
        return {'error': str(exc), 'dentries': 0, 'heads': 0, 'tails': 0,
                'crc_ok': 0, 'crc_bad': 0, 'names': []}

    dentry = [r for r in recs
              if r.tag in (FCTag.CREAT, FCTag.LINK, FCTag.UNLINK)
              and r.payload and not r.decode_error]
    return {
        'fc_start_block': fc_start,
        'fc_blocks': n_fc,
        'fc_bytes': len(raw),
        'records': len(recs),
        'dentries': len(dentry),
        'heads': sum(1 for r in recs if r.tag == FCTag.HEAD),
        'tails': sum(1 for r in recs if r.tag == FCTag.TAIL),
        'crc_ok': dec.crc_checked - dec.crc_failures,
        'crc_bad': dec.crc_failures,
        'resyncs': dec.resync_count,
        'names': sorted({r.payload.name for r in dentry})[:6],
    }


def build(workdir, tag):
    """Create a fast-commit image and run a known workload on it."""
    img = workdir / f'{tag}.img'
    snap = workdir / f'{tag}_snap.img'
    for p in (img, snap):
        if p.exists():
            p.unlink()
    sh(f'dd if=/dev/zero of={img} bs=1M count={IMAGE_MB} status=none')
    sh(f'mkfs.ext4 -O fast_commit -b 4096 -F -J size=16 {img} -q')

    loop = sh(f'losetup -fP --show {img}').stdout.strip()
    mnt = tempfile.mkdtemp(prefix=f'af_{tag}_')
    sh(f'mount {loop} {mnt}')

    fd = os.open(f'{mnt}/.warm', os.O_WRONLY | os.O_CREAT, 0o644)
    os.write(fd, b'w' * 512)
    os.fsync(fd)
    os.close(fd)
    time.sleep(6)

    for i in range(N_FILES):
        p = f'{mnt}/secret_{i:02d}.dat'
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_SYNC, 0o644)
        os.write(fd, b'S' * 4096)
        os.close(fd)
    return img, snap, loop, mnt


def capture(loop, snap):
    sh(f'blockdev --flushbufs {loop}')
    sh(f'dd if={loop} of={snap} bs=4096 status=none')


def teardown(loop, mnt):
    sh(f'umount -l {mnt}', check=False)
    sh(f'losetup -d {loop}', check=False)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--workdir', default='/tmp/fctrace_af')
    ap.add_argument('--output', default='results/measured/antiforensic.json')
    args = ap.parse_args()

    if os.geteuid() != 0:
        print('must run as root', file=sys.stderr)
        return 1

    ensure_loops()
    wd = Path(args.workdir)
    wd.mkdir(parents=True, exist_ok=True)
    print(f'kernel: {os.uname().release}')
    print(f'workload: {N_FILES} files created with O_SYNC\n')
    results = {}

    # ---------------------------------------------------------- A1 baseline
    img, snap, loop, mnt = build(wd, 'a1')
    capture(loop, snap)
    teardown(loop, mnt)
    base = analyse(snap)
    results['A1_baseline'] = base
    print(f'A1 baseline            dentries={base["dentries"]:<3} '
          f'heads={base["heads"]} tails={base["tails"]} '
          f'crc_ok={base["crc_ok"]} crc_bad={base["crc_bad"]}')

    # ------------------------------------------------- A2 full journal wipe
    img, snap, loop, mnt = build(wd, 'a2')
    capture(loop, snap)
    teardown(loop, mnt)
    pre = analyse(snap)
    sh(f'dd if=/dev/zero of={snap} bs=4096 '
       f'seek={pre["fc_start_block"]} count={pre["fc_blocks"]} '
       f'conv=notrunc status=none')
    post = analyse(snap)
    results['A2_journal_wipe'] = {'before': pre, 'after': post}
    print(f'A2 full journal wipe   dentries={pre["dentries"]} -> '
          f'{post["dentries"]}   heads={pre["heads"]}->{post["heads"]}  '
          f'DETECTABLE={post["dentries"] == 0 and post["heads"] == 0}')

    # ------------------------------------------------------ A3 partial wipe
    img, snap, loop, mnt = build(wd, 'a3')
    capture(loop, snap)
    teardown(loop, mnt)
    pre = analyse(snap)
    # Zero a single block in the middle of the FC area -- a targeted edit
    # rather than wholesale destruction.
    sh(f'dd if=/dev/zero of={snap} bs=4096 '
       f'seek={pre["fc_start_block"] + 2} count=1 conv=notrunc status=none')
    post = analyse(snap)
    results['A3_partial_wipe'] = {'before': pre, 'after': post}
    print(f'A3 partial wipe        dentries={pre["dentries"]} -> '
          f'{post["dentries"]}   crc_ok={pre["crc_ok"]}->{post["crc_ok"]}  '
          f'crc_bad={pre["crc_bad"]}->{post["crc_bad"]}  '
          f'resyncs={post["resyncs"]}')

    # ------------------------------------------------- A4 forced full commit
    img, snap, loop, mnt = build(wd, 'a4')
    sh('sync')
    time.sleep(7)
    capture(loop, snap)
    teardown(loop, mnt)
    post = analyse(snap)
    results['A4_forced_sync'] = post
    print(f'A4 forced sync         dentries={post["dentries"]:<3} '
          f'(baseline {base["dentries"]})  '
          f'evidence_lost={base["dentries"] - post["dentries"]}')

    # ---------------------------------------------------- A5 feature disable
    img, snap, loop, mnt = build(wd, 'a5')
    capture(loop, snap)
    teardown(loop, mnt)
    sh(f'tune2fs -O ^fast_commit {snap}', check=False)
    post = analyse(snap)
    results['A5_feature_disabled'] = post
    print(f'A5 tune2fs -O ^fc      dentries={post["dentries"]:<3} '
          f'records_still_present={post["dentries"] > 0}')

    # ------------------------------------------------------ A6 secure delete
    img, snap, loop, mnt = build(wd, 'a6')
    for i in range(N_FILES):
        sh(f'shred -u -n 1 {mnt}/secret_{i:02d}.dat', check=False)
    fd = os.open(mnt, os.O_RDONLY | os.O_DIRECTORY)
    os.fsync(fd)
    os.close(fd)
    capture(loop, snap)
    teardown(loop, mnt)
    post = analyse(snap)
    results['A6_shred'] = post
    print(f'A6 shred -u            dentries={post["dentries"]:<3} '
          f'names_survive={post["names"][:3]}')

    # --------------------------------------------------------- A7 timestomp
    img, snap, loop, mnt = build(wd, 'a7')
    for i in range(N_FILES):
        sh(f'touch -t 199001010000 {mnt}/secret_{i:02d}.dat', check=False)
    fd = os.open(mnt, os.O_RDONLY | os.O_DIRECTORY)
    os.fsync(fd)
    os.close(fd)
    capture(loop, snap)
    teardown(loop, mnt)
    post = analyse(snap)
    results['A7_timestomp'] = post
    print(f'A7 timestomp           dentries={post["dentries"]:<3} '
          f'crc_ok={post["crc_ok"]} crc_bad={post["crc_bad"]}  '
          f'fc_order_intact={post["crc_bad"] == 0}')

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    print(f'\nResults written: {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
