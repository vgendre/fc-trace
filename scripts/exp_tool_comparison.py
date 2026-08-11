#!/usr/bin/env python3
"""
exp_tool_comparison.py — comparative benchmark (reviewer comment 3)
====================================================================
Runs FC-Trace and established forensic tools against the SAME captured
snapshots, scores them against the SAME ground truth with the SAME metric
implementation (fctrace.compare.diff_engine), and reports a capability probe
alongside the accuracy metrics.

Tools
-----
  FC-Trace              fast-commit TLV records -> reconstructed events
  TSK `fls`             directory entries, allocated and deleted
  TSK `jls`             JBD2 journal block listing
  `debugfs logdump`     journal dump, including its Fast Commit Area section
  plaso / log2timeline  timestamp-based super-timeline      (if installed)
  Autopsy               via one exported CSV per scenario  (--autopsy-csv-dir)

Common event model
------------------
Each tool's native output is mapped onto (event_type, ino, name) triples:

  fls -r      an allocated directory entry means the file was created at some
              point and still exists            -> CREATE
  fls -r -d   a deleted directory entry means the entry was removed
                                                -> UNLINK

This mapping is deliberately generous to `fls`: it credits a CREATE for every
surviving file even though `fls` reports current state rather than an observed
event. The alternative -- crediting nothing, because `fls` observes no events
at all -- would understate a tool practitioners genuinely rely on. Where a tool
structurally cannot produce an event class, the cell is reported as a
capability gap rather than as zero.

Capability probe
----------------
Beyond the metrics, the harness asks one question that separates evidence
sources: can the tool recover the name a file had BEFORE it was renamed?
In scenario S1, `a.txt` was renamed to `b.txt`. The old name no longer exists
anywhere in the file system; it survives only in the fast-commit area.

Example::

    python3 scripts/exp_tool_comparison.py --snap-dir data/raw_images \\
        --gt-dir data/ground_truth --autopsy-csv-dir autopsy_exports \\
        --autopsy-runtime-json autopsy_runtime.json
"""

import argparse
import csv
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from fctrace.io.image_reader import Ext4Image
from fctrace.io.journal_reader import JournalReader
from fctrace.parser.tlv_decoder import decode_fc_buffer
from fctrace.reconstruct.event_builder import EventBuilder
from fctrace.compare.diff_engine import DiffEngine

logging.disable(logging.CRITICAL)

# The pre-rename name from scenario S1's workload.
PRE_RENAME_NAME = 'a.txt'


def _event(etype, ino=0, name='', new_name='', source=''):
    return {'event_type': etype, 'ino': ino, 'name': name,
            'parent_ino': 0, 'new_name': new_name, 'new_parent': 0,
            'source': source}


# ---------------------------------------------------------------- FC-Trace
def run_fctrace(img_path):
    t0 = time.perf_counter()
    with Ext4Image(img_path) as img:
        jr = JournalReader(img)
        jr.open()
        raw = jr.read_fc_area()
        bs = jr.jbd2_sb.block_size or img.block_size
    recs = decode_fc_buffer(raw, block_size=bs)
    evs = [e.to_dict() for e in EventBuilder(recs).build()]
    return evs, time.perf_counter() - t0


# ---------------------------------------------------------------- Sleuth Kit
def _parse_fls(out):
    """fls -r output:  'r/r 13:\ta.txt'  or  'r/r * 14:\tdeleted.txt'"""
    rows = []
    for line in out.splitlines():
        if ':' not in line:
            continue
        deleted = '*' in line.split(':')[0]
        left, _, right = line.partition(':')
        toks = left.replace('*', ' ').split()
        if not toks:
            continue
        try:
            ino = int(toks[-1].split('-')[0])
        except ValueError:
            continue
        name = right.strip()
        if name in ('.', '..') or not name:
            continue
        rows.append((ino, os.path.basename(name), deleted))
    return rows


