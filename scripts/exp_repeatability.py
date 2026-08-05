#!/usr/bin/env python3
"""
Repeatability experiment: run the S1-S5 real-image evaluation N times and
report mean +/- standard deviation per metric.

The manuscript currently reports a single run, which reviewers flagged. Fast
commit is timing-sensitive -- whether an operation reaches the FC area depends
on where it falls relative to the JBD2 commit interval -- so repeated trials
are the honest way to characterise it.

Usage: exp_repeatability.py [N]
"""
import json
import statistics as st
import subprocess
import sys
import time
from pathlib import Path

import argparse

_ap = argparse.ArgumentParser(description=__doc__,
                              formatter_class=argparse.RawDescriptionHelpFormatter)
_ap.add_argument('-n', '--runs', type=int, default=5)
_ap.add_argument('--snap-dir', default='/tmp/fctrace_repeat')
_ap.add_argument('--output', default='results/measured/repeatability.json')
_args = _ap.parse_args()

N = _args.runs
WORK = Path(__file__).resolve().parent.parent
OUT = Path(_args.output).parent
OUT.mkdir(parents=True, exist_ok=True)

METRICS = ['recall', 'precision', 'f1', 'ordering_acc', 'path_rate']
runs = []

for i in range(N):
    dest = OUT / f'run{i}.json'
    print(f'--- run {i + 1}/{N} ---', flush=True)
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, 'scripts/run_real_image_tests.py',
         '--output', str(dest),
         '--snap-dir', f'{_args.snap_dir}_{i}',
         '--gt-dir', str(OUT / f'gt{i}'),
         '--scenarios', 'S1,S2,S3,S4,S5'],
        cwd=WORK,
        env={'PYTHONPATH': 'src', 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'},
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f'  FAILED rc={proc.returncode}')
        print(proc.stderr[-2000:])
        continue
    runs.append(json.loads(dest.read_text()))
    print(f'  ok ({time.time() - t0:.0f}s)', flush=True)
    subprocess.run(['rm', '-rf', f'{_args.snap_dir}_{i}'])

if not runs:
    sys.exit('no successful runs')

scenarios = [r['scenario'] for r in runs[0]]
print(f'\n{"=" * 96}')
print(f'REPEATABILITY OVER {len(runs)} RUNS  (mean +/- sd)')
print('=' * 96)
hdr = f'{"scenario":<26}' + ''.join(f'{m:>16}' for m in METRICS)
print(hdr)
print('-' * len(hdr))

summary = {}
for si, sc in enumerate(scenarios):
    row = f'{sc:<26}'
    summary[sc] = {}
    for m in METRICS:
        vals = [r[si][m] for r in runs]
        mean = st.mean(vals)
        sd = st.stdev(vals) if len(vals) > 1 else 0.0
        summary[sc][m] = {'mean': mean, 'sd': sd,
                          'min': min(vals), 'max': max(vals), 'n': len(vals)}
        row += f'{mean:>9.3f}+/-{sd:<4.3f}'
    print(row)

print('-' * len(hdr))
row = f'{"MEAN across scenarios":<26}'
for m in METRICS:
    per_run = [st.mean([r[si][m] for si in range(len(scenarios))]) for r in runs]
    mean = st.mean(per_run)
    sd = st.stdev(per_run) if len(per_run) > 1 else 0.0
    summary.setdefault('_overall', {})[m] = {
        'mean': mean, 'sd': sd, 'min': min(per_run),
        'max': max(per_run), 'n': len(per_run)}
    row += f'{mean:>9.3f}+/-{sd:<4.3f}'
print(row)
print('=' * 96)

# Where did variation actually appear?
print('\nPer-run spread (only metrics that varied):')
any_var = False
for si, sc in enumerate(scenarios):
    for m in METRICS:
        vals = [r[si][m] for r in runs]
        if max(vals) - min(vals) > 1e-9:
            any_var = True
            print(f'  {sc:<26} {m:<14} {[round(v, 3) for v in vals]}')
if not any_var:
    print('  none -- every metric was identical across all runs')

Path(_args.output).write_text(
    json.dumps({'n_runs': len(runs), 'summary': summary}, indent=2))
print(f'\nResults written: {_args.output}')
