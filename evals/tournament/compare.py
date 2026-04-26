"""Scorecard, delta computation, and regression detection.

Scorecard: variant × task → pass/fail
Delta: per-variant pass-rate delta vs a baseline variant configuration
Regression check: stub for Phase 2 (once we have multiple historical runs).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.reports.data import TrialRecord, load_trials_jsonl

log = logging.getLogger(__name__)


@dataclass
class Scorecard:
    """Aggregate pass/fail matrix indexed by (variant_key, task_id)."""

    experiment_id: str
    cells: dict[tuple[str, str], bool | None] = field(default_factory=dict)
    """Keys are (variant_key, task_id). Value is True=pass, False=fail, None=missing."""

    variant_keys: list[str] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)

    def pass_rate(self, variant_key: str) -> float | None:
        """Pass rate for a given variant key across all tasks. None if no data."""
        results = [
            self.cells.get((variant_key, tid))
            for tid in self.task_ids
        ]
        valid = [r for r in results if r is not None]
        if not valid:
            return None
        return sum(1 for r in valid if r) / len(valid)

    def task_pass_rate(self, task_id: str) -> float | None:
        """Pass rate for a given task across all variant keys. None if no data."""
        results = [
            self.cells.get((vk, task_id))
            for vk in self.variant_keys
        ]
        valid = [r for r in results if r is not None]
        if not valid:
            return None
        return sum(1 for r in valid if r) / len(valid)


@dataclass
class Delta:
    """Pass-rate delta between a variant and the baseline."""

    variant_key: str
    baseline_key: str
    delta: float
    """Positive = better than baseline, negative = worse."""

    variant_pass_rate: float | None
    baseline_pass_rate: float | None
    task_deltas: dict[str, float | None] = field(default_factory=dict)
    """Per-task delta — positive = better, negative = worse, None = missing data."""


def _variant_key(trial: TrialRecord) -> str:
    """Reconstruct variant key from trial (matches reports/matrix.py)."""
    sp = trial.variant_ids.get("system_prompt", "unknown-sp")
    td = trial.variant_ids.get("tool_description", "td-1-current")
    return f"{sp} / {td}"


def build_scorecard(trials: list[TrialRecord]) -> Scorecard:
    """Build a Scorecard from a list of TrialRecord objects.

    Multiple trials for the same (variant_key, task_id) are aggregated:
    the cell is True if ANY trial passed, False if ALL failed.
    """
    experiment_id = trials[0].experiment_id if trials else ""

    variant_keys: set[str] = set()
    task_ids: set[str] = set()
    raw: dict[tuple[str, str], list[bool]] = {}

    for t in trials:
        vk = _variant_key(t)
        variant_keys.add(vk)
        task_ids.add(t.task_id)
        key = (vk, t.task_id)
        raw.setdefault(key, []).append(t.passed)

    sorted_vks = sorted(variant_keys)
    sorted_tids = sorted(task_ids)

    cells: dict[tuple[str, str], bool | None] = {}
    for vk in sorted_vks:
        for tid in sorted_tids:
            results = raw.get((vk, tid))
            if results is None:
                cells[(vk, tid)] = None
            else:
                # Aggregate: pass if any trial passed
                cells[(vk, tid)] = any(results)

    return Scorecard(
        experiment_id=experiment_id,
        cells=cells,
        variant_keys=sorted_vks,
        task_ids=sorted_tids,
    )


def compute_deltas(
    scorecard: Scorecard,
    baseline_variant_key: str,
) -> list[Delta]:
    """Compute per-variant deltas vs the baseline variant key.

    If baseline_variant_key is not in scorecard.variant_keys, logs a warning
    and returns deltas with None baseline pass rates.
    """
    if baseline_variant_key not in scorecard.variant_keys:
        log.warning(
            "Baseline variant key %r not in scorecard (keys: %s)",
            baseline_variant_key,
            scorecard.variant_keys,
        )

    baseline_rate = scorecard.pass_rate(baseline_variant_key)

    deltas: list[Delta] = []
    for vk in scorecard.variant_keys:
        if vk == baseline_variant_key:
            continue
        vk_rate = scorecard.pass_rate(vk)

        task_deltas: dict[str, float | None] = {}
        for tid in scorecard.task_ids:
            vk_result = scorecard.cells.get((vk, tid))
            bl_result = scorecard.cells.get((baseline_variant_key, tid))
            if vk_result is None or bl_result is None:
                task_deltas[tid] = None
            else:
                # 1.0 for pass, 0.0 for fail
                task_deltas[tid] = float(vk_result) - float(bl_result)

        delta_val: float
        if vk_rate is not None and baseline_rate is not None:
            delta_val = vk_rate - baseline_rate
        elif vk_rate is None:
            delta_val = float("-inf")
        else:
            delta_val = float("inf")

        deltas.append(
            Delta(
                variant_key=vk,
                baseline_key=baseline_variant_key,
                delta=delta_val,
                variant_pass_rate=vk_rate,
                baseline_pass_rate=baseline_rate,
                task_deltas=task_deltas,
            )
        )

    # Sort by delta descending (best improvements first)
    deltas.sort(key=lambda d: d.delta if d.delta != float("-inf") else -999, reverse=True)
    return deltas


def regression_check(
    current: Scorecard,
    historical_baseline: Scorecard,
    threshold: float = 0.1,
) -> list[dict[str, Any]]:
    """Compare current scorecard to a historical baseline run.

    Returns a list of regression records — cases where current pass rate
    dropped more than `threshold` (0.0–1.0) vs historical baseline.

    Phase 2 / once we have a second run to compare against.
    This is a stub — returns empty list until historical data is available.
    """
    # TODO(Phase 2): implement once we have multiple runs in the run store.
    # Pattern:
    #   for vk in current.variant_keys:
    #     curr_rate = current.pass_rate(vk)
    #     hist_rate = historical_baseline.pass_rate(vk)
    #     if hist_rate is not None and curr_rate is not None:
    #       if hist_rate - curr_rate > threshold:
    #         regressions.append({...})
    log.info(
        "regression_check: stub — no historical data yet (Phase 2). "
        "Run the experiment twice and pass both scorecards to compare."
    )
    return []


def load_scorecard_from_jsonl(path: Path) -> Scorecard | None:
    """Load a Scorecard from a run's results.jsonl. Convenience for compare.py CLI use."""
    trials = load_trials_jsonl(path)
    if not trials:
        log.warning("No trials loaded from %s", path)
        return None
    return build_scorecard(trials)