def run_tsk_fls(img_path):
    t0 = time.perf_counter()
    evs = []
    for args, etype in ((['-r'], 'CREATE'), (['-r', '-d'], 'UNLINK')):
        p = subprocess.run(['fls', *args, img_path],
                           capture_output=True, text=True, timeout=300)
        for ino, name, _deleted in _parse_fls(p.stdout):
            if ino < 11:
                continue
            evs.append(_event(etype, ino, name, source='TSK_fls'))
    return evs, time.perf_counter() - t0


def run_tsk_jls(img_path):
    """jls lists journal blocks; it yields no dentry-level events."""
    t0 = time.perf_counter()
    p = subprocess.run(['jls', img_path], capture_output=True,
                       text=True, timeout=300)
    lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
    fc_refs = sum(1 for ln in lines if 'fast' in ln.lower())
    return [], time.perf_counter() - t0, len(lines), fc_refs, p.stdout


def run_debugfs_logdump(img_path):
    t0 = time.perf_counter()
    p = subprocess.run(['debugfs', '-R', 'logdump -O -n 5', img_path],
                       capture_output=True, text=True, timeout=300)
    txt = p.stdout + p.stderr
    fc_aware = '*** Fast Commit Area ***' in txt
    # debugfs decodes the records but never pairs ADD_ENTRY with DEL_ENTRY,
    # so it reports no rename and emits no ordering or integrity verdict.
    return time.perf_counter() - t0, len(txt.splitlines()), fc_aware, txt


# ---------------------------------------------------------------- plaso
def run_plaso(img_path, workdir):
    """
    Timestamp-based super-timeline.

    Returns (events, seconds, status). *status* is 'ok' when the image was
    parsed, or 'cannot-process' when the extraction phase rejected the
    filesystem. That distinction matters: a tool that fails to parse an image
    has not scored zero, it has produced no result at all, and conflating the
    two would misrepresent it in the comparison table.

    Measured with plaso 20260119: ext4 volumes with the `fast_commit` feature
    are rejected by the extraction phase ("type: EXT, location: / could not be
    processed"), while otherwise-identical volumes without the feature parse
    normally. Confirmed by varying only that mkfs flag.
    """
    if not shutil.which('log2timeline.py'):
        return None, 0.0, 'not-installed'
    storage = Path(workdir) / 'plaso.storage'
    out_csv = Path(workdir) / 'plaso.csv'
    for f in (storage, out_csv):
        if f.exists():
            f.unlink()
    t0 = time.perf_counter()
    ext = subprocess.run(['log2timeline.py', '--status_view', 'none', '--quiet',
                          '--storage-file', str(storage), img_path],
                         capture_output=True, text=True, timeout=1800)
    subprocess.run(['psort.py', '-q', '-o', 'dynamic', '-w', str(out_csv),
                    str(storage)], capture_output=True, text=True, timeout=1800)
    elapsed = time.perf_counter() - t0

    combined = ext.stdout + ext.stderr
    could_not_process = 'could not be processed' in combined

    text = out_csv.read_text(errors='replace') if out_csv.exists() else ''
    evs = []
    for line in text.splitlines()[1:]:
        parts = line.split(',')
        if len(parts) < 2:
            continue
        name = os.path.basename(parts[-1].strip().strip('"'))
        if name:
            evs.append(_event('CREATE', 0, name, source='plaso'))

    if could_not_process and not evs:
        return evs, elapsed, 'cannot-process'
    return evs, elapsed, 'ok'


# ---------------------------------------------------------------- Autopsy
def load_autopsy_csv(path):
    """
    Score an Autopsy report exported as CSV.

    Autopsy is a Java GUI over the TSK engine and cannot be driven headlessly
    here; export one Results-CSV report per scenario and pass the directory
    with --autopsy-csv-dir.
    """
    text = Path(path).read_text(errors='replace')
    evs = []
    for row in csv.DictReader(text.splitlines()):
        name = (row.get('Name') or row.get('File Name')
                or row.get('Source Name') or '')
        name = os.path.basename(name.strip())
        if name and name not in ('.', '..'):
            evs.append(_event('CREATE', 0, name, source='Autopsy'))
    return evs, text



