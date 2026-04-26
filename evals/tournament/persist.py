"""CogBlock emission and filesystem run store.

RunStore: manages evals/runs/{experiment_id}_run_{ts}/ directories.
emit_trial_cogblock: POSTs trial record to kernel /v1/bus/send.
emit_experiment_cogblock: Merkle-rolls trial hashes, emits experiment summary.

Phase 1: CogBlock emission is best-effort — if the kernel is not reachable,
the trial is still saved to JSONL. The cogblock_hash field in TrialRecord
is set on successful emission and updated in the JSONL file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

from evals.reports.data import RunSummary, TrialRecord, save_trial_jsonl

log = logging.getLogger(__name__)

# Default kernel URL; override via COG_KERNEL_URL env var.
_DEFAULT_KERNEL_URL = "http://localhost:6931"

# Default runs root relative to this file's package location.
_DEFAULT_RUNS_ROOT = Path(__file__).parent.parent / "runs"


class RunStore:
    """Manages the filesystem run directory and JSONL journal."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or _DEFAULT_RUNS_ROOT

    def run_dir(self, experiment_id: str, ts: str) -> Path:
        name = f"{experiment_id}_run_{ts}"
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def results_path(self, run_dir: Path) -> Path:
        return run_dir / "results.jsonl"

    def save_trial(self, record: TrialRecord, run_dir: Path) -> None:
        """Append a trial record to the run's results.jsonl."""
        save_trial_jsonl(record, self.results_path(run_dir))
        log.debug("Saved trial %s to %s", record.trial_id, self.results_path(run_dir))

    def save_summary(self, summary: RunSummary, run_dir: Path) -> None:
        """Write the run summary as summary.json."""
        path = run_dir / "summary.json"
        path.write_text(json.dumps(asdict(summary), indent=2, default=str), encoding="utf-8")
        log.info("Saved summary to %s", path)


def _kernel_url() -> str:
    return os.environ.get("COG_KERNEL_URL", _DEFAULT_KERNEL_URL).rstrip("/")


def _trial_payload(record: TrialRecord) -> dict:
    """Build the CogBlock payload for a trial record."""
    return {
        "type": "tournament.trial.v1",
        "trial": asdict(record),
        "experiment_id": record.experiment_id,
    }


def _compute_hash(payload: dict) -> str:
    """SHA-256 of canonical JSON (sorted keys)."""
    canonical = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _bus_send(bus_id: str, event_type: str, from_: str, payload: dict) -> str | None:
    """POST one event to /v1/bus/send. Returns the event hash from the kernel.

    Kernel busSendRequest shape (serve_bus.go):
        bus_id   string  — target bus channel
        from     string  — sender identity
        message  string  — event body (we send JSON-serialized payload)
        type     string  — event type discriminator
    Response: {ok: bool, seq: int, hash: string}
    """
    message_str = json.dumps(payload, default=str)
    body = {
        "bus_id": bus_id,
        "from": from_,
        "message": message_str,
        "type": event_type,
    }
    try:
        resp = httpx.post(
            f"{_kernel_url()}/v1/bus/send",
            json=body,
            timeout=15.0,
        )
        if resp.status_code in (200, 201, 202, 204):
            try:
                data = resp.json()
                return str(data.get("hash") or "")
            except Exception:
                return ""
        else:
            log.warning(
                "_bus_send: kernel %s returned %d: %s",
                _kernel_url(),
                resp.status_code,
                resp.text[:200],
            )
            return None
    except Exception as e:
        log.warning("_bus_send: failed to reach kernel at %s: %s", _kernel_url(), e)
        return None


def emit_trial_cogblock(record: TrialRecord) -> str | None:
    """POST a tournament.trial.v1 CogBlock to the kernel bus.

    Emits to bus_tournament with event type 'tournament.trial.v1'.
    The From field is 'tournament/{experiment_id}/{variant_key}' for
    attributability per the Phase B spec.

    Returns the event hash from the kernel if successful, else a local SHA-256
    digest of the payload (so cogblock_hash is always set on success-ish).
    Returns None on emission failure — caller continues without failing the trial.
    """
    payload = _trial_payload(record)
    local_hash = _compute_hash(payload)

    variant_key = "+".join(
        f"{k}={v}" for k, v in sorted(record.variant_ids.items())
    ) or "default"
    from_ = f"tournament/{record.experiment_id}/{variant_key}"

    result = _bus_send(
        bus_id="bus_tournament",
        event_type="tournament.trial.v1",
        from_=from_,
        payload=payload,
    )
    if result is None:
        return None
    # Kernel returns the event hash; fall back to local hash if kernel returned ""
    return result or local_hash


def emit_experiment_cogblock(
    trials: list[TrialRecord],
    summary: RunSummary,
) -> str | None:
    """Emit a tournament.experiment.v1 CogBlock with a Merkle root of trial hashes.

    Fired once at end of run. Provides a single attributable event that anchors
    the full run on bus_tournament — phase B verification gate per the substrate plan.

    Returns the event hash from the kernel if successful, else the local Merkle root.
    Returns None on emission failure.
    """
    # Collect trial hashes (use cogblock_hash if set, else recompute from payload)
    trial_hashes: list[str] = []
    for t in trials:
        if t.cogblock_hash:
            trial_hashes.append(t.cogblock_hash)
        else:
            trial_hashes.append(_compute_hash(_trial_payload(t)))

    # Merkle root: SHA-256 of sorted trial hashes joined by newline
    combined = "\n".join(sorted(trial_hashes))
    merkle_root = hashlib.sha256(combined.encode("utf-8")).hexdigest()

    payload = {
        "type": "tournament.experiment.v1",
        "experiment_id": summary.experiment_id,
        "run_id": summary.run_id,
        "total": summary.total,
        "passed": summary.passed,
        "failed": summary.failed,
        "target": summary.target,
        "model": summary.model,
        "started_at": summary.started_at,
        "ended_at": summary.ended_at,
        "trial_hashes": trial_hashes,
        "merkle_root": merkle_root,
    }

    result = _bus_send(
        bus_id="bus_tournament",
        event_type="tournament.experiment.v1",
        from_=f"tournament/{summary.experiment_id}",
        payload=payload,
    )
    if result is None:
        return None
    return result or merkle_root
