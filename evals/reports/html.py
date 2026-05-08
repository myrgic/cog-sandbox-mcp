"""Dark-first HTML report renderer for tournament and eval runs.

Two-tab layout: Brief (narrative) + Data (matrix + per-trial drill-down).
Pure CSS tabs via radio + label pattern. No JavaScript.
Native <details>/<summary> for per-trial drill-down.

CSS variables and .matrix-cell classes adapted from internal modality-bus-report styles.

Brief tab loads from:
  brief.html (raw HTML) → brief.md (minimal markdown) → auto-generated default
"""

from __future__ import annotations

import html as _html
import re
from pathlib import Path

from evals.reports.data import RunSummary, TrialRecord
from evals.reports.matrix import render_matrix


# ---------------------------------------------------------------------------
# Minimal markdown → HTML converter (handles headers, bold, italic, code,
# code blocks, lists, blockquotes, links, paragraphs)
# ---------------------------------------------------------------------------

def minimal_md_to_html(md: str) -> str:
    """Tiny markdown subset → HTML. Not a real parser — for brief.md only."""
    out: list[str] = []
    lines = md.split("\n")
    in_code = False
    in_list = False
    in_para = False

    def close_para() -> None:
        nonlocal in_para
        if in_para:
            out.append("</p>")
            in_para = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    def inline(s: str) -> str:
        s = _html.escape(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
        return s

    for line in lines:
        if line.startswith("```"):
            close_para()
            close_list()
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                out.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            out.append(_html.escape(line) + "\n")
            continue
        m = re.match(r"^(#{1,4})\s+(.+)$", line)
        if m:
            close_para()
            close_list()
            level = len(m.group(1))
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            continue
        m = re.match(r"^[-*]\s+(.+)$", line)
        if m:
            close_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(m.group(1))}</li>")
            continue
        m = re.match(r"^>\s+(.+)$", line)
        if m:
            close_para()
            close_list()
            out.append(f"<blockquote>{inline(m.group(1))}</blockquote>")
            continue
        if line.strip() == "":
            close_para()
            close_list()
            continue
        if not in_para:
            out.append("<p>")
            in_para = True
        out.append(inline(line) + " ")

    close_para()
    close_list()
    if in_code:
        out.append("</code></pre>")
    return "".join(out)


# ---------------------------------------------------------------------------
# Inline CSS — dark-first, matrix-cell classes from modality-bus-report.html
# ---------------------------------------------------------------------------

