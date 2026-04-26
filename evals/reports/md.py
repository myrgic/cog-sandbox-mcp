"""Markdown report renderer.

Produces a plain-text markdown summary table from TrialRecord + RunSummary.
Mirrors the HTML report's content at a lower fidelity for diff-friendly output
and quick terminal review.
"""

from __future__ import annotations

from pathlib import Path

from evals.reports.data import RunSummary, TrialRecord
from evals.reports.matrix import render_matrix


def render_markdown(
    trials: list[TrialRecord],
    summary: RunSummary,
    brief_path: Path | None = None,
) -> str:
    """Render a markdown report string."""
    lines: list[str] = []

    # Header
    pct = 100.0 * summary.passed / summary.total if summary.total else 0.0
    lines.append(f"# Tournament Report: {summary.experiment_id}")
    lines.append("")
    lines.append(f"Run `{summary.run_id}` — {summary.started_at} → {summary.ended_at}")
    lines.append(f"Target: {summary.target} | Model: {summary.model}")
    lines.append("")

    # Brief section
    if brief_path and brief_path.exists():
        lines.append("## Brief")
        lines.append("")
        lines.append(brief_path.read_text(encoding="utf-8"))
        lines.append("")

    # Summary table
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Total trials | {summary.total} |")
    lines.append(f"| Passed | {summary.passed} |")
    lines.append(f"| Failed | {summary.failed} |")
    lines.append(f"| Pass rate | {pct:.1f}% |")
    lines.append("")

    # Variant × task matrix
    if trials and any(t.variant_ids for t in trials):
        rows, cols, cells = render_matrix(trials)
        lines.append("## Variant × Task Matrix")
        lines.append("")
        # Header row
        header = "| Task | " + " | ".join(cols) + " |"
        sep = "|---|" + "|".join(["---"] * len(cols)) + "|"
        lines.append(header)
        lines.append(sep)
        for row in rows:
            row_cells = []
            for col in cols:
                cell = cells.get((row, col))
                row_cells.append(cell.label if cell else "—")
            lines.append("| " + row + " | " + " | ".join(row_cells) + " |")
        lines.append("")

    # Per-trial detail
    lines.append("## Trial Detail")
    lines.append("")
    for t in trials:
        status = "PASS" if t.passed else "FAIL"
        variant_str = ", ".join(f"{k}={v}" for k, v in sorted(t.variant_ids.items()))
        lines.append(f"### {t.trial_id} [{status}]")
        lines.append("")
        lines.append(f"- Task: `{t.task_id}`")
        lines.append(f"- Variants: {variant_str or 'none'}")
        lines.append(f"- Target: {t.target}")
        if t.model:
            lines.append(f"- Model: {t.model}")
        if t.duration_sec is not None:
            lines.append(f"- Duration: {t.duration_sec:.1f}s")
        if t.judge_identity_uri:
            lines.append(f"- Judge: `{t.judge_identity_uri}`")
        if t.cogblock_hash:
            lines.append(f"- CogBlock: `{t.cogblock_hash}`")
        lines.append("")
        if t.failures:
            lines.append("**Failures:**")
            for f in t.failures:
                lines.append(f"- {f}")
            lines.append("")
        if t.tool_calls:
            tc_names = [tc["name"] for tc in t.tool_calls]
            lines.append(f"Tool calls: {', '.join(tc_names)}")
            lines.append("")
        if t.content:
            preview = t.content[:200].replace("\n", " ")
            if len(t.content) > 200:
                preview += "…"
            lines.append(f"Content: {preview}")
            lines.append("")

    return "\n".join(lines)
