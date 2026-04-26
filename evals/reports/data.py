"""Trial record dataclasses and JSONL persistence helpers.

TrialRecord is the shared contract across reports/ and tournament/.
Both evals/runner.py (no variants — single variant) and the tournament
runner (multi-variant matrix) produce TrialRecord lists; both can be
passed to render_html / render_markdown.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TrialRecord:
    """One execution of a task variant under a specific variant configuration."""

    trial_id: str
    """Unique identifier for this trial — e.g. '{experiment_id}_{task_id}_{ts}'."""

    experiment_id: str
    """Experiment this trial belongs to — e.g. 'exp-001-anti-pattern-placement'."""

    variant_ids: dict[str, str]
    """Axis → variant_id mapping — e.g. {'system_prompt': 'sp-1-production', 'tool_description': 'td-1-current'}."""

    task_id: str
    """Task variant ID — e.g. 'task-1-state-probe'."""

    target: str
    """Inference target — e.g. 'laptop-lms', 'desktop-lms', 'ollama-local'."""

    passed: bool
    """Whether the trial passed the rubric."""

    failures: list[str] = field(default_factory=list)
    """Rubric failure messages from scoring.Verdict.failures."""

    notes: list[str] = field(default_factory=list)
    """Rubric notes from scoring.Verdict.notes (tool_calls list, finish_reason, etc.)."""

    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    """Sequence of tool calls made. Each is {'name': str, 'arguments': dict, 'result': str | None}."""

    content: str = ""
    """Final assistant message text."""

    reasoning: str = ""
    """Concatenated reasoning blocks (may be empty if model has no reasoning tokens)."""

    duration_sec: float | None = None
    """Wall-clock time for this trial in seconds."""

    timestamp: str = ""
    """ISO 8601 timestamp when trial started — e.g. '2026-04-25T14:30:00Z'."""

    model: str = ""
    """Model identifier used for inference — e.g. 'google/gemma-4-26b-a4b'."""

    base_url: str = ""
    """Inference endpoint base URL — e.g. 'http://localhost:1234'."""

    judge_identity_uri: str | None = None
    """URI of the judge identity per ADR-014 — e.g. 'cog://agents/identities/cog'.
    Set for judge-required tasks; None for auto-graded trials."""

    cogblock_hash: str | None = None
    """Content-addressable hash from the kernel BlobStore after emit_trial_cogblock().
    None until persist.py sets it post-emission."""

    td_wired: bool = False
    """Phase 1 flag: True once client_chat.py (Phase 2) is wired and TD overrides
    are actually applied during dispatch. False in Phase 1 — TD variants are documented
    but tool descriptions are not overridden at dispatch time."""


@dataclass
class RunSummary:
    """Aggregate summary for a complete experiment run."""

    experiment_id: str
    run_id: str
    started_at: str
    """ISO 8601 timestamp."""

    ended_at: str
    """ISO 8601 timestamp."""

    total: int
    passed: int
    failed: int
    target: str
    model: str = ""


def save_trial_jsonl(record: TrialRecord, path: Path) -> None:
    """Append a TrialRecord to a JSONL file (streaming-safe, creates file if absent)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(record), default=str) + "\n")


def load_trials_jsonl(path: Path) -> list[TrialRecord]:
    """Load all TrialRecord objects from a JSONL file. Skips malformed lines."""
    records: list[TrialRecord] = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            records.append(TrialRecord(**data))
        except (json.JSONDecodeError, TypeError):
            pass
    return records