_CSS = """
  :root {
    --bg: #0d1117;
    --surface: #161b22;
    --surface2: #1c2333;
    --border: #30363d;
    --text: #c9d1d9;
    --text-dim: #8b949e;
    --accent: #58a6ff;
    --accent2: #7ee787;
    --accent3: #d2a8ff;
    --accent4: #ffa657;
    --accent5: #ff7b72;
    --accent6: #79c0ff;
    --green: #3fb950;
    --yellow: #d29922;
    --red: #f85149;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', system-ui, sans-serif;
    font-size: 14px;
    line-height: 1.6;
  }
  h1 { font-size: 24px; font-weight: 700; margin-bottom: 4px; color: #f0f6fc; }
  h2 { font-size: 18px; font-weight: 600; margin: 28px 0 12px; color: #f0f6fc;
       border-bottom: 1px solid var(--border); padding-bottom: 8px; }
  h3 { font-size: 14px; font-weight: 600; margin: 16px 0 8px; color: var(--accent); }
  h4 { font-size: 13px; font-weight: 600; margin: 12px 0 6px; color: var(--text-dim); }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  code { background: var(--surface2); padding: 1px 5px; border-radius: 3px;
         font-size: 12px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
         color: var(--accent4); }
  pre { background: var(--surface2); border: 1px solid var(--border); border-radius: 6px;
        padding: 12px; font-size: 12px; overflow-x: auto; white-space: pre-wrap; }
  pre code { background: none; padding: 0; color: var(--text); }

  /* Layout */
  header {
    padding: 20px 28px; background: var(--surface);
    border-bottom: 1px solid var(--border);
  }
  .subtitle { color: var(--text-dim); font-size: 13px; margin-top: 4px; }
  main { max-width: 1200px; margin: 0 auto; padding: 24px 28px 80px; }
  section { margin-bottom: 32px; }

  /* Cards */
  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px;
  }
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
  }
  .stat-card { text-align: center; }
  .stat-value { font-size: 28px; font-weight: 700; color: #f0f6fc; }
  .stat-label { font-size: 11px; color: var(--text-dim); text-transform: uppercase;
                letter-spacing: 0.5px; margin-top: 2px; }
  .stat-value.pass { color: var(--green); }
  .stat-value.fail { color: var(--red); }

  /* Badges */
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 11px; font-weight: 600;
  }
  .badge-green { background: rgba(63,185,80,0.15); color: var(--green); }
  .badge-red { background: rgba(248,81,73,0.15); color: var(--red); }
  .badge-yellow { background: rgba(210,153,34,0.15); color: var(--yellow); }
  .badge-blue { background: rgba(88,166,255,0.15); color: var(--accent); }

  /* Tables */
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 8px 12px; border-bottom: 2px solid var(--border);
       color: var(--text-dim); font-weight: 600; font-size: 11px;
       text-transform: uppercase; letter-spacing: 0.5px; }
  td { padding: 8px 12px; border-bottom: 1px solid var(--border); }
  tr:hover td { background: rgba(88,166,255,0.04); }

  /* Matrix — lifted from modality-bus-report.html lines 109-116 */
  .matrix-cell {
    width: 28px; height: 28px; display: inline-flex; align-items: center;
    justify-content: center; border-radius: 4px; font-size: 14px;
  }
  .matrix-active { background: rgba(63,185,80,0.2); color: var(--green); }
  .matrix-planned { background: rgba(210,153,34,0.15); color: var(--yellow); }
  .matrix-empty { background: rgba(139,148,158,0.08); color: var(--text-dim); }
  .matrix-fail { background: rgba(248,81,73,0.15); color: var(--red); }

  /* Matrix table */
  .matrix-table { border-collapse: collapse; font-size: 12px; }
  .matrix-table th { padding: 6px 10px; font-size: 10px; max-width: 140px;
                     white-space: normal; word-break: break-word; }
  .matrix-table td { padding: 4px 10px; text-align: center; }
  .matrix-table td:first-child { text-align: left; font-size: 11px;
                                  color: var(--text-dim); white-space: nowrap; }

  /* Details / drill-down */
  details { border: 1px solid var(--border); border-radius: 6px;
            margin: 8px 0; background: var(--surface); }
  details[open] { background: var(--surface2); }
  summary {
    padding: 10px 14px; cursor: pointer; font-size: 13px; font-weight: 500;
    list-style: none; display: flex; align-items: center; gap: 10px;
  }
  summary::-webkit-details-marker { display: none; }
  summary::before { content: '▶'; font-size: 10px; color: var(--text-dim);
                    transition: transform 0.15s; }
  details[open] > summary::before { transform: rotate(90deg); }
  .detail-body { padding: 12px 14px 14px; border-top: 1px solid var(--border); }
  .tool-call { margin: 6px 0; font-size: 12px; }
  .tool-name { color: var(--accent); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .tool-result { color: var(--text-dim); }
  .failure-list { padding-left: 20px; margin: 6px 0; }
  .failure-list li { color: var(--red); font-size: 12px; margin: 3px 0; }
  .note-list { padding-left: 20px; margin: 6px 0; }
  .note-list li { color: var(--text-dim); font-size: 12px; margin: 3px 0; }

  /* Tabs — pure CSS radio + label */
  input[name="tab"] { display: none; }
  .tabs {
    display: flex; gap: 0; border-bottom: 1px solid var(--border);
    background: var(--surface); padding: 0 28px;
  }
  .tabs label {
    padding: 11px 18px; cursor: pointer; font-size: 13px; font-weight: 500;
    color: var(--text-dim); border-bottom: 2px solid transparent;
    margin-bottom: -1px; user-select: none;
  }
  .tabs label:hover { color: var(--accent); }
  .pane { display: none; }
  #tab-brief:checked ~ .tabs label[for="tab-brief"],
  #tab-data:checked ~ .tabs label[for="tab-data"] {
    color: var(--accent); border-bottom-color: var(--accent);
  }
  #tab-brief:checked ~ main .pane-brief,
  #tab-data:checked ~ main .pane-data { display: block; }

  /* Brief pane styles */
  .brief-content {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 28px 32px; line-height: 1.7; font-size: 14px;
  }
  .brief-content h1 { font-size: 22px; margin-top: 0; }
  .brief-content h2 { font-size: 17px; margin-top: 24px; }
  .brief-content h3 { font-size: 14px; margin-top: 16px; }
  .brief-content p { margin: 10px 0; }
  .brief-content ul, .brief-content ol { padding-left: 24px; }
  .brief-content li { margin: 4px 0; }
  .brief-content blockquote {
    border-left: 3px solid var(--accent); padding-left: 14px;
    color: var(--text-dim); margin: 10px 0; font-style: italic;
  }
  .brief-content pre { margin: 12px 0; }
  .alert {
    padding: 10px 14px; border-radius: 6px; margin: 10px 0; font-size: 13px;
  }
  .alert-warn { background: rgba(210,153,34,0.1); border: 1px solid var(--yellow);
                color: var(--yellow); }

  @media (max-width: 800px) {
    main { padding: 16px; }
    .stats-grid { grid-template-columns: repeat(2, 1fr); }
  }
"""


