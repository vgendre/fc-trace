#!/usr/bin/env python3
"""
exp_workloads.py — fast-commit evidence under realistic storage workloads
=========================================================================
The five scripted scenarios are controlled metadata workloads; this harness instead runs standard, citable workloads and reports what the fast-commit area actually retains from each.

Workloads
---------
  fio-fsync1     random writes with fsync after every write (fio, Axboe)
  fio-fsync16    same with fsync every 16 writes -- sweeps commit frequency
  fio-fsync256   same with fsync every 256 writes
  smallfiles     many small file creations, Postmark-like churn
  tarball        extract an archive, an ordinary bulk-create workload
  sqlite         DB inserts with synchronous=FULL, application fsync pattern

Unlike the S1-S5 scenarios there is no hand-written ground truth here. What
is reported instead is what an examiner would actually obtain: how many files
the workload created, how many are recoverable from the fast-commit area, the
event-type distribution, and how much of the area the workload consumed.

Requires root. Example::

    sudo python3 scripts/exp_workloads.py --output /tmp/fc-trace-workloads.json
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from fctrace.io.image_reader import Ext4Image
from fctrace.io.journal_reader import JournalReader
from fctrace.parser.tlv_decoder import TLVDecoder
from fctrace.parser.fc_tags import FCTag
from fctrace.reconstruct.event_builder import EventBuilder

logging.disable(logging.CRITICAL)

IMAGE_MB = 512


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


# ------------------------------------------------------------- workloads
def wl_fio(mnt, fsync_n):
    if not shutil.which('fio'):
        return None
    sh(f'fio --name=w --directory={mnt} --rw=randwrite --bs=4k '
       f'--size=8M --numjobs=2 --fsync={fsync_n} --runtime=20 --time_based '
       f'--group_reporting --minimal', check=False)
    return f'fio randwrite, fsync every {fsync_n} write(s), 2 jobs, 20 s'


def wl_smallfiles(mnt, n=150):
    for i in range(n):
        p = f'{mnt}/sf_{i:04d}.dat'
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_SYNC, 0o644)
        os.write(fd, os.urandom(512))
        os.close(fd)
    return f'{n} small files created with O_SYNC (Postmark-like churn)'


def wl_tarball(mnt):
    src = Path(mnt) / 'tree'
    src.mkdir(exist_ok=True)
    for d in range(8):
        sub = src / f'dir{d}'
        sub.mkdir(exist_ok=True)
        for f in range(12):
            (sub / f'f{f}.txt').write_text('x' * 400)
    tar = f'{mnt}/bundle.tar'
    sh(f'tar cf {tar} -C {mnt} tree')
    sh(f'rm -rf {src}')
    sh(f'tar xf {tar} -C {mnt}')
    fd = os.open(mnt, os.O_RDONLY | os.O_DIRECTORY)
    os.fsync(fd)
    os.close(fd)
    return 'tar create + extract of 96 files across 8 directories'


def wl_sqlite(mnt, rows=400):
    import sqlite3
    db = f'{mnt}/app.db'
    c = sqlite3.connect(db)
    c.execute('PRAGMA synchronous=FULL')
    c.execute('CREATE TABLE t(a INTEGER, b TEXT)')
    for i in range(rows):
        c.execute('INSERT INTO t VALUES(?,?)', (i, 'x' * 80))
        c.commit()
    c.close()
    return f'SQLite {rows} inserts, synchronous=FULL'


WORKLOADS = {
    'fio-fsync1':   lambda m: wl_fio(m, 1),
    'fio-fsync16':  lambda m: wl_fio(m, 16),
    'fio-fsync256': lambda m: wl_fio(m, 256),
    'smallfiles':   wl_smallfiles,
    'tarball':      wl_tarball,
    'sqlite':       wl_sqlite,
}


def run_workload(name, fn, workdir):
    img = workdir / 'wl.img'
    snap = workdir / 'wl_snap.img'
    for p in (img, snap):
        if p.exists():
            p.unlink()
    sh(f'dd if=/dev/zero of={img} bs=1M count={IMAGE_MB} status=none')
    sh(f'mkfs.ext4 -O fast_commit -b 4096 -F -J size=64 {img} -q')

    loop = sh(f'losetup -fP --show {img}').stdout.strip()
    mnt = tempfile.mkdtemp(prefix=f'wl_{name}_')
    sh(f'mount {loop} {mnt}')
    try:
        fd = os.open(f'{mnt}/.warm', os.O_WRONLY | os.O_CREAT, 0o644)
        os.write(fd, b'w' * 512)
        os.fsync(fd)
        os.close(fd)
        time.sleep(6)

        t0 = time.perf_counter()
        desc = fn(mnt)
        if desc is None:
            return None
        wl_time = time.perf_counter() - t0

        files_on_disk = sum(len(f) for _, _, f in os.walk(mnt))
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
    dec = TLVDecoder(raw, block_size=bs)
    recs = dec.decode()
    evs = [e.to_dict() for e in
           EventBuilder([r for r in recs if r.tag != FCTag.PAD]).build()]

    tags = Counter(r.tag.name for r in recs)
    etypes = Counter(e['event_type'] for e in evs)
    # Fraction of the area holding real records rather than trailing zeroes.
    last = max((r.offset for r in recs), default=0)
    used = (last / len(raw) * 100) if raw else 0.0

    return {
        'workload': name,
        'description': desc,
        'workload_seconds': round(wl_time, 1),
        'files_on_disk': files_on_disk,
        'num_fc_blks': num_fc,
        'fc_area_kib': len(raw) // 1024,
        'fc_utilisation_pct': round(used, 1),
        'records': len(recs),
        'tags': dict(tags),
        'events': len(evs),
        'event_types': dict(etypes),
        'transactions': len({r.tid for r in recs if r.tid > 0}),
        'crc_ok': dec.crc_checked - dec.crc_failures,
        'crc_bad': dec.crc_failures,
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--workdir', default='/tmp/fctrace_wl')
    ap.add_argument('--output', default='/tmp/fc-trace-workloads.json')
    ap.add_argument('--only', default=None, help='comma-separated workload names')
    args = ap.parse_args()

    if os.geteuid() != 0:
        print('must run as root', file=sys.stderr)
        return 1

    ensure_loops()
    wd = Path(args.workdir)
    wd.mkdir(parents=True, exist_ok=True)

    names = ([n.strip() for n in args.only.split(',')]
             if args.only else list(WORKLOADS))
    print(f'kernel: {os.uname().release}')
    print(f'fio: {"available" if shutil.which("fio") else "NOT INSTALLED"}\n')

    hdr = (f'{"workload":<14} {"files":>6} {"recs":>6} {"events":>7} {"tx":>4} '
           f'{"CREATE":>7} {"UNLINK":>7} {"crc ok":>7} {"FC use%":>8}')
    print(hdr)
    print('-' * len(hdr))

    rows = []
    for n in names:
        fn = WORKLOADS.get(n)
        if fn is None:
            print(f'{n}: unknown workload')
            continue
        try:
            r = run_workload(n, fn, wd)
        except Exception as exc:
            print(f'{n:<14} FAILED: {exc}')
            continue
        if r is None:
            print(f'{n:<14} skipped (fio not installed)')
            continue
        rows.append(r)
        et = r['event_types']
        print(f'{n:<14} {r["files_on_disk"]:>6} {r["records"]:>6} '
              f'{r["events"]:>7} {r["transactions"]:>4} '
              f'{et.get("CREATE", 0):>7} {et.get("UNLINK", 0):>7} '
              f'{r["crc_ok"]:>7} {r["fc_utilisation_pct"]:>7.1f}%', flush=True)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2))
    print(f'\nResults written: {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
