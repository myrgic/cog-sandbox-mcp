"""Tournament runner CLI.

Executes a tournament experiment: loads variant cogdocs, expands the trial
matrix, runs each trial against the inference backend, scores results, emits
CogBlocks (best-effort), and writes the run report.

Usage:
    # Kernel dispatch (default) — goes through cog_dispatch_to_harness:
    python -m evals.tournament.runner --experiment exp-001-anti-pattern-placement \\
        --model gemma4:e4b --dispatch-mode kernel --target laptop-kernel

    # LM Studio dispatch (legacy comparison path):
    python -m evals.tournament.runner --experiment exp-001-anti-pattern-placement \\
        --dispatch-mode lms --target laptop-lms

Sequential execution: trials run one at a time (Ollama single-thread constraint).
See memory: feedback_ollama_single_thread_constraint.md — the kernel's harness
serializes at Ollama; firing concurrent dispatches would pile up behind the same
single thread, competing with the metabolic ticker and background work.

Phase 1 limitation: TD overrides are not yet wired into dispatch — only SP
variants are varied. A warning is logged per trial when td_wired=False.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Add parent to sys.path when run as __main__ to enable relative imports
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from evals.harness.cases import Case
from evals.harness.client import AgenticResult, LMStudioAgenticClient, ToolCall
from evals.harness.scoring import Verdict, score
from evals.tournament.client_kernel import KernelMCPClient
from evals.reports.data import RunSummary, TrialRecord, save_trial_jsonl
from evals.reports.html import render_html
from evals.reports.md import render_markdown
from evals.tournament.compare import build_scorecard, compute_deltas
from evals.tournament.matrix import (
    Experiment,
    TrialSpec,
    expand_matrix,
    load_experiment_from_cogdoc,
)
from evals.tournament.persist import (
    RunStore,
    emit_experiment_cogblock,
    emit_trial_cogblock,
)
from evals.tournament.variants import load_variants

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Environment / dotenv helpers (mirrors evals/runner.py pattern)
# ---------------------------------------------------------------------------

def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


def _default_env_file() -> Path:
    return Path(__file__).parent.parent / ".env"


# ---------------------------------------------------------------------------
# Runner logic
# ---------------------------------------------------------------------------

def _agentic_to_scorable(result: AgenticResult) -> Any:
    """Shim AgenticResult so scoring.score() works unchanged (same as runner.py)."""
    class _Shim:
        pass
    shim = _Shim()
    shim.content = result.content
    shim.tool_calls = result.tool_calls
    shim.finish_reason = (
        "tool_calls" if result.tool_calls and not result.content else "stop"
    )
    return shim


def _run_trial(
    spec: TrialSpec,
    client: LMStudioAgenticClient | KernelMCPClient,
    model: str,
    plugin_ids: list[str],
) -> tuple[AgenticResult, Verdict]:
    """Execute a single trial spec and return (result, verdict).

    Routes to either LMStudioAgenticClient.run() or KernelMCPClient.dispatch()
    based on the client type. The SP variant is passed as a separate system_prompt
    override in kernel mode (DispatchRequest.SystemPrompt per agent_dispatch.go:83)
    rather than baked into the prompt preamble.
    """
    # Build the Case from the task variant
    task_content = spec.task_variant.content or {}
    prompt = task_content.get("prompt", "")
    from evals.harness.cases import Rubric
    rubric_data = task_content.get("rubric") or {}
    rubric = Rubric(
        expected_tools=rubric_data.get("expected_tools") or [],
        expected_tools_any_of=rubric_data.get("expected_tools_any_of") or [],
        forbidden_tools=rubric_data.get("forbidden_tools") or [],
        content_contains=rubric_data.get("content_contains") or [],
        content_must_not_contain=rubric_data.get("content_must_not_contain") or [],
        first_tool_one_of=rubric_data.get("first_tool_one_of") or [],
    )
    max_tokens = task_content.get("max_tokens", 1024)

    # Phase 1: TD variant is documented but not yet wired (Phase 2 work)
    if spec.tool_description_variant and spec.tool_description_variant.id != "td-1-current":
        log.warning(
            "Trial %s: TD variant %r is not yet wired into dispatch (Phase 2). "
            "Using baseline tool descriptions. td_wired=False.",
            spec.trial_id,
            spec.tool_description_variant.id,
        )

    # Dispatch — split by client type
    if isinstance(client, KernelMCPClient):
        # Kernel mode: system_prompt passed as DispatchRequest.SystemPrompt override.
        # The prompt itself is the task content (no [system]/[/system] preamble needed).
        sp_text: str | None = None
        if spec.system_prompt_variant and spec.system_prompt_variant.content:
            sp_text = spec.system_prompt_variant.content

        result = client.dispatch(
            task=prompt,
            system_prompt=sp_text,
            tools=None,  # kernel default tool registry
            model=model,
            iss="tournament",
            sub=spec.variant_ids.get("system_prompt", spec.experiment_id),
            timeout_seconds=max(60, max_tokens // 10),
        )
    else:
        # LMS mode: bake SP into the prompt preamble (original Phase 1 approach)
        if spec.system_prompt_variant and spec.system_prompt_variant.content:
            sp_text = spec.system_prompt_variant.content
            effective_prompt = f"[system]\n{sp_text}\n[/system]\n\n{prompt}"
        else:
            effective_prompt = prompt

        result = client.run(
            model=model,
            prompt=effective_prompt,
            plugin_ids=plugin_ids,
            max_tokens=max_tokens,
        )

    verdict = score(rubric, _agentic_to_scorable(result))
    return result, verdict


def _make_trial_record(
    spec: TrialSpec,
    result: AgenticResult,
    verdict: Verdict,
    model: str,
    base_url: str,
    timestamp: str,
    duration_sec: float,
    td_wired: bool,
) -> TrialRecord:
    """Build a TrialRecord from the trial components."""
    tool_calls_data = [
        {"name": tc.name, "arguments": tc.arguments, "result": tc.result}
        for tc in result.tool_calls
    ]
    return TrialRecord(
        trial_id=spec.trial_id,
        experiment_id=spec.experiment_id,
        variant_ids=spec.variant_ids,
        task_id=spec.task_variant.id,
        target=spec.target,
        passed=verdict.passed,
        failures=verdict.failures,
        notes=verdict.notes,
        tool_calls=tool_calls_data,
        content=result.content,
        reasoning=result.reasoning,
        duration_sec=duration_sec,
        timestamp=timestamp,
        model=model,
        base_url=base_url,
        judge_identity_uri=None,  # Set by caller for judge-required tasks
        td_wired=td_wired,
    )


def run_experiment(
    experiment_id: str,
    client: LMStudioAgenticClient | KernelMCPClient,
    model: str,
    plugin_ids: list[str],
    base_url: str,
    save_runs: bool = True,
    emit_cogblocks: bool = True,
    run_store: RunStore | None = None,
    target_override: str | None = None,
) -> tuple[list[TrialRecord], RunSummary]:
    """Run all trials for an experiment. Returns (trials, summary)."""
    # Load variants and experiment
    variants_by_id = load_variants()
    experiment = load_experiment_from_cogdoc(experiment_id, variants_by_id)
    if experiment is None:
        raise ValueError(f"Experiment {experiment_id!r} not found")

    specs = expand_matrix(experiment, variants_by_id)
    if not specs:
        raise ValueError(f"Experiment {experiment_id!r} expanded to zero trial specs")

    # Apply --target override if provided (overrides experiment cogdoc's target field)
    effective_target = target_override or experiment.target
    if target_override:
        specs = [
            TrialSpec(
                trial_id=s.trial_id,
                experiment_id=s.experiment_id,
                task_variant=s.task_variant,
                variant_ids=s.variant_ids,
                system_prompt_variant=s.system_prompt_variant,
                tool_description_variant=s.tool_description_variant,
                target=target_override,
            )
            for s in specs
        ]

    ts_start = datetime.now(timezone.utc)
    ts_str = ts_start.strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{experiment_id}_run_{ts_str}"

    store = run_store or RunStore()
    run_dir = store.run_dir(experiment_id, ts_str) if save_runs else None

    print(
        f"==> Tournament: {experiment.title}\n"
        f"    {len(specs)} trials across {len(experiment.task_ids)} tasks\n"
        f"    Model: {model} @ {base_url}\n"
        f"    Target: {effective_target}\n"
        f"    Run ID: {run_id}"
    )

    trials: list[TrialRecord] = []
    passed = failed = 0

    for i, spec in enumerate(specs, 1):
        sp_id = spec.variant_ids.get("system_prompt", "none")
        td_id = spec.variant_ids.get("tool_description", "none")
        task_id = spec.task_variant.id
        print(f"  [{i}/{len(specs)}] sp={sp_id} td={td_id} task={task_id} ...", end=" ", flush=True)

        ts_trial = datetime.now(timezone.utc).isoformat()
        t0 = time.monotonic()
        # td_wired=False when TD variant is non-baseline (not yet implemented in Phase 1)
        td_wired = not (
            spec.tool_description_variant
            and spec.tool_description_variant.id != "td-1-current"
        )

        try:
            result, verdict = _run_trial(spec, client, model, plugin_ids)
        except Exception as e:
            elapsed = time.monotonic() - t0
            log.warning("Trial %s failed with exception: %s", spec.trial_id, e)
            print(f"ERROR: {type(e).__name__}: {e}")
            # Build a failed record; preserve td_wired from above (exception ≠ unwired)
            from evals.harness.scoring import Verdict as V
            verdict = V(passed=False, failures=[f"Exception: {type(e).__name__}: {e}"])
            from evals.harness.client import AgenticResult as AR
            result = AR(
                content="", tool_calls=[], reasoning="",
                output_types=[], stats={}, raw={},
            )

        elapsed = time.monotonic() - t0
        label = "PASS" if verdict.passed else "FAIL"
        print(f"{label} ({elapsed:.1f}s)")

        if not verdict.passed:
            for f in verdict.failures:
                print(f"      - {f}")
            failed += 1
        else:
            passed += 1

        record = _make_trial_record(
            spec=spec,
            result=result,
            verdict=verdict,
            model=model,
            base_url=base_url,
            timestamp=ts_trial,
            duration_sec=elapsed,
            td_wired=td_wired,
        )

        # Emit CogBlock (best-effort)
        if emit_cogblocks:
            block_hash = emit_trial_cogblock(record)
            if block_hash:
                record.cogblock_hash = block_hash
            # If emission failed, cogblock_hash stays None — trial still saved

        trials.append(record)

        # Write to JSONL incrementally (streaming safety)
        if run_dir:
            store.save_trial(record, run_dir)

    # Summary
    ts_end = datetime.now(timezone.utc)
    summary = RunSummary(
        experiment_id=experiment_id,
        run_id=run_id,
        started_at=ts_start.isoformat(),
        ended_at=ts_end.isoformat(),
        total=len(trials),
        passed=passed,
        failed=failed,
        target=effective_target,
        model=model,
    )

    if run_dir:
        store.save_summary(summary, run_dir)

    # Emit experiment cogblock (best-effort)
    if emit_cogblocks:
        emit_experiment_cogblock(trials, summary)

    # Render report
    if run_dir:
        html_out = run_dir / "report.html"
        html_content = render_html(trials, summary, brief_path=run_dir)
        html_out.write_text(html_content, encoding="utf-8")

        md_out = run_dir / "report.md"
        md_content = render_markdown(trials, summary)
        md_out.write_text(md_content, encoding="utf-8")

        print(f"\n==> Report: {html_out}")

    print(f"\n==> {passed} passed, {failed} failed ({len(trials)} total)")

    # Scorecard + deltas
    scorecard = build_scorecard(trials)
    baseline_sp = experiment.variant_axes.get("system_prompt", [None])[0] or "unknown-sp"
    baseline_td = experiment.variant_axes.get("tool_description", [None])[0] or "td-1-current"
    baseline_key = f"{baseline_sp} / {baseline_td}"

    deltas = compute_deltas(scorecard, baseline_key)
    if deltas:
        print(f"\n==> Deltas vs baseline ({baseline_key}):")
        for d in deltas:
            sign = "+" if d.delta > 0 else ""
            bl_pct = f"{d.baseline_pass_rate * 100:.0f}%" if d.baseline_pass_rate is not None else "?"
            v_pct = f"{d.variant_pass_rate * 100:.0f}%" if d.variant_pass_rate is not None else "?"
            print(f"     {d.variant_key}: {sign}{d.delta * 100:.0f}pp  ({bl_pct} → {v_pct})")

    return trials, summary


def main(argv: list[str] | None = None) -> int:
    _load_dotenv(_default_env_file())

    parser = argparse.ArgumentParser(prog="evals.tournament.runner")
    parser.add_argument(
        "--experiment",
        required=True,
        help="Experiment ID — e.g. exp-001-anti-pattern-placement",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("COG_EVAL_MODEL", "google/gemma-4-26b-a4b"),
        help="Model identifier. Kernel mode: 'gemma4:e4b'. LMS mode: 'google/gemma-4-26b-a4b'.",
    )
    parser.add_argument(
        "--dispatch-mode",
        choices=["kernel", "lms"],
        default="kernel",
        help=(
            "Dispatch backend. 'kernel' (default): routes via cog_dispatch_to_harness "
            "against http://localhost:6931/mcp — kernel owns inference + tool execution. "
            "'lms': routes via LM Studio /api/v1/chat plugin endpoint (comparison/regression path)."
        ),
    )
    parser.add_argument(
        "--kernel-url",
        default=os.environ.get("COG_KERNEL_URL", "http://localhost:6931"),
        help="Kernel base URL (kernel dispatch mode only). Default: http://localhost:6931",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("LMS_API_BASE_URL", "http://localhost:1234"),
        help="LM Studio base URL (lms dispatch mode only). Default: http://localhost:1234",
    )
    parser.add_argument(
        "--plugin-id",
        action="append",
        default=None,
        help="MCP plugin identifier for LMS mode (can repeat). Default: mcp/cog-sandbox",
    )
    parser.add_argument(
        "--target",
        default=None,
        help="Override target name in trial records (default: from experiment cogdoc)",
    )
    parser.add_argument(
        "--save-runs",
        action="store_true",
        default=True,
        help="Write results.jsonl + report.html to evals/runs/ (default: on)",
    )
    parser.add_argument(
        "--no-save-runs",
        action="store_false",
        dest="save_runs",
    )
    parser.add_argument(
        "--no-cogblocks",
        action="store_true",
        default=False,
        help="Skip CogBlock emission. Default: off (emit CogBlocks when kernel is reachable).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    plugin_ids = args.plugin_id or [os.environ.get("COG_EVAL_PLUGIN_ID", "mcp/cog-sandbox")]

    # Build the inference client
    if args.dispatch_mode == "kernel":
        # Kernel dispatch: cog_dispatch_to_harness via MCP session
        # Model default changes to e4b for kernel mode (Ollama, not LMS)
        model = args.model if args.model != "google/gemma-4-26b-a4b" else os.environ.get("COG_EVAL_MODEL", "e4b")
        effective_base_url = args.kernel_url
        print(f"==> Dispatch mode: kernel @ {effective_base_url}")
        client: LMStudioAgenticClient | KernelMCPClient = KernelMCPClient(
            base_url=effective_base_url,
            timeout=180.0,
        )
    else:
        # LMS dispatch: legacy /api/v1/chat plugin path
        model = args.model
        effective_base_url = args.base_url
        token = os.environ.get("LMS_API_TOKEN", "").strip()
        if not token:
            print(
                "error: LMS_API_TOKEN is empty. Paste your LM Studio API token into "
                "evals/.env (or export it) and re-run.",
                file=sys.stderr,
            )
            return 2
        print(f"==> Dispatch mode: lms @ {effective_base_url}")
        client = LMStudioAgenticClient(base_url=effective_base_url, api_token=token)

    try:
        _, summary = run_experiment(
            experiment_id=args.experiment,
            client=client,
            model=model,
            plugin_ids=plugin_ids,
            base_url=effective_base_url,
            save_runs=args.save_runs,
            emit_cogblocks=not args.no_cogblocks,
            target_override=args.target,
        )
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1
    finally:
        client.close()

    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