# ---------------------------------------------------------------------------
# Brief rendering
# ---------------------------------------------------------------------------

def _render_brief(
    summary: RunSummary,
    trials: list[TrialRecord],
    brief_path: Path | None,
) -> str:
    """Return HTML for the Brief tab content."""
    # Prefer explicit brief files
    if brief_path:
        html_brief = brief_path.parent / "brief.html"
        md_brief = brief_path if brief_path.suffix == ".md" else brief_path.parent / "brief.md"
        if html_brief.exists():
            return f'<div class="brief-content">{html_brief.read_text(encoding="utf-8")}</div>'
        if md_brief.exists():
            return f'<div class="brief-content">{minimal_md_to_html(md_brief.read_text(encoding="utf-8"))}</div>'

    # Also check adjacent brief.html / brief.md if brief_path is a directory
    if brief_path and brief_path.is_dir():
        if (brief_path / "brief.html").exists():
            return f'<div class="brief-content">{(brief_path / "brief.html").read_text(encoding="utf-8")}</div>'
        if (brief_path / "brief.md").exists():
            return f'<div class="brief-content">{minimal_md_to_html((brief_path / "brief.md").read_text(encoding="utf-8"))}</div>'

    # Auto-generated default
    pct = 100.0 * summary.passed / summary.total if summary.total else 0.0
    return f"""
        <div class="brief-content">
            <div class="alert alert-warn">
                No <code>brief.html</code> or <code>brief.md</code> found —
                showing auto-generated default. Write a narrative report there to replace this section.
            </div>
            <h2>Run summary</h2>
            <p>{summary.total} trials, {pct:.1f}% pass ({summary.passed} passed, {summary.failed} failed).</p>
            <p>Experiment: <code>{_html.escape(summary.experiment_id)}</code><br>
               Run: <code>{_html.escape(summary.run_id)}</code><br>
               Target: <code>{_html.escape(summary.target)}</code><br>
               Model: <code>{_html.escape(summary.model)}</code></p>
            <p class="brief-meta">Switch to the Data tab for the full matrix.</p>
        </div>
    """


# ---------------------------------------------------------------------------
# Matrix table rendering
# ---------------------------------------------------------------------------

