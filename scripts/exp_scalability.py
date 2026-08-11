#!/usr/bin/env python3
"""
exp_scalability.py — computational overhead and scalability
================================================================================
Measures how FC-Trace's cost behaves as the forensic image grows.

The claim under test: FC-Trace performs a constant number of seeks
(superblock -> group descriptor -> journal inode -> fast-commit area) and then
reads only the fast-commit blocks, whose count is fixed by s_num_fc_blks.
Its cost is therefore bounded by the evidence source, not by volume size.
A tool that must walk the inode table or directory tree does not have this
property.

Reports, per image size: wall-clock, peak RSS, and bytes actually read from
the block device -- the last being the least ambiguous evidence for the
O(1) claim.

Uses sparse files, so a 512 GiB image costs almost no real disk. Requires
root only if you ask for `fls` comparison on images root created.

Example::

    sudo python3 scripts/exp_scalability.py \\
        --sizes 512M,2G,8G,32G,128G,512G \\
        --output /tmp/fc-trace-scalability.json
"""

import argparse
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from fctrace.io.image_reader import Ext4Image
from fctrace.io.journal_reader import JournalReader
from fctrace.parser.tlv_decoder import decode_fc_buffer
from fctrace.reconstruct.event_builder import EventBuilder

import logging
logging.disable(logging.CRITICAL)


def sh(cmd, check=True):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f'{cmd}\n{r.stderr}')
    return r


def read_bytes_now() -> int:
    """Bytes this process has actually fetched from block devices."""
    try:
        for line in open('/proc/self/io'):
            if line.startswith('read_bytes:'):
                return int(line.split()[1])
    except OSError:
        pass
    return -1


def peak_rss_kib() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def make_sparse_image(path: Path, size: str) -> None:
    if path.exists():
        path.unlink()
    # truncate creates a sparse file; dd would materialise every byte.
    sh(f'truncate -s {size} {path}')
    # Lazy init keeps mkfs from writing the whole inode table up front.
    sh(f'mkfs.ext4 -O fast_commit -b 4096 -F -J size=64 '
       f'-E lazy_itable_init=1,lazy_journal_init=1 {path} -q')


def run_fctrace(path: Path) -> dict:
    io_before = read_bytes_now()
    t0 = time.perf_counter()
    with Ext4Image(str(path)) as img:
        jr = JournalReader(img)
        jr.open()
        raw = jr.read_fc_area()
        bs = jr.jbd2_sb.block_size or img.block_size
        num_fc = jr.jbd2_sb.num_fc_blks
    events = [e.to_dict() for e in
              EventBuilder(decode_fc_buffer(raw, block_size=bs)).build()]
    elapsed = time.perf_counter() - t0
    io_after = read_bytes_now()
    return {
        'wall_ms': elapsed * 1000,
        'peak_rss_kib': peak_rss_kib(),
        'read_bytes': (io_after - io_before) if io_before >= 0 else -1,
        'fc_area_bytes': len(raw),
        'num_fc_blks': num_fc,
        'events': len(events),
    }


def run_fls(path: Path) -> dict:
    """The Sleuth Kit walks the directory tree; used as the contrast case."""
    if not sh('which fls', check=False).stdout.strip():
        return {}
    t0 = time.perf_counter()
    r = subprocess.run(['/usr/bin/time', '-v', 'fls', '-r', str(path)],
                       capture_output=True, text=True)
    elapsed = time.perf_counter() - t0
    rss = 0
    for line in r.stderr.splitlines():
        if 'Maximum resident set size' in line:
            rss = int(line.split(':')[1].strip())
    return {'wall_ms': elapsed * 1000, 'peak_rss_kib': rss,
            'lines': len(r.stdout.splitlines())}


def parse_size(s: str) -> int:
    units = {'K': 1024, 'M': 1024 ** 2, 'G': 1024 ** 3, 'T': 1024 ** 4}
    s = s.strip().upper()
    if s[-1] in units:
        return int(float(s[:-1]) * units[s[-1]])
    return int(s)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--sizes', default='512M,2G,8G,32G,128G',
                    help='comma-separated image sizes')
    ap.add_argument('--repeat', type=int, default=5,
                    help='timed repetitions per size')
    ap.add_argument('--workdir', default='/tmp/fctrace_scale')
    ap.add_argument('--output', default='/tmp/fc-trace-scalability.json')
    ap.add_argument('--no-fls', action='store_true',
                    help='skip the Sleuth Kit contrast run')
    args = ap.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    print(f'kernel: {os.uname().release}')
    print(f'repetitions per size: {args.repeat}\n')

    hdr = (f'{"image":>7} {"num_fc":>7} {"FC KiB":>7} | '
           f'{"FC-Trace ms":>12} {"RSS KiB":>8} {"read KiB":>9} | '
           f'{"fls ms":>9} {"fls RSS":>8}')
    print(hdr)
    print('-' * len(hdr))

    rows = []
    for size in [s.strip() for s in args.sizes.split(',')]:
        img = workdir / f'scale_{size}.img'
        make_sparse_image(img, size)

        # Warm one run, then take the median of `repeat` timings.
        run_fctrace(img)
        samples = [run_fctrace(img) for _ in range(args.repeat)]
        samples.sort(key=lambda d: d['wall_ms'])
        fc = samples[len(samples) // 2]

        fl = {} if args.no_fls else run_fls(img)

        row = {'size': size, 'size_bytes': parse_size(size),
               'fctrace': fc, 'fls': fl}
        rows.append(row)

        print(f'{size:>7} {fc["num_fc_blks"]:>7} {fc["fc_area_bytes"]//1024:>7} | '
              f'{fc["wall_ms"]:>12.2f} {fc["peak_rss_kib"]:>8} '
              f'{fc["read_bytes"]//1024 if fc["read_bytes"]>=0 else -1:>9} | '
              f'{fl.get("wall_ms", 0):>9.1f} {fl.get("peak_rss_kib", 0):>8}',
              flush=True)

        img.unlink(missing_ok=True)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2))
    print(f'\nResults written: {out}')

    # The headline: does cost track image size?
    if len(rows) >= 2:
        first, last = rows[0], rows[-1]
        growth = last['size_bytes'] / first['size_bytes']
        t_ratio = last['fctrace']['wall_ms'] / max(first['fctrace']['wall_ms'], 1e-9)
        print(f'\nImage size grew {growth:.0f}x '
              f'({first["size"]} -> {last["size"]}).')
        print(f'FC-Trace wall-clock changed {t_ratio:.2f}x.')
        if first['fctrace']['read_bytes'] >= 0:
            print(f'Bytes read: {first["fctrace"]["read_bytes"]} -> '
                  f'{last["fctrace"]["read_bytes"]}')
        if rows[0].get('fls') and rows[-1]['fls']:
            f_ratio = (last['fls']['wall_ms'] /
                       max(first['fls']['wall_ms'], 1e-9))
            print(f'fls wall-clock changed {f_ratio:.2f}x over the same range.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
