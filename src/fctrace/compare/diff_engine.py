"""
diff_engine.py — Evaluation metrics engine for FC-Trace
========================================================
Computes the following metrics comparing a forensic analysis result
(FC-Trace or a baseline) against a ground-truth event list:

  Metric        Definition
  ──────────────────────────────────────────────────────────
  Recall        |TP| / (|TP| + |FN|)
  Precision     |TP| / (|TP| + |FP|)
  F1            2 * P * R / (P + R)
  Ordering Acc  Fraction of correctly-ordered adjacent TP pairs
  Path Rate     Fraction of TP events where name == ground-truth name
  Runtime       Wall-clock seconds for the analysis (passed externally)

Ground-truth format (list of dicts):
  { 'event_type': str, 'ino': int, 'name': str, 'new_name': str,
    'parent_ino': int }

Matching strategy (lenient):
  Two events match if event_type AND ino agree.  When ino == 0 in the
  result (baselines often cannot recover ino), we fall back to
  event_type + name matching.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class EvaluationResult:
    """Holds all computed metrics for one analysis method on one scenario."""
    method:          str   = ''
    scenario:        str   = ''
    tp:              int   = 0
    fp:              int   = 0
    fn:              int   = 0
    recall:          float = 0.0
    precision:       float = 0.0
    f1:              float = 0.0
    ordering_acc:    float = 0.0
    path_rate:       float = 0.0
    runtime_s:       float = 0.0
    total_gt_events: int   = 0
    total_pred_events: int = 0
    notes:           List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'method':            self.method,
            'scenario':          self.scenario,
            'tp':                self.tp,
            'fp':                self.fp,
            'fn':                self.fn,
            'recall':            round(self.recall, 4),
            'precision':         round(self.precision, 4),
            'f1':                round(self.f1, 4),
            'ordering_acc':      round(self.ordering_acc, 4),
            'path_rate':         round(self.path_rate, 4),
            'runtime_s':         round(self.runtime_s, 4),
            'total_gt_events':   self.total_gt_events,
            'total_pred_events': self.total_pred_events,
            'notes':             self.notes,
        }


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def _event_key(ev: Dict[str, Any]) -> Tuple[str, int]:
    """Primary match key: (event_type, ino)."""
    return (ev.get('event_type', ''), ev.get('ino', 0))


def _event_key_name(ev: Dict[str, Any]) -> Tuple[str, str]:
    """Fallback match key: (event_type, name) when ino is unknown."""
    return (ev.get('event_type', ''), ev.get('name', ''))


def _name_matches(pred: Dict[str, Any], gt: Dict[str, Any]) -> bool:
    """Return True if recovered dentry path fields match ground truth."""
    if pred.get('name', '').strip() != gt.get('name', '').strip():
        return False
    if gt.get('event_type', '') == 'RENAME':
        return (
            pred.get('new_name', '').strip() == gt.get('new_name', '').strip()
            and pred.get('new_parent', 0) == gt.get('new_parent', 0)
        )
    return True


# ---------------------------------------------------------------------------
# Core diff engine
# ---------------------------------------------------------------------------

class DiffEngine:
    """
    Compare predicted events against ground truth and compute metrics.
    """

    def __init__(
        self,
        ground_truth: List[Dict[str, Any]],
        predicted:    List[Dict[str, Any]],
        method:       str  = 'unknown',
        scenario:     str  = 'unknown',
        runtime_s:    float = 0.0,
    ) -> None:
        self._gt   = ground_truth
        self._pred = predicted
        self.method    = method
        self.scenario  = scenario
        self.runtime_s = runtime_s

    def evaluate(self) -> EvaluationResult:
        """Run full evaluation and return an EvaluationResult."""
        res = EvaluationResult(
            method=self.method,
            scenario=self.scenario,
            runtime_s=self.runtime_s,
            total_gt_events=len(self._gt),
            total_pred_events=len(self._pred),
        )

        if not self._gt and not self._pred:
            return res

        tp_pairs, fp_indices, fn_indices = self._match()

        res.tp = len(tp_pairs)
        res.fp = len(fp_indices)
        res.fn = len(fn_indices)

        res.recall    = _safe_div(res.tp, res.tp + res.fn)
        res.precision = _safe_div(res.tp, res.tp + res.fp)
        res.f1        = _harmonic(res.recall, res.precision)

        if tp_pairs:
            res.ordering_acc = self._ordering_accuracy(tp_pairs)
            res.path_rate    = self._path_recovery_rate(tp_pairs)

        logger.info(
            "[%s / %s] TP=%d FP=%d FN=%d R=%.3f P=%.3f F1=%.3f",
            self.method, self.scenario,
            res.tp, res.fp, res.fn,
            res.recall, res.precision, res.f1,
        )
        return res

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def _match(
        self,
    ) -> Tuple[
        List[Tuple[Dict, Dict]],   # (pred, gt) matched pairs
        List[int],                  # indices of unmatched pred (FP)
        List[int],                  # indices of unmatched gt (FN)
    ]:
        """
        Greedy one-to-one matching of predicted events to ground-truth events.

        Priority:
        1. event_type + ino  (strong match)
        2. event_type + name (weak match, only if ino == 0 in pred)
        """
        matched_pred:  Set[int] = set()
        matched_gt:    Set[int] = set()
        tp_pairs: List[Tuple[Dict, Dict]] = []

        # Index ground truth by primary key
        gt_by_key: Dict[Tuple, List[int]] = {}
        for idx, gt_ev in enumerate(self._gt):
            key = _event_key(gt_ev)
            gt_by_key.setdefault(key, []).append(idx)

        gt_by_name: Dict[Tuple, List[int]] = {}
        for idx, gt_ev in enumerate(self._gt):
            key = _event_key_name(gt_ev)
            gt_by_name.setdefault(key, []).append(idx)

        for p_idx, pred_ev in enumerate(self._pred):
            matched = False

            # Strong match (event_type + ino)
            if pred_ev.get('ino', 0) != 0:
                key = _event_key(pred_ev)
                for g_idx in list(gt_by_key.get(key, [])):
                    if g_idx not in matched_gt:
                        tp_pairs.append((pred_ev, self._gt[g_idx]))
                        matched_pred.add(p_idx)
                        matched_gt.add(g_idx)
                        matched = True
                        break

            # Weak match (event_type + name), only when inode is unknown.
            if not matched and pred_ev.get('ino', 0) == 0:
                key = _event_key_name(pred_ev)
                for g_idx in list(gt_by_name.get(key, [])):
                    if g_idx not in matched_gt:
                        tp_pairs.append((pred_ev, self._gt[g_idx]))
                        matched_pred.add(p_idx)
                        matched_gt.add(g_idx)
                        matched = True
                        break

        fp_indices = [i for i in range(len(self._pred))
                      if i not in matched_pred]
        fn_indices = [i for i in range(len(self._gt))
                      if i not in matched_gt]

        return tp_pairs, fp_indices, fn_indices

    # ------------------------------------------------------------------
    # Ordering accuracy
    # ------------------------------------------------------------------

    def _ordering_accuracy(
        self, tp_pairs: List[Tuple[Dict, Dict]]
    ) -> float:
        """
        For each adjacent pair of TP events, check whether the predicted
        ordering agrees with the ground-truth ordering.

        A pair (A, B) is correctly ordered if pred_A precedes pred_B in
        the *predicted* list AND gt_A precedes gt_B in the *ground-truth*
        list.  Both orderings are measured against their respective original
        input lists, not against the tp_pairs sequence.

        Returns the fraction of adjacent pairs that are correctly ordered.
        """
        if len(tp_pairs) < 2:
            return 1.0  # trivially correct

        # Position of each predicted event in the original predicted list
        pred_pos = {id(p): i for i, p in enumerate(self._pred)}
        # Position of each ground-truth event in the original GT list
        gt_pos   = {id(g): i for i, g in enumerate(self._gt)}

        # Sort matched pairs by predicted position
        pairs_by_pred = sorted(
            tp_pairs, key=lambda pq: pred_pos.get(id(pq[0]), 0)
        )

        correct = 0
        total   = len(pairs_by_pred) - 1

        for i in range(total):
            _, g_a = pairs_by_pred[i]
            _, g_b = pairs_by_pred[i + 1]
            # pred says A < B; check if GT also says A < B
            if gt_pos.get(id(g_a), 0) < gt_pos.get(id(g_b), 0):
                correct += 1

        return _safe_div(correct, total)

    # ------------------------------------------------------------------
    # Path recovery rate
    # ------------------------------------------------------------------

    @staticmethod
    def _path_recovery_rate(
        tp_pairs: List[Tuple[Dict, Dict]]
    ) -> float:
        """
        For TP dentry events (CREATE / UNLINK / RENAME / LINK), compute
        the fraction where the predicted name exactly matches ground truth.
        """
        dentry_types = {'CREATE', 'UNLINK', 'RENAME', 'LINK'}
        eligible = [
            (p, g) for p, g in tp_pairs
            if g.get('event_type', '') in dentry_types
        ]
        if not eligible:
            return 1.0   # no dentry events to evaluate
        correct = sum(1 for p, g in eligible if _name_matches(p, g))
        return _safe_div(correct, len(eligible))


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _safe_div(a: float, b: float) -> float:
    return a / b if b > 0 else 0.0


def _harmonic(r: float, p: float) -> float:
    return 2 * p * r / (p + r) if (p + r) > 0 else 0.0


# ---------------------------------------------------------------------------
# Convenience: evaluate one method against one ground-truth file
# ---------------------------------------------------------------------------

def evaluate_against_ground_truth(
    predicted: List[Dict[str, Any]],
    ground_truth_path: str,
    method: str,
    scenario: str,
    runtime_s: float = 0.0,
) -> EvaluationResult:
    """
    Load a JSON ground-truth file and evaluate *predicted* events.

    The ground-truth file is a JSON list of event dicts (same schema as
    ForensicEvent.to_dict()).
    """
    import json
    try:
        with open(ground_truth_path) as fh:
            gt = json.load(fh)
    except Exception as exc:
        logger.error("Cannot load ground truth %s: %s", ground_truth_path, exc)
        gt = []

    engine = DiffEngine(
        ground_truth=gt,
        predicted=predicted,
        method=method,
        scenario=scenario,
        runtime_s=runtime_s,
    )
    return engine.evaluate()
