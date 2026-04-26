"""Variant × task matrix pivot helpers.

Produces raw structures that html.py and md.py use to render the matrix view.
Decoupled from rendering so both output formats share the same pivot logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from evals.reports.data import TrialRecord


@dataclass
class MatrixCell:
    """Value for one cell in the variant × task matrix."""

    passed: bool | None
    """True = pass, False = fail, None = no trial recorded for this cell."""

    trial_ids: list[str]
    """All trial IDs that contributed to this cell (may be multiple reps)."""

    pass_count: int = 0
    fail_count: int = 0

    @property
    def label(self) -> str:
        if self.passed is None:
            return "—"
        if self.pass_count + self.fail_count == 0:
            return "—"
        if self.pass_count > 0 and self.fail_count == 0:
            return "pass"
        if self.pass_count == 0:
            return "fail"
        return f"{self.pass_count}/{self.pass_count + self.fail_count}"

    @property
    def css_class(self) -> str:
        if self.passed is None:
            return "matrix-empty"
        if self.pass_count > 0 and self.fail_count == 0:
            return "matrix-active"
        if self.pass_count == 0 and self.fail_count > 0:
            return "matrix-fail"
        return "matrix-planned"


def _variant_key(trial: TrialRecord) -> str:
    """Build a stable column key from a trial's variant_ids dict.

    Columns are (sp_id, td_id) tuples rendered as 'sp+td' strings. Trials
    with no tool_description axis fall back to 'td-1-current' for grouping.
    """
    sp = trial.variant_ids.get("system_prompt", "unknown-sp")
    td = trial.variant_ids.get("tool_description", "td-1-current")
    return f"{sp} / {td}"


def render_matrix(
    trials: list[TrialRecord],
) -> tuple[list[str], list[str], dict[tuple[str, str], MatrixCell]]:
    """Pivot trials into a task × variant matrix.

    Returns:
        rows: sorted list of task_ids (row labels)
        cols: sorted list of variant keys (column labels, format 'sp / td')
        cells: dict mapping (task_id, variant_key) → MatrixCell
    """
    task_ids: set[str] = set()
    variant_keys: set[str] = set()
    raw: dict[tuple[str, str], list[TrialRecord]] = {}

    for trial in trials:
        tid = trial.task_id
        vkey = _variant_key(trial)
        task_ids.add(tid)
        variant_keys.add(vkey)
        key = (tid, vkey)
        raw.setdefault(key, []).append(trial)

    rows = sorted(task_ids)
    cols = sorted(variant_keys)

    cells: dict[tuple[str, str], MatrixCell] = {}
    for row in rows:
        for col in cols:
            key = (row, col)
            trials_here = raw.get(key, [])
            if not trials_here:
                cells[key] = MatrixCell(passed=None, trial_ids=[])
            else:
                passes = sum(1 for t in trials_here if t.passed)
                fails = len(trials_here) - passes
                cells[key] = MatrixCell(
                    passed=passes > 0,
                    trial_ids=[t.trial_id for t in trials_here],
                    pass_count=passes,
                    fail_count=fails,
                )

    return rows, cols, cells
