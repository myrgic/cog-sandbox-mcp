"""Tournament runner CLI.

Executes a tournament experiment: loads variant cogdocs, expands the trial
matrix, runs each trial against the inference backend, scores results, emits
CogBlocks (best-effort), and writes the run report.

Usage:
    # Kernel dispatch (default) — goes through cog_dispatch_to_harness:
    python -m evals.tournament.runner --experiment exp-001-anti-pattern-placement \\
        --model gemma4:e4b --dispatch-mode kernel --target laptop-kernel

    # Claude baseline — kernel /v1/chat/completions → claude-code subprocess (Max OAuth):
    python -m evals.tournament.runner --experiment exp-001-anti-pattern-placement \\
        --dispatch-mode claude --target claude-code

    # LM Studio dispatch (legacy comparison path):
    python -m evals.tournament.runner --experiment exp-001-anti-pattern-placement \\
        --dispatch-mode lms --target laptop-lms

Sequential execution: trials run one at a time (Ollama single-thread constraint).
See memory: feedback_ollama_single_thread_constraint.md — the kernel's harness
serializes at Ollama; firing concurrent dispatches would pile up behind the same
single thread, competing with the metabolic ticker and background work.

Phase 2: TD overrides are wired via ChatCompletionsClient (client_chat.py).
Trials with a non-baseline TD variant route to LMS /v1/chat/completions with
per-trial tool-description overrides applied. Baseline TD trials continue
through KernelMCPClient (cog_dispatch_to_harness).

Claude baseline dispatch (--dispatch-mode claude) — ROUTING CONSTRAINT:
The kernel's claude-code provider is agentic-native. It spawns `claude -p`
subprocesses that use Claude's own native MCP context to answer. The tools[]
array sent in /v1/chat/completions is NOT forwarded to the subprocess — Claude
answers from its existing MCP session (coherent, correct answers) without
recording explicit tool_calls in the chat response. This means rubric checks
that require tool_calls[] will always FAIL in claude mode.

Implication: claude-code trial results measure answer quality (content_contains_*
rubrics) but NOT tool-call compliance (expected_tools, first_tool_one_of).

There is NO kernel-side workaround via cog_dispatch_to_harness today.
HarnessDispatcher.DispatchToHarness in cogos-dev/cogos/agent_dispatch.go (lines
113-125) only switches between two model routes: DispatchModel26B (LM Studio
OpenAI-compat) and DispatchModelE4B (local Ollama, default). Unknown model
strings — including "sonnet", "claude-sonnet-4-6", anything else — silently
fall through to e4b. Verified live 2026-04-26: a probe call with model="sonnet"
returned in 1.9s with model_used="e4b" stamped in DispatchResult.stats.

Two paths to actually record Claude tool_calls in the kernel ledger would be:
  (a) Add a claude-code branch to HarnessDispatcher (Go change, kernel rebuild)
      that uses ClaudeCodeProvider with --output-format stream-json and parses
      tool_use events into DispatchToolCallSummary entries, OR
  (b) Have the runner consume claude -p stream-json directly (bypass kernel)
      and emit synthesized ToolCall entries into TrialRecord, then optionally
      persist via cog_emit. This sidesteps the kernel ledger entirely.

Until either lands, --dispatch-mode claude trials must use rubrics that don't
require tool_calls[].
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
from evals.tournament.client_chat import ChatCompletionsClient
from evals.tournament.client_claudecode import ClaudeCodeClient
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


def _is_td_nonbaseline(spec: TrialSpec) -> bool:
    """True when the trial's TD variant is non-baseline (needs ChatCompletionsClient)."""
    return bool(
        spec.tool_description_variant
        and spec.tool_description_variant.id != "td-1-current"
    )