def _render_matrix_table(trials: list[TrialRecord]) -> str:
    """Render variant × task matrix as an HTML table using .matrix-cell CSS."""
    if not trials:
        return "<p style='color:var(--text-dim)'>No trials to display.</p>"

    rows, cols, cells = render_matrix(trials)

    if not rows or not cols:
        return "<p style='color:var(--text-dim)'>No matrix data.</p>"

    parts: list[str] = ['<div style="overflow-x:auto">']
    parts.append('<table class="matrix-table">')

    # Header row
    parts.append("<thead><tr>")
    parts.append('<th style="min-width:160px">Task</th>')
    for col in cols:
        esc_col = _html.escape(col)
        parts.append(f"<th>{esc_col}</th>")
    parts.append("</tr></thead>")

    # Data rows
    parts.append("<tbody>")
    for row in rows:
        parts.append("<tr>")
        parts.append(f"<td>{_html.escape(row)}</td>")
        for col in cols:
            cell = cells.get((row, col))
            if cell is None or cell.passed is None:
                parts.append('<td><span class="matrix-cell matrix-empty">—</span></td>')
            else:
                css = cell.css_class
                label = "✓" if (cell.pass_count > 0 and cell.fail_count == 0) else (
                    "✗" if cell.pass_count == 0 else cell.label
                )
                # Link to first trial for this cell if we have an ID
                trial_anchor = ""
                if cell.trial_ids:
                    trial_anchor = f' title="{_html.escape(cell.trial_ids[0])}"'
                parts.append(
                    f'<td><span class="matrix-cell {css}"{trial_anchor}>{label}</span></td>'
                )
        parts.append("</tr>")
    parts.append("</tbody></table></div>")

    return "".join(parts)


# ---------------------------------------------------------------------------
# Per-trial drill-down (details/summary)
# ---------------------------------------------------------------------------