def find_autopsy_csv(csv_dir, scenario):
    """Find the scenario-specific Autopsy Results CSV."""
    if not csv_dir:
        return None
    base = Path(csv_dir)
    for name in (f'{scenario}_autopsy.csv', f'autopsy_{scenario}.csv',
                 f'{scenario}.csv'):
        candidate = base / name
        if candidate.is_file():
            return candidate
    return None


def load_autopsy_runtime(path):
    """Load measured Autopsy ingest seconds keyed by scenario name."""
    if not path:
        return {}
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        return {str(k): float(v) for k, v in data.items()}
    if isinstance(data, list):
        return {str(row['scenario']): float(row['runtime_s'])
                for row in data
                if isinstance(row, dict) and 'scenario' in row
                and 'runtime_s' in row}
    raise ValueError('Autopsy runtime JSON must be an object or list of records')


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--snap-dir', default='data/raw_images')
    ap.add_argument('--gt-dir', default='data/ground_truth')
    ap.add_argument('--output', default='results/measured/tool_comparison.json')
    ap.add_argument('--autopsy-csv', default=None,
                    help='single-scenario compatibility CSV; scored only for S1')
    ap.add_argument('--autopsy-csv-dir', default=None,
                    help='directory containing one Autopsy Results CSV per scenario')
    ap.add_argument('--autopsy-runtime-json', default=None,
                    help='JSON mapping scenario names to measured Autopsy ingest seconds')
    ap.add_argument('--with-plaso', action='store_true',
                    help='also run log2timeline/psort (slow)')
    ap.add_argument('--workdir', default='/tmp/fctrace_cmp')
    args = ap.parse_args()

    snap_dir, gt_dir = Path(args.snap_dir), Path(args.gt_dir)
    Path(args.workdir).mkdir(parents=True, exist_ok=True)
    autopsy_runtime = load_autopsy_runtime(args.autopsy_runtime_json)

    scenarios = sorted(p.stem for p in snap_dir.glob('*.img')
                       if not p.name.endswith('_loop.img'))
    if not scenarios:
        sys.exit(f'no scenario .img files in {snap_dir}. '
                 'Run scripts/run_real_image_tests.py first.')

    print(f'kernel: {os.uname().release}')
    fls_v = subprocess.run(['fls', '-V'], capture_output=True, text=True)
    print(f'sleuthkit: {fls_v.stdout.strip() or fls_v.stderr.strip()}')
    print(f'plaso: {"available" if shutil.which("log2timeline.py") else "not installed"}')
    print(f'autopsy csv dir: {args.autopsy_csv_dir or "not supplied"}')
    print(f'autopsy runtime json: {args.autopsy_runtime_json or "not supplied"}')
    if args.autopsy_csv and not args.autopsy_csv_dir:
        print('legacy single Autopsy CSV mode: the CSV is scored only for S1_normal_workload')
    print()

    hdr = (f'{"scenario":<24} {"tool":<12} {"TP":>3} {"FP":>4} {"FN":>4} '
           f'{"R":>6} {"P":>6} {"F1":>6} {"Ord":>7} {"ms":>9} {"pre-rename":>11}')
    print(hdr)
    print('-' * len(hdr))

    results = []
    for sc in scenarios:
        img = str(snap_dir / f'{sc}.img')
        gt_file = gt_dir / f'{sc}_gt.json'
        if not gt_file.exists():
            continue
        gt = json.loads(gt_file.read_text())
        gt_types = {e['event_type'] for e in gt}

        runs = [('FC-Trace', *run_fctrace(img)),
                ('TSK fls', *run_tsk_fls(img))]

        autopsy_path = find_autopsy_csv(args.autopsy_csv_dir, sc)
        if (autopsy_path is None and args.autopsy_csv
                and sc == 'S1_normal_workload'
                and Path(args.autopsy_csv).is_file()):
            autopsy_path = Path(args.autopsy_csv)

        plaso_status = None
        if args.with_plaso:
            pe, prt, plaso_status = run_plaso(img, args.workdir)
            if pe is not None and plaso_status == 'ok':
                runs.append(('plaso', pe, prt))
        if autopsy_path is not None:
            ae, atxt = load_autopsy_csv(autopsy_path)
            runs.append(('Autopsy', ae, autopsy_runtime.get(sc, 0.0)))

        for tool, evs, rt in runs:
            evs_eval = [e for e in evs if e.get('event_type') in gt_types]
            r = DiffEngine(gt, evs_eval, method=tool, scenario=sc,
                           runtime_s=rt).evaluate()
            ordered = tool == 'FC-Trace'
            ord_s = f'{r.ordering_acc:7.3f}' if ordered else '    n/a'
            # Capability probe: is the pre-rename name anywhere in the output?
            found_pre = any(e.get('name') == PRE_RENAME_NAME
                            or e.get('new_name') == PRE_RENAME_NAME
                            for e in evs)
            probe = ('yes' if found_pre else 'no') if sc.startswith('S1') else '-'
            print(f'{sc:<24} {tool:<12} {r.tp:>3} {r.fp:>4} {r.fn:>4} '
                  f'{r.recall:>6.3f} {r.precision:>6.3f} {r.f1:>6.3f} '
                  f'{ord_s} {rt*1000:>9.1f} {probe:>11}')
            d = r.to_dict()
            d.update(tool=tool, ordering_supported=ordered,
                     recovers_pre_rename_name=found_pre)
            if tool == 'Autopsy':
                d['autopsy_csv'] = str(autopsy_path)
            results.append(d)

        if plaso_status and plaso_status != 'ok':
            note = {'cannot-process': 'extraction phase rejected the filesystem',
                    'not-installed': 'plaso not installed'}[plaso_status]
            print(f'{"":<24} {"plaso":<12} {"n/a":>3} {"n/a":>4} {"n/a":>4} '
                  f'{"n/a":>6} {"n/a":>6} {"n/a":>6} {"n/a":>7} {prt*1000:>9.1f} '
                  f'{"-":>11}   ({note})')
            results.append({'scenario': sc, 'tool': 'plaso',
                            'status': plaso_status, 'note': note,
                            'runtime_s': prt, 'ordering_supported': False})

        _, jls_rt, jls_n, jls_fc, _ = run_tsk_jls(img)
        dbg_rt, dbg_lines, dbg_fc, dbg_txt = run_debugfs_logdump(img)
        dbg_pre = PRE_RENAME_NAME in dbg_txt
        print(f'{"":<24} {"TSK jls":<12} {"n/a":>3} {"n/a":>4} {"n/a":>4} '
              f'{"n/a":>6} {"n/a":>6} {"n/a":>6} {"n/a":>7} {jls_rt*1000:>9.1f} '
              f'{"-":>11}   ({jls_n} blocks, {jls_fc} fast-commit refs)')
        print(f'{"":<24} {"debugfs":<12} {"n/a":>3} {"n/a":>4} {"n/a":>4} '
              f'{"n/a":>6} {"n/a":>6} {"n/a":>6} {"n/a":>7} {dbg_rt*1000:>9.1f} '
              f'{("raw" if dbg_pre else "no"):>11}   '
              f'(FC-aware={dbg_fc}, unordered dump)')
        results.append({'scenario': sc, 'tool': 'TSK jls',
                        'journal_blocks': jls_n, 'fast_commit_refs': jls_fc,
                        'runtime_s': jls_rt, 'ordering_supported': False})
        results.append({'scenario': sc, 'tool': 'debugfs logdump',
                        'fast_commit_aware': dbg_fc, 'lines': dbg_lines,
                        'runtime_s': dbg_rt, 'ordering_supported': False,
                        'recovers_pre_rename_name': 'raw records only'})
        print()

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    print(f'Results written: {out}')
    print('\npre-rename column: can the tool recover "a.txt", the name S1\'s '
          'file had\nbefore it was renamed to b.txt? It exists nowhere in the '
          'file system.')


if __name__ == '__main__':
    main()