def _run_trial(
    spec: TrialSpec,
    client: LMStudioAgenticClient | KernelMCPClient | ChatCompletionsClient | ClaudeCodeClient,
    model: str,
    plugin_ids: list[str],
    chat_client: ChatCompletionsClient | None = None,
    parametric_mode: bool = False,
) -> tuple[AgenticResult, Verdict]:
    """Execute a single trial spec and return (result, verdict).

    Routing logic (Phase 2):
    - TD non-baseline variant → chat_client (ChatCompletionsClient) with per-trial
      description overrides. If chat_client is None, falls back with a warning.
    - TD baseline or no TD axis + KernelMCPClient → kernel dispatch.
    - LMStudioAgenticClient → legacy LMS plugin path (lms dispatch mode).

    The SP variant is passed as system_prompt to both kernel and chat paths.
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
        content_contains_ci=rubric_data.get("content_contains_ci") or [],
        content_must_not_contain_ci=rubric_data.get("content_must_not_contain_ci") or [],
        content_contains_any_of_ci=rubric_data.get("content_contains_any_of_ci") or [],
        first_tool_one_of=rubric_data.get("first_tool_one_of") or [],
    )
    max_tokens = task_content.get("max_tokens", 1024)

    # Extract SP text for all dispatch paths
    sp_text: str | None = None
    if spec.system_prompt_variant and spec.system_prompt_variant.content:
        sp_text = spec.system_prompt_variant.content

    # Route: non-baseline TD → ChatCompletionsClient (Phase 2)
    if _is_td_nonbaseline(spec):
        if chat_client is None:
            log.warning(
                "Trial %s: TD variant %r requires ChatCompletionsClient but none "
                "was provided. Using baseline tool descriptions. td_wired=False.",
                spec.trial_id,
                spec.tool_description_variant.id if spec.tool_description_variant else "?",
            )
        else:
            # Apply TD overrides from the variant's content dict
            td_overrides: dict[str, str] = {}
            if spec.tool_description_variant and isinstance(
                spec.tool_description_variant.content, dict
            ):
                td_overrides = {
                    k: v
                    for k, v in spec.tool_description_variant.content.items()
                    if isinstance(v, str)
                }
            log.debug(
                "Trial %s: routing to ChatCompletionsClient with %d TD overrides (variant=%s)",
                spec.trial_id,
                len(td_overrides),
                spec.tool_description_variant.id if spec.tool_description_variant else "?",
            )
            result = chat_client.dispatch(
                task=prompt,
                system_prompt=sp_text,
                td_overrides=td_overrides,
                model=model,
                max_tokens=max_tokens,
            )
            verdict = score(rubric, _agentic_to_scorable(result))
            return result, verdict

    # Route: kernel dispatch (baseline TD or no TD axis)
    if isinstance(client, ClaudeCodeClient):
        # Claude baseline — kernel /v1/chat/completions → claude-code subprocess (Max OAuth).
        # No API key; no temperature override; N=1. TD overrides not applied for baseline.
        result = client.dispatch(
            task=prompt,
            system_prompt=sp_text,
            max_tokens=max_tokens,
        )
    elif isinstance(client, KernelMCPClient):
        result = client.dispatch(
            task=prompt,
            system_prompt=sp_text,
            tools=None,  # kernel default tool registry (overridden when no_tools=True)
            model=model,
            iss="tournament",
            sub=spec.variant_ids.get("system_prompt", spec.experiment_id),
            timeout_seconds=max(60, max_tokens // 10),
            no_tools=parametric_mode,
        )
    elif isinstance(client, ChatCompletionsClient):
        # Direct ChatCompletionsClient mode (--dispatch-mode chat, baseline TD)
        result = client.dispatch(
            task=prompt,
            system_prompt=sp_text,
            td_overrides={},  # baseline: no overrides
            model=model,
            max_tokens=max_tokens,
        )
    else:
        # LMS mode: bake SP into the prompt preamble (legacy Phase 1 approach)
        if sp_text:
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
    parametric_mode: bool = False,
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
        parametric_mode=parametric_mode,
    )


def run_experiment(
    experiment_id: str,
    client: LMStudioAgenticClient | KernelMCPClient | ChatCompletionsClient | ClaudeCodeClient,
    model: str,
    plugin_ids: list[str],
    base_url: str,
    save_runs: bool = True,
    emit_cogblocks: bool = True,
    run_store: RunStore | None = None,
    target_override: str | None = None,
    chat_client: ChatCompletionsClient | None = None,
    parametric_mode: bool = False,
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

    mode_tag = " [PARAMETRIC — no tool surface]" if parametric_mode else ""
    print(
        f"==> Tournament: {experiment.title}\n"
        f"    {len(specs)} trials across {len(experiment.task_ids)} tasks\n"
        f"    Model: {model} @ {base_url}\n"
        f"    Target: {effective_target}\n"
        f"    Run ID: {run_id}{mode_tag}"
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
        # td_wired=True when: (a) TD is baseline (no override needed), or
        # (b) TD is non-baseline AND chat_client is available to apply overrides.
        td_wired = not _is_td_nonbaseline(spec) or (
            _is_td_nonbaseline(spec) and chat_client is not None
        )

        try:
            result, verdict = _run_trial(
                spec, client, model, plugin_ids,
                chat_client=chat_client,
                parametric_mode=parametric_mode,
            )
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
            parametric_mode=parametric_mode,
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
        choices=["kernel", "lms", "chat", "claude"],
        default="kernel",
        help=(
            "Dispatch backend. 'kernel' (default): routes via cog_dispatch_to_harness "
            "against http://localhost:6931/mcp — kernel owns inference + tool execution. "
            "'lms': routes via LM Studio /api/v1/chat plugin endpoint (comparison/regression path). "
            "'chat': routes all trials via LMS /v1/chat/completions (TD axis always active). "
            "'claude': routes via kernel /v1/chat/completions with model=sonnet — uses the "
            "host claude-code subprocess provider (Claude Max OAuth, zero incremental cost). "
            "NO Anthropic API key required or used."
        ),
    )
    parser.add_argument(
        "--kernel-url",
        default=os.environ.get("COG_KERNEL_URL", "http://localhost:6931"),
        help="Kernel base URL (kernel dispatch mode only). Default: http://localhost:6931",
    )
    parser.add_argument(
        "--claude-model",
        default=os.environ.get("COG_EVAL_CLAUDE_MODEL", "sonnet"),
        help=(
            "Model name for --dispatch-mode claude. The kernel router resolves "
            "this to the claude-code subprocess provider (Max OAuth, no API key). "
            "Common values: 'sonnet', 'haiku'. Default: sonnet."
        ),
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
        "--no-tools",
        action="store_true",
        default=False,
        dest="no_tools",
        help=(
            "Parametric dispatch mode: send tools=[] to the harness so the model has no "
            "tool surface. A directive is prepended to the system prompt: "
            "'Answer directly from your knowledge. Do not attempt tool calls. If you don't "
            "know, say so.' TrialRecord.parametric_mode is set to True. "
            "Only valid with --dispatch-mode kernel. "
            "Use to evaluate (model, harness, no-tool-surface) tuples for tasks that "
            "don't require tool-assisted retrieval."
        ),
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

    token = os.environ.get("LMS_API_TOKEN", "").strip()

    # Build the inference client
    chat_client: ChatCompletionsClient | None = None

    # --no-tools is only meaningful for kernel mode (harness-mediated dispatch).
    if args.no_tools and args.dispatch_mode != "kernel":
        print(
            f"error: --no-tools is only supported with --dispatch-mode kernel "
            f"(got {args.dispatch_mode!r}). lms/chat/claude routes do not go through "
            "cog_dispatch_to_harness and cannot be given an empty tool allowlist this way.",
            file=sys.stderr,
        )
        return 2

    if args.dispatch_mode == "claude":
        # Claude baseline: kernel /v1/chat/completions → claude-code subprocess.
        # Uses host Claude Max subscription via OAuth keychain — NO API key needed.
        # No LMS_API_TOKEN check — the kernel handles auth internally.
        model = "sonnet"  # kernel router resolves this to claude-code provider
        effective_base_url = args.kernel_url
        print(
            f"==> Dispatch mode: claude (kernel claude-code provider @ {effective_base_url})"
        )
        print(
            "    Auth: Claude Max OAuth (keychain) — no ANTHROPIC_API_KEY used"
        )
        print(f"    Model: {args.claude_model}")
        client: (
            LMStudioAgenticClient | KernelMCPClient | ChatCompletionsClient | ClaudeCodeClient
        ) = ClaudeCodeClient(
            kernel_url=effective_base_url,
            timeout=180.0,
            model=args.claude_model,
        )
    elif args.dispatch_mode == "kernel":
        # Kernel dispatch: cog_dispatch_to_harness via MCP session
        # Model default changes to e4b for kernel mode (Ollama, not LMS)
        model = args.model if args.model != "google/gemma-4-26b-a4b" else os.environ.get("COG_EVAL_MODEL", "e4b")
        effective_base_url = args.kernel_url
        lms_model = os.environ.get("LMS_CHAT_MODEL", "f29de68cb284ca208446e647b339569935025ef3")
        mode_label = "kernel (parametric — no tool surface)" if args.no_tools else "kernel"
        print(f"==> Dispatch mode: {mode_label} @ {effective_base_url}")
        client = KernelMCPClient(
            base_url=effective_base_url,
            timeout=180.0,
        )
        # Phase 2: spin up ChatCompletionsClient for TD non-baseline trials
        if token:
            print(f"==> TD axis: ChatCompletionsClient @ {args.base_url} (model={lms_model})")
            chat_client = ChatCompletionsClient(
                base_url=args.base_url,
                api_token=token,
                kernel_url=effective_base_url,
                timeout=180.0,
            )
        else:
            print(
                "==> Warning: LMS_API_TOKEN not set — TD non-baseline trials will "
                "fall back to baseline descriptions (td_wired=False).",
                file=sys.stderr,
            )
    elif args.dispatch_mode == "chat":
        # Chat completions dispatch: all trials via LMS /v1/chat/completions
        model = args.model if args.model != "google/gemma-4-26b-a4b" else os.environ.get(
            "LMS_CHAT_MODEL", "f29de68cb284ca208446e647b339569935025ef3"
        )
        effective_base_url = args.base_url
        if not token:
            print(
                "error: LMS_API_TOKEN is empty. Paste your LM Studio API token into "
                "evals/.env (or export it) and re-run.",
                file=sys.stderr,
            )
            return 2
        print(f"==> Dispatch mode: chat @ {effective_base_url}")
        client = ChatCompletionsClient(
            base_url=effective_base_url,
            api_token=token,
            kernel_url=args.kernel_url,
            timeout=180.0,
        )
        # chat_client IS client for this mode — no separate instance needed
        chat_client = client  # type: ignore[assignment]
    else:
        # LMS dispatch: legacy /api/v1/chat plugin path
        model = args.model
        effective_base_url = args.base_url
        if not token:
            print(
                "error: LMS_API_TOKEN is empty. Paste your LM Studio API token into "
                "evals/.env (or export it) and re-run.",
                file=sys.stderr,
            )
            return 2
        print(f"==> Dispatch mode: lms @ {effective_base_url}")
        client = LMStudioAgenticClient(base_url=effective_base_url, api_token=token)

    clients_to_close: list[Any] = [client]
    if chat_client is not None and chat_client is not client:
        clients_to_close.append(chat_client)

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
            chat_client=chat_client,
            parametric_mode=args.no_tools,
        )
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1
    finally:
        for c in clients_to_close:
            c.close()

    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
