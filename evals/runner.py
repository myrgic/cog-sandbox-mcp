"""CLI runner for the eval harness.

Flow per case:
  1. POST the case's prompt to LM Studio's /api/v1/chat with the MCP plugin
     listed under 'integrations'. LM Studio handles inference AND the full
     multi-turn tool-execution loop using the loaded MCP plugin.
  2. Parse the returned output array (reasoning / function_call / message items).
  3. Score against the case's rubric.
  4. Aggregate and report.

Requires an LM Studio API token (since plugin use over the API is gated by
'Require Authentication'). Set LMS_API_TOKEN in evals/.env or the environment.

Usage:
    python -m evals.runner [--model MODEL] [--cases DIR] [--filter TAG]
                           [--plugin-id PLUGIN_ID] [--save-trace PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import datetime

from evals.harness.cases import Case, load_cases
from evals.harness.client import AgenticResult, LMStudioAgenticClient, ToolCall
from evals.harness.scoring import Verdict, score
from evals.reports.data import RunSummary, TrialRecord
from evals.reports.html import render_html


def _default_cases_dir() -> Path:
    return Path(__file__).parent / "cases"


def _default_env_file() -> Path:
    return Path(__file__).parent / ".env"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, # comments. Does not overwrite set vars."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
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


def _agentic_to_scorable(result: AgenticResult) -> dict[str, Any]:
    """Shape the agentic result so scoring.score() (which expects ChatResult-like
    attributes) works unchanged."""
    # scoring.py looks for: .content, .tool_calls (objects with .name), .finish_reason
    class _Shim:
        pass

    shim = _Shim()
    shim.content = result.content
    shim.tool_calls = result.tool_calls
    shim.finish_reason = (
        "tool_calls" if result.tool_calls and not result.content else "stop"
    )
    return shim


def run_case(case: Case, client: LMStudioAgenticClient, model: str, plugin_ids: list[str]) -> tuple[AgenticResult, Verdict]:
    # System prompts currently go into the user prompt as a preamble — /api/v1/chat
    # doesn't take a separate system field in the input-string form. Friction signal
    # if we need explicit system handling.
    prompt = case.prompt
    if case.system_prompt:
        prompt = f"[system]\n{case.system_prompt}\n[/system]\n\n{case.prompt}"
    result = client.run(
        model=model,
        prompt=prompt,
        plugin_ids=plugin_ids,
        temperature=case.temperature,
        max_tokens=case.max_tokens,
    )
    verdict = score(case.rubric, _agentic_to_scorable(result))
    return result, verdict


def main(argv: list[str] | None = None) -> int:
    _load_dotenv(_default_env_file())

    parser = argparse.ArgumentParser(prog="evals.runner")
    parser.add_argument("--model", default=os.environ.get("COG_EVAL_MODEL", "google/gemma-4-26b-a4b"))
    parser.add_argument(
        "--base-url",
        default=os.environ.get("LMS_API_BASE_URL", "http://localhost:1234"),
    )
    parser.add_argument("--cases", type=Path, default=_default_cases_dir())
    parser.add_argument("--filter", help="Only run cases whose name contains this substring")
    parser.add_argument("--tag", help="Only run cases with this tag")
    parser.add_argument(
        "--plugin-id",
        action="append",
        default=None,
        help="MCP plugin identifier (can repeat). Default: mcp/cog-sandbox",
    )
    parser.add_argument("--save-trace", type=Path, help="JSONL output path for full per-case traces")
    parser.add_argument("--html-report", type=Path, help="Write a self-contained HTML report to this path after the run")
    args = parser.parse_args(argv)

    plugin_ids = args.plugin_id or [os.environ.get("COG_EVAL_PLUGIN_ID", "mcp/cog-sandbox")]

    token = os.environ.get("LMS_API_TOKEN", "").strip()
    if not token:
        print(
            "error: LMS_API_TOKEN is empty. Paste your LM Studio API token into "
            "evals/.env (or export it) and re-run.",
            file=sys.stderr,
        )
        return 2

    cases = load_cases(args.cases)
    if args.filter:
        cases = [c for c in cases if args.filter in c.name]
    if args.tag:
        cases = [c for c in cases if args.tag in c.tags]
    if not cases:
        print("no cases matched", file=sys.stderr)
        return 2

    print(f"==> running {len(cases)} case(s) against {args.model} @ {args.base_url}")
    print(f"    plugins: {plugin_ids}")

    client = LMStudioAgenticClient(base_url=args.base_url, api_token=token)
    trace_fh = args.save_trace.open("w", encoding="utf-8") if args.save_trace else None

    run_started_at = datetime.datetime.now(datetime.timezone.utc)
    run_id = run_started_at.strftime("run-%Y%m%dT%H%M%SZ")

    passed = 0
    failed = 0
    trials: list[TrialRecord] = []
    try:
        for case in cases:
            trial_ts = datetime.datetime.now(datetime.timezone.utc)
            try:
                result, verdict = run_case(case, client, args.model, plugin_ids)
            except Exception as e:
                print(f"  [FAIL]{case.name}  ERROR: {type(e).__name__}: {e}")
                failed += 1
                # Record a failed trial with no result data so the report is complete.
                trials.append(TrialRecord(
                    trial_id=f"{run_id}_{case.name}",
                    experiment_id="evals-runner",
                    variant_ids={},
                    task_id=case.name,
                    target=args.base_url,
                    passed=False,
                    failures=[f"{type(e).__name__}: {e}"],
                    notes=[],
                    tool_calls=[],
                    content="",
                    reasoning="",
                    timestamp=trial_ts.isoformat(),
                    model=args.model,
                    base_url=args.base_url,
                    judge_identity_uri=None,
                    cogblock_hash=None,
                    td_wired=True,
                ))
                continue
            label = "[PASS]" if verdict.passed else "[FAIL]"
            print(f"  {label}{case.name}")
            if not verdict.passed:
                for f in verdict.failures:
                    print(f"      -{f}")
                for n in verdict.notes:
                    print(f"      *{n}")
                failed += 1
            else:
                passed += 1
            trials.append(TrialRecord(
                trial_id=f"{run_id}_{case.name}",
                experiment_id="evals-runner",
                variant_ids={},
                task_id=case.name,
                target=args.base_url,
                passed=verdict.passed,
                failures=verdict.failures,
                notes=verdict.notes,
                tool_calls=[
                    {"name": tc.name, "arguments": tc.arguments, "result": tc.result}
                    for tc in result.tool_calls
                ],
                content=result.content,
                reasoning=result.reasoning,
                timestamp=trial_ts.isoformat(),
                model=args.model,
                base_url=args.base_url,
                judge_identity_uri=None,
                cogblock_hash=None,
                td_wired=True,
            ))
            if trace_fh:
                trace_fh.write(
                    json.dumps(
                        {
                            "case": case.name,
                            "passed": verdict.passed,
                            "failures": verdict.failures,
                            "notes": verdict.notes,
                            "content": result.content,
                            "reasoning": result.reasoning,
                            "output_types": result.output_types,
                            "tool_calls": [
                                {"name": tc.name, "arguments": tc.arguments, "result": tc.result}
                                for tc in result.tool_calls
                            ],
                            "stats": result.stats,
                        },
                        default=str,
                    )
                    + "\n"
                )
    finally:
        client.close()
        if trace_fh:
            trace_fh.close()

    run_ended_at = datetime.datetime.now(datetime.timezone.utc)
    summary = RunSummary(
        experiment_id="evals-runner",
        run_id=run_id,
        started_at=run_started_at.isoformat(),
        ended_at=run_ended_at.isoformat(),
        total=len(cases),
        passed=passed,
        failed=failed,
        target=args.base_url,
        model=args.model,
    )

    print(f"\n==> {passed} passed, {failed} failed ({len(cases)} total)")

    if args.html_report:
        html = render_html(trials, summary, brief_path=None)
        args.html_report.parent.mkdir(parents=True, exist_ok=True)
        args.html_report.write_text(html, encoding="utf-8")
        print(f"==> HTML report written to {args.html_report}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