def _render_trial_detail(trial: TrialRecord) -> str:
    """Render a single trial as a <details> block."""
    status_badge = (
        '<span class="badge badge-green">PASS</span>'
        if trial.passed
        else '<span class="badge badge-red">FAIL</span>'
    )
    variant_str = " / ".join(
        f"{k}={v}" for k, v in sorted(trial.variant_ids.items())
    ) or "no variants"

    parts: list[str] = []
    parts.append(
        f'<details id="trial-{_html.escape(trial.trial_id)}">'
    )
    parts.append(
        f'<summary>{status_badge} <code>{_html.escape(trial.trial_id)}</code>'
        f' &nbsp;·&nbsp; {_html.escape(trial.task_id)}'
        f' &nbsp;·&nbsp; <span style="color:var(--text-dim);font-size:12px">'
        f'{_html.escape(variant_str)}</span></summary>'
    )
    parts.append('<div class="detail-body">')

    # Metadata
    meta: list[str] = []
    if trial.target:
        meta.append(f"Target: <code>{_html.escape(trial.target)}</code>")
    if trial.model:
        meta.append(f"Model: <code>{_html.escape(trial.model)}</code>")
    if trial.duration_sec is not None:
        meta.append(f"Duration: {trial.duration_sec:.1f}s")
    if trial.timestamp:
        meta.append(f"At: {_html.escape(trial.timestamp)}")
    if trial.judge_identity_uri:
        meta.append(f"Judge: <code>{_html.escape(trial.judge_identity_uri)}</code>")
    if trial.cogblock_hash:
        meta.append(f"CogBlock: <code>{_html.escape(trial.cogblock_hash)}</code>")
    if meta:
        parts.append(
            f'<p style="font-size:12px;color:var(--text-dim);margin-bottom:10px">'
            f" &nbsp;|&nbsp; ".join(meta) + "</p>"
        )

    # Failures
    if trial.failures:
        parts.append('<h4 style="color:var(--red)">Failures</h4>')
        parts.append('<ul class="failure-list">')
        for f in trial.failures:
            parts.append(f"<li>{_html.escape(f)}</li>")
        parts.append("</ul>")

    # Notes
    if trial.notes:
        parts.append('<h4>Notes</h4>')
        parts.append('<ul class="note-list">')
        for n in trial.notes:
            parts.append(f"<li>{_html.escape(n)}</li>")
        parts.append("</ul>")

    # Tool calls
    if trial.tool_calls:
        parts.append("<h4>Tool calls</h4>")
        for i, tc in enumerate(trial.tool_calls, 1):
            name = tc.get("name", "?")
            args = tc.get("arguments", {})
            result = tc.get("result")
            parts.append(f'<div class="tool-call">')
            parts.append(
                f'{i}. <span class="tool-name">{_html.escape(name)}</span>'
            )
            if args:
                args_str = str(args)
                if len(args_str) > 200:
                    args_str = args_str[:200] + "…"
                parts.append(
                    f' <span style="color:var(--text-dim);font-size:11px">'
                    f'({_html.escape(args_str)})</span>'
                )
            if result is not None:
                result_str = str(result)
                if len(result_str) > 300:
                    result_str = result_str[:300] + "…"
                parts.append(
                    f'<br><span class="tool-result">&nbsp;&nbsp;→ {_html.escape(result_str)}</span>'
                )
            parts.append("</div>")

    # Content
    if trial.content:
        content_preview = trial.content
        if len(content_preview) > 600:
            content_preview = content_preview[:600] + "…"
        parts.append("<h4>Response</h4>")
        parts.append(f'<pre>{_html.escape(content_preview)}</pre>')

    parts.append("</div></details>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Main render entry point
# ---------------------------------------------------------------------------

def render_html(
    trials: list[TrialRecord],
    summary: RunSummary,
    brief_path: Path | None = None,
) -> str:
    """Render a complete two-tab HTML report string.

    Args:
        trials: All TrialRecord objects for this run.
        summary: Aggregate RunSummary.
        brief_path: Optional path to brief.html or brief.md (or their parent dir).
                    Falls back to auto-generated default if None or not found.

    Returns:
        Self-contained HTML string (embed inline CSS, no external dependencies).
    """
    pct = 100.0 * summary.passed / summary.total if summary.total else 0.0
    title = f"Tournament Report — {summary.experiment_id}"

    brief_html = _render_brief(summary, trials, brief_path)

    # Summary stat cards
    stat_cards = f"""
        <div class="stats-grid">
            <div class="card stat-card">
                <div class="stat-value">{summary.total}</div>
                <div class="stat-label">Total trials</div>
            </div>
            <div class="card stat-card">
                <div class="stat-value pass">{summary.passed}</div>
                <div class="stat-label">Passed</div>
            </div>
            <div class="card stat-card">
                <div class="stat-value {'fail' if summary.failed > 0 else 'pass'}">{summary.failed}</div>
                <div class="stat-label">Failed</div>
            </div>
            <div class="card stat-card">
                <div class="stat-value {'pass' if pct >= 80 else 'fail' if pct < 50 else ''}">{pct:.1f}%</div>
                <div class="stat-label">Pass rate</div>
            </div>
        </div>
    """

    # Variant × task matrix (only render if multi-variant)
    has_variants = any(len(t.variant_ids) > 0 for t in trials)
    matrix_section = ""
    if has_variants and trials:
        matrix_section = f"""
            <section>
                <h2>Variant × Task Matrix</h2>
                {_render_matrix_table(trials)}
            </section>
        """

    # Per-trial details
    trial_details = "\n".join(_render_trial_detail(t) for t in trials)
    trials_section = f"""
        <section>
            <h2>Trial Detail ({len(trials)} trials)</h2>
            {trial_details or '<p style="color:var(--text-dim)">No trials.</p>'}
        </section>
    """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_html.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
    <input type="radio" name="tab" id="tab-brief" checked>
    <input type="radio" name="tab" id="tab-data">
    <header>
        <h1>{_html.escape(title)}</h1>
        <div class="subtitle">
            {summary.total} trials &middot; {pct:.1f}% pass &middot;
            target: {_html.escape(summary.target)} &middot;
            run: <code>{_html.escape(summary.run_id)}</code> &middot;
            {_html.escape(summary.started_at)}
        </div>
    </header>
    <div class="tabs">
        <label for="tab-brief">Brief</label>
        <label for="tab-data">Data</label>
    </div>
    <main>
        <div class="pane pane-brief">
            {brief_html}
        </div>
        <div class="pane pane-data">
            <section>
                <h2>Summary</h2>
                {stat_cards}
            </section>
            {matrix_section}
            {trials_section}
        </div>
    </main>
</body>
</html>
"""
