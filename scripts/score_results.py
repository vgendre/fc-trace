#!/usr/bin/env python3
"""
score_results.py — FC-Trace batch evaluation runner
=====================================================
Evaluates FC-Trace against every ground-truth scenario, printing a metric
table and writing results to JSON. Real-image baseline analyzers are available
with --include-baselines, but their low-level event models are not directly
comparable to the dentry-event ground truth and should be treated as
exploratory diagnostics unless a comparable baseline event model is added.

Usage (after generating images with generate_dataset.py)::

    python3 scripts/score_results.py \\
        --images-dir  data/raw_images \\
        --gt-dir      data/ground_truth \\
        --output      results/evaluation.json

For simulation mode (no real images required)::

    python3 scripts/score_results.py --simulate

The --simulate flag uses canonical TLV buffers to validate parser and
event-reconstruction behavior. Baseline outputs in simulation mode are
capability-model references, not independent executions of external tools.
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

# Allow running from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from fctrace.compare.diff_engine import DiffEngine, EvaluationResult
from fctrace.compare.baseline_ext4 import (
    InodeOnlyAnalyzer,
    JournalOnlyAnalyzer,
    SleuthKitAdapter,
)
from fctrace.parser.fc_tags import (
    FCTag, STRUCT_FC_TL, STRUCT_FC_HEAD, STRUCT_FC_TAIL,
    STRUCT_FC_DENTRY, STRUCT_FC_RANGE_INO, STRUCT_EXT4_EXTENT,
    STRUCT_FC_DEL_RANGE,
)
from fctrace.parser.tlv_decoder import decode_fc_buffer
from fctrace.reconstruct.event_builder import EventBuilder

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('score_results')

# ─────────────────────────────────────────────────────────────
# TLV helpers (simulation mode)
# ─────────────────────────────────────────────────────────────

def _pt(tag, v):
    return STRUCT_FC_TL.pack(int(tag), len(v)) + v

def _head(t):   return _pt(FCTag.HEAD, STRUCT_FC_HEAD.pack(0, t))
def _tail(t):   return _pt(FCTag.TAIL, STRUCT_FC_TAIL.pack(t, 0))
def _creat(p, i, n):  return _pt(FCTag.CREAT,  STRUCT_FC_DENTRY.pack(p, i) + n.encode())
def _unlink(p, i, n): return _pt(FCTag.UNLINK, STRUCT_FC_DENTRY.pack(p, i) + n.encode())
def _link(p, i, n):   return _pt(FCTag.LINK,   STRUCT_FC_DENTRY.pack(p, i) + n.encode())
def _rename(src_p, dst_p, i, src, dst): return _link(dst_p, i, dst) + _unlink(src_p, i, src)
def _del_r(i):        return _pt(FCTag.DEL_RANGE, STRUCT_FC_DEL_RANGE.pack(i, 0, 4))

def _gt(et, i, p=2, n='', nn='', np=0):
    return {'event_type': et, 'ino': i, 'parent_ino': p,
            'name': n, 'new_name': nn, 'new_parent': np}


def _build_simulation_scenarios():
    """Build canonical TLV buffers and ground-truth lists for all 5 scenarios."""
    scenarios = {}

    # S1: Normal workload
    buf = (_head(1) + _creat(2,11,'a.txt') +
           _rename(2,2,11,'a.txt','b.txt') +
           _link(2,11,'hardlink.txt') + _unlink(2,11,'hardlink.txt') +
           _creat(2,12,'c.txt') + _unlink(2,12,'c.txt') + _tail(1))
    gt = [_gt('CREATE',11,n='a.txt'), _gt('RENAME',11,n='a.txt',nn='b.txt',np=2),
          _gt('LINK',11,n='hardlink.txt'), _gt('UNLINK',11,n='hardlink.txt'),
          _gt('CREATE',12,n='c.txt'), _gt('UNLINK',12,n='c.txt')]
    scenarios['S1_normal_workload'] = {'buf': buf, 'gt': gt, 'crash': False}

    # S2: Crash-before-commit
    buf = b''.join(_head(10+i) + _creat(2, 20+i, f'crash_{i}.dat') for i in range(5))
    gt  = [_gt('CREATE', 20+i, n=f'crash_{i}.dat') for i in range(5)]
    scenarios['S2_crash_before_commit'] = {'buf': buf, 'gt': gt, 'crash': True}

    # S3: Anti-forensic burst
    buf = b''
    gt  = []
    for i in range(10):
        ino = 50 + i; name = f'secret_{i}.bin'
        buf += _head(20+i) + _creat(2,ino,name) + _del_r(ino) + _unlink(2,ino,name) + _tail(20+i)
        gt  += [_gt('CREATE',ino,n=name), _gt('EXTENT_DEL',ino), _gt('UNLINK',ino,n=name)]
    scenarios['S3_antiforensic_burst'] = {'buf': buf, 'gt': gt, 'crash': False}

    # S4: Short-lived files
    buf = b''
    gt  = []
    for i in range(8):
        ino=80+i; src=f'tmp_{i}_src.tmp'; dst=f'tmp_{i}_dst.tmp'
        buf += _head(30+i)+_creat(2,ino,src)+_rename(2,2,ino,src,dst)+_unlink(2,ino,dst)+_tail(30+i)
        gt  += [_gt('CREATE',ino,n=src), _gt('RENAME',ino,n=src,nn=dst,np=2), _gt('UNLINK',ino,n=dst)]
    scenarios['S4_shortlived_files'] = {'buf': buf, 'gt': gt, 'crash': False}

    # S5: Deep rename tree
    buf = (_head(40) + _creat(3,90,'target.txt') +
           _rename(3,4,90,'target.txt','moved.txt') +
           _rename(4,2,90,'moved.txt','moved.txt') + _tail(40))
    gt  = [_gt('CREATE',90,p=3,n='target.txt'),
           _gt('RENAME',90,p=3,n='target.txt',nn='moved.txt',np=4),
           _gt('RENAME',90,p=4,n='moved.txt', nn='moved.txt',np=2)]
    scenarios['S5_deep_rename_tree'] = {'buf': buf, 'gt': gt, 'crash': False}

    return scenarios


# ─────────────────────────────────────────────────────────────
# Core evaluation
# ─────────────────────────────────────────────────────────────

def run_fc_trace_on_buffer(buf: bytes) -> List[dict]:
    """Run the full FC-Trace pipeline on a TLV byte buffer."""
    recs = decode_fc_buffer(buf)
    return [e.to_dict() for e in EventBuilder(recs).build()]


def run_fc_trace_on_image(image_path: str) -> tuple:
    """Run FC-Trace on a real disk image. Returns (events, runtime_s)."""
    from fctrace.io.image_reader import Ext4Image
    from fctrace.io.journal_reader import JournalReader
    t0 = time.perf_counter()
    with Ext4Image(image_path) as img:
        jr = JournalReader(img)
        jr.open()
        buf = jr.read_fc_area()
    recs   = decode_fc_buffer(buf)
    events = [e.to_dict() for e in EventBuilder(recs).build()]
    return events, time.perf_counter() - t0


def run_baseline_on_image(image_path: str, baseline_name: str) -> tuple[List[dict], float]:
    """
    Run one baseline analyzer on a real disk image.
    Returns (events, runtime_s). Never raises to callers.
    """
    analyzers = {
        'B1_inode_only': InodeOnlyAnalyzer,
        'B2_journal_only': JournalOnlyAnalyzer,
        'B3_sleuthkit': SleuthKitAdapter,
    }
    analyzer_cls = analyzers[baseline_name]
    t0 = time.perf_counter()
    try:
        events = analyzer_cls(image_path).analyze()
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s failed on %s: %s", baseline_name, image_path, exc)
        events = []
    return events, time.perf_counter() - t0


def _filter_to_gt_event_types(predicted: List[dict], gt: List[dict]) -> List[dict]:
    """
    Keep only event classes represented in ground truth.

    FC-Trace can emit lower-level support records such as INODE_UPDATE and
    EXTENT_ADD. Those are valid parser output, but they should not count as
    false positives when a scenario's ground truth is scoped to dentry events.
    """
    gt_types = {ev.get('event_type', '') for ev in gt}
    return [ev for ev in predicted if ev.get('event_type', '') in gt_types]


def _simulate_baseline_predictions(sc_id: str, gt: List[dict]) -> Dict[str, List[dict]]:
    """
    Return capability-model baseline predictions for each method.

    These are not measured outputs from inode scanners, JBD2 analyzers, or
    Sleuth Kit. They only provide a conservative reference point for the
    simulation table, whose primary purpose is validating FC-Trace decoding.
    """
    crash  = sc_id == 'S2'
    af     = sc_id == 'S3'
    short  = sc_id == 'S4'
    deep   = sc_id == 'S5'
    normal = sc_id == 'S1'

    def _gt(et, i, n=''):
        return {'event_type': et, 'ino': i, 'parent_ino': 2,
                'name': n, 'new_name': '', 'new_parent': 0}

    if normal:
        b1 = [_gt('CREATE',11,n='b.txt'), _gt('CREATE',12,n='')]
        b3 = [_gt('CREATE',11,n='b.txt'), _gt('UNLINK',12,n='c.txt')]
    elif crash:
        b1 = b3 = []
    elif af:
        b1 = b3 = []
    elif short:
        b1 = []
        b3 = [_gt('UNLINK',80+i,n=f'tmp_{i}_dst.tmp') for i in range(4)]
    elif deep:
        b1 = [_gt('CREATE',90,n='moved.txt')]
        b3 = [_gt('CREATE',90,n='moved.txt')]
    else:
        b1 = b3 = []

    return {
        'B1_inode_only': b1,
        'B2_journal_only': [],   # no journal evidence in FC window for any scenario
        'B3_sleuthkit': b3,
    }


# ─────────────────────────────────────────────────────────────
# Report formatting
# ─────────────────────────────────────────────────────────────

def _print_table(all_results: List[EvaluationResult]) -> None:
    """Print a formatted metric table to stdout."""
    W = 74
    print()
    print("╔" + "═"*W + "╗")
    print("║  FC-TRACE EVALUATION RESULTS" + " "*(W-29) + "║")
    print("╠" + "═"*W + "╣")
    hdr = f"  {'Scenario':<30}{'Method':<14}{'R':>6}{'P':>6}{'F1':>6}{'Ord':>6}{'Path':>6}  "
    print(f"║{hdr}║")
    print("╠" + "═"*W + "╣")

    current_sc = None
    for res in all_results:
        sc_label = res.scenario[:28] if res.scenario != current_sc else ''
        current_sc = res.scenario
        ord_str = f"{res.ordering_acc:6.3f}" if res.method == 'FC-Trace' else "   ---"
        line = (f"  {sc_label:<30}{res.method:<14}"
                f"{res.recall:6.3f}{res.precision:6.3f}{res.f1:6.3f}"
                f"{ord_str}{res.path_rate:6.3f}  ")
        marker = "★" if res.method == 'FC-Trace' else " "
        print(f"║{marker}{line[1:]}║")

    print("╠" + "═"*W + "╣")
    fc_results = [r for r in all_results if r.method == 'FC-Trace']
    avg_r  = sum(r.recall for r in fc_results) / len(fc_results)
    avg_p  = sum(r.precision for r in fc_results) / len(fc_results)
    avg_f1 = sum(r.f1 for r in fc_results) / len(fc_results)
    avg_ord = sum(r.ordering_acc for r in fc_results) / len(fc_results)
    avg_path = sum(r.path_rate for r in fc_results) / len(fc_results)
    avg_line = (f"  {'FC-Trace mean (all scenarios)':<30}{'':14}"
                f"{avg_r:6.3f}{avg_p:6.3f}{avg_f1:6.3f}{avg_ord:6.3f}{avg_path:6.3f}  ")
    print(f"║★{avg_line[1:]}║")
    print("╚" + "═"*W + "╝")
    print()


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description='FC-Trace batch evaluation runner',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--simulate', action='store_true',
                        help='Use canonical TLV buffers (no disk images required)')
    parser.add_argument('--include-simulated-baselines', action='store_true',
                        help='In --simulate mode, also print capability-model baseline rows')
    parser.add_argument('--include-baselines', action='store_true',
                        help='In real-image mode, also run exploratory baseline analyzers')
    parser.add_argument('--images-dir', default='data/raw_images',
                        help='Directory containing *.img scenario images')
    parser.add_argument('--gt-dir', default='data/ground_truth',
                        help='Directory containing *_gt.json ground-truth files')
    parser.add_argument('--output', default='results/evaluation.json',
                        help='Output path for JSON results')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    all_results: List[EvaluationResult] = []
    sc_ids = ['S1', 'S2', 'S3', 'S4', 'S5']

    if args.simulate:
        logger.info("Running in SIMULATION mode (canonical TLV buffers)")
        scenarios = _build_simulation_scenarios()
        for sc_id, (sc_name, sc_data) in zip(sc_ids, scenarios.items()):
            gt     = sc_data['gt']
            buf    = sc_data['buf']
            t0     = time.perf_counter()
            pred   = run_fc_trace_on_buffer(buf)
            rt     = time.perf_counter() - t0

            pred_eval = _filter_to_gt_event_types(pred, gt)
            fc_res = DiffEngine(gt, pred_eval, method='FC-Trace',
                                scenario=sc_name, runtime_s=rt).evaluate()
            all_results.append(fc_res)

            if args.include_simulated_baselines:
                bl_preds = _simulate_baseline_predictions(sc_id, gt)
                for bl_name, bl_pred in bl_preds.items():
                    bl_pred_eval = _filter_to_gt_event_types(bl_pred, gt)
                    bl_res = DiffEngine(gt, bl_pred_eval, method=bl_name,
                                        scenario=sc_name).evaluate()
                    all_results.append(bl_res)

    else:
        gt_dir  = Path(args.gt_dir)
        img_dir = Path(args.images_dir)
        any_image_evaluated = False
        for sc_id in sc_ids:
            gt_files  = list(gt_dir.glob(f'{sc_id}_*_gt.json'))
            img_files = list(img_dir.glob(f'{sc_id}_*.img'))
            if not gt_files:
                logger.warning("No ground-truth for %s — skipping", sc_id)
                continue
            gt = json.loads(gt_files[0].read_text())
            sc_name = gt_files[0].stem.replace('_gt', '')

            if img_files:
                try:
                    pred, rt = run_fc_trace_on_image(str(img_files[0]))
                    any_image_evaluated = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Scenario %s image failed to parse (%s). Skipping.",
                        sc_id,
                        exc,
                    )
                    continue
            else:
                logger.warning("No image for %s — skipping scenario in real-image mode", sc_id)
                continue

            pred_eval = _filter_to_gt_event_types(pred, gt)
            fc_res = DiffEngine(gt, pred_eval, method='FC-Trace',
                                scenario=sc_name, runtime_s=rt).evaluate()
            all_results.append(fc_res)

            if args.include_baselines:
                logger.warning(
                    "Baseline rows are exploratory: B1/B2/B3 emit low-level "
                    "event models that are not directly comparable to FC-Trace "
                    "dentry-event ground truth."
                )
                for bl_name in ('B1_inode_only', 'B2_journal_only', 'B3_sleuthkit'):
                    bl_pred, bl_rt = run_baseline_on_image(str(img_files[0]), bl_name)
                    bl_pred_eval = _filter_to_gt_event_types(bl_pred, gt)
                    bl_res = DiffEngine(
                        gt,
                        bl_pred_eval,
                        method=bl_name,
                        scenario=sc_name,
                        runtime_s=bl_rt,
                    ).evaluate()
                    bl_res.notes.append(
                        'Exploratory baseline row; baseline event model is not directly comparable.'
                    )
                    all_results.append(bl_res)

        if not any_image_evaluated:
            logger.error(
                "No scenario images found under %s. "
                "Generate dataset first or run with --simulate.",
                img_dir,
            )
            return 2

    _print_table(all_results)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w') as fh:
        json.dump([r.to_dict() for r in all_results], fh, indent=2)
    logger.info("Results written: %s", out_path)

    # Summary
    fc_res = [r for r in all_results if r.method == 'FC-Trace']
    if fc_res:
        avg_f1 = sum(r.f1 for r in fc_res) / len(fc_res)
        print(f"FC-Trace mean F1 across all scenarios: {avg_f1:.4f}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
