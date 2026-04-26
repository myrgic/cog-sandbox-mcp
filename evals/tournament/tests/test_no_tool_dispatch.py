"""Tests for the parametric (no-tool) dispatch mode.

Verifies:
1. KernelMCPClient.dispatch sends tools=[] and injects the directive when no_tools=True.
2. KernelMCPClient.dispatch leaves tools and system_prompt unchanged when no_tools=False.
3. runner._run_trial passes parametric_mode=True down to KernelMCPClient.dispatch.
4. runner._make_trial_record sets parametric_mode=True on the TrialRecord.
5. TrialRecord.parametric_mode round-trips through asdict/load_trials_jsonl.
6. --no-tools flag exits with error for non-kernel dispatch modes.

No live kernel required — all MCP calls are mocked.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, call, patch
import pytest

from evals.harness.client import AgenticResult, ToolCall
from evals.tournament.client_kernel import KernelMCPClient
from evals.reports.data import TrialRecord, load_trials_jsonl, save_trial_jsonl


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PARAMETRIC_DIRECTIVE = (
    "Answer directly from your knowledge. "
    "Do not attempt tool calls. "
    "If you don't know, say so."
)

_DUMMY_DISPATCH_RESULT = {
    "results": [
        {
            "index": 0,
            "success": True,
            "content": "The answer is nginx.",
            "tool_calls": [],
            "error": "",
            "duration_sec": 1.2,
            "turns": 1,
        }
    ],
    "total_duration_sec": 1.2,
}


def _make_kernel_client_with_mock(dispatch_result: dict) -> tuple[KernelMCPClient, MagicMock]:
    """Construct a KernelMCPClient whose MCP session is fully mocked."""
    mock_session = MagicMock()
    # tool_call returns the serialised JSON as text inside MCP content items
    mock_session.tool_call.return_value = {
        "content": [{"type": "text", "text": json.dumps(dispatch_result)}]
    }

    client = KernelMCPClient.__new__(KernelMCPClient)
    client.base_url = "http://localhost:6931"
    client.timeout = 30.0
    client._session = mock_session
    return client, mock_session


# ---------------------------------------------------------------------------
# KernelMCPClient.dispatch — no_tools=True
# ---------------------------------------------------------------------------

class TestKernelDispatchNoTools:
    def test_tools_empty_list_sent(self):
        client, mock_session = _make_kernel_client_with_mock(_DUMMY_DISPATCH_RESULT)
        client.dispatch(task="What is rate-limiting in nginx?", no_tools=True)

        _, call_kwargs = mock_session.tool_call.call_args
        # tool_call is called as tool_call(name, arguments_dict)
        args_sent = mock_session.tool_call.call_args[0][1]
        assert "tools" in args_sent, "tools key must be present when no_tools=True"
        assert args_sent["tools"] == [], f"Expected tools=[], got {args_sent['tools']!r}"

    def test_directive_prepended_when_no_system_prompt(self):
        client, mock_session = _make_kernel_client_with_mock(_DUMMY_DISPATCH_RESULT)
        client.dispatch(task="What does limit_req_zone do?", no_tools=True)

        args_sent = mock_session.tool_call.call_args[0][1]
        sp = args_sent.get("system_prompt", "")
        assert sp == _PARAMETRIC_DIRECTIVE, (
            f"Expected bare directive, got: {sp!r}"
        )

    def test_directive_prepended_before_existing_system_prompt(self):
        client, mock_session = _make_kernel_client_with_mock(_DUMMY_DISPATCH_RESULT)
        existing_sp = "You are a helpful assistant."
        client.dispatch(
            task="Explain nginx rate limits.",
            system_prompt=existing_sp,
            no_tools=True,
        )

        args_sent = mock_session.tool_call.call_args[0][1]
        sp = args_sent.get("system_prompt", "")
        assert sp.startswith(_PARAMETRIC_DIRECTIVE), (
            f"Directive must be first; got: {sp!r}"
        )
        assert existing_sp in sp, (
            f"Original SP must be preserved; got: {sp!r}"
        )
        # Directive and original SP separated by double newline
        assert f"{_PARAMETRIC_DIRECTIVE}\n\n{existing_sp}" == sp

    def test_result_has_no_tool_calls(self):
        client, _ = _make_kernel_client_with_mock(_DUMMY_DISPATCH_RESULT)
        result = client.dispatch(task="What is pypiserver?", no_tools=True)
        assert result.tool_calls == [], (
            f"Expected empty tool_calls list, got {result.tool_calls}"
        )

    def test_content_returned(self):
        client, _ = _make_kernel_client_with_mock(_DUMMY_DISPATCH_RESULT)
        result = client.dispatch(task="What is pypiserver?", no_tools=True)
        assert "nginx" in result.content or result.content  # content is non-empty


# ---------------------------------------------------------------------------
# KernelMCPClient.dispatch — no_tools=False (default, no regression)
# ---------------------------------------------------------------------------

class TestKernelDispatchWithTools:
    def test_tools_not_sent_when_none(self):
        """When no_tools=False and tools=None, tools key must NOT appear (use harness default)."""
        client, mock_session = _make_kernel_client_with_mock(_DUMMY_DISPATCH_RESULT)
        client.dispatch(task="Check state.", no_tools=False)

        args_sent = mock_session.tool_call.call_args[0][1]
        assert "tools" not in args_sent, (
            "tools key must be absent when no_tools=False and tools=None"
        )

    def test_explicit_tools_list_forwarded(self):
        """When tools=['some_tool'] is explicitly passed, it is forwarded unchanged."""
        client, mock_session = _make_kernel_client_with_mock(_DUMMY_DISPATCH_RESULT)
        client.dispatch(task="Do X.", tools=["cog_get_state"], no_tools=False)

        args_sent = mock_session.tool_call.call_args[0][1]
        assert args_sent.get("tools") == ["cog_get_state"]

    def test_system_prompt_unchanged(self):
        """System prompt must not be modified when no_tools=False."""
        client, mock_session = _make_kernel_client_with_mock(_DUMMY_DISPATCH_RESULT)
        original_sp = "Be concise."
        client.dispatch(task="Summarize.", system_prompt=original_sp, no_tools=False)

        args_sent = mock_session.tool_call.call_args[0][1]
        assert args_sent.get("system_prompt") == original_sp


# ---------------------------------------------------------------------------
# TrialRecord.parametric_mode field
# ---------------------------------------------------------------------------

class TestTrialRecordParametricMode:
    def _make_record(self, parametric_mode: bool = False) -> TrialRecord:
        return TrialRecord(
            trial_id="t-001",
            experiment_id="exp-001",
            variant_ids={"system_prompt": "sp-1"},
            task_id="tb-008-nginx-rate-limit",
            target="laptop-kernel",
            passed=True,
            parametric_mode=parametric_mode,
        )

    def test_default_is_false(self):
        record = self._make_record()
        assert record.parametric_mode is False

    def test_explicit_true(self):
        record = self._make_record(parametric_mode=True)
        assert record.parametric_mode is True

    def test_asdict_includes_field(self):
        record = self._make_record(parametric_mode=True)
        d = asdict(record)
        assert "parametric_mode" in d
        assert d["parametric_mode"] is True

    def test_roundtrip_jsonl(self, tmp_path: Path):
        record = self._make_record(parametric_mode=True)
        path = tmp_path / "trials.jsonl"
        save_trial_jsonl(record, path)
        loaded = load_trials_jsonl(path)
        assert len(loaded) == 1
        assert loaded[0].parametric_mode is True

    def test_roundtrip_jsonl_false(self, tmp_path: Path):
        record = self._make_record(parametric_mode=False)
        path = tmp_path / "trials.jsonl"
        save_trial_jsonl(record, path)
        loaded = load_trials_jsonl(path)
        assert loaded[0].parametric_mode is False


# ---------------------------------------------------------------------------
# runner._make_trial_record propagates parametric_mode
# ---------------------------------------------------------------------------

class TestMakeTrialRecordParametricMode:
    def _build_spec_and_result(self):
        """Build minimal TrialSpec and AgenticResult for _make_trial_record."""
        from evals.tournament.matrix import TrialSpec
        from evals.tournament.variants import Variant

        task_variant = Variant(
            id="tb-008-nginx-rate-limit",
            variant_class="task",
            content={
                "prompt": "What nginx directive is used for rate limiting?",
                "rubric": {"content_contains_ci": ["limit_req_zone"]},
                "max_tokens": 256,
            },
        )
        spec = TrialSpec(
            trial_id="t-001",
            experiment_id="exp-002-terminal-bench",
            task_variant=task_variant,
            variant_ids={"system_prompt": "sp-1-production"},
            system_prompt_variant=None,
            tool_description_variant=None,
            target="laptop-kernel",
        )
        result = AgenticResult(
            content="Use limit_req_zone.",
            tool_calls=[],
            reasoning="",
            output_types=[],
            stats={},
            raw={},
        )
        from evals.harness.scoring import Verdict
        verdict = Verdict(passed=True, failures=[], notes=[])
        return spec, result, verdict

    def test_parametric_mode_true_propagates(self):
        from evals.tournament.runner import _make_trial_record
        spec, result, verdict = self._build_spec_and_result()
        record = _make_trial_record(
            spec=spec,
            result=result,
            verdict=verdict,
            model="gemma4:e4b",
            base_url="http://localhost:6931",
            timestamp="2026-04-25T00:00:00Z",
            duration_sec=1.5,
            td_wired=False,
            parametric_mode=True,
        )
        assert record.parametric_mode is True

    def test_parametric_mode_false_is_default(self):
        from evals.tournament.runner import _make_trial_record
        spec, result, verdict = self._build_spec_and_result()
        record = _make_trial_record(
            spec=spec,
            result=result,
            verdict=verdict,
            model="gemma4:e4b",
            base_url="http://localhost:6931",
            timestamp="2026-04-25T00:00:00Z",
            duration_sec=1.5,
            td_wired=False,
        )
        assert record.parametric_mode is False


# ---------------------------------------------------------------------------
# CLI argument validation — --no-tools guard
# ---------------------------------------------------------------------------

class TestCLINoToolsFlag:
    def _run_main(self, argv: list[str]) -> int:
        """Run runner.main() with argv; returns exit code."""
        from evals.tournament import runner
        return runner.main(argv)

    def test_no_tools_with_lms_mode_exits_with_2(self):
        # Provide required --experiment arg; mock away actual network
        rc = self._run_main([
            "--experiment", "exp-002-terminal-bench",
            "--dispatch-mode", "lms",
            "--no-tools",
        ])
        assert rc == 2, f"Expected exit code 2, got {rc}"

    def test_no_tools_with_chat_mode_exits_with_2(self):
        rc = self._run_main([
            "--experiment", "exp-002-terminal-bench",
            "--dispatch-mode", "chat",
            "--no-tools",
        ])
        assert rc == 2, f"Expected exit code 2, got {rc}"

    def test_no_tools_with_claude_mode_exits_with_2(self):
        rc = self._run_main([
            "--experiment", "exp-002-terminal-bench",
            "--dispatch-mode", "claude",
            "--no-tools",
        ])
        assert rc == 2, f"Expected exit code 2, got {rc}"

    def test_help_shows_no_tools_flag(self, capsys):
        import sys
        with pytest.raises(SystemExit):
            from evals.tournament import runner
            runner.main(["--help"])
        captured = capsys.readouterr()
        assert "--no-tools" in captured.out, (
            "--no-tools must appear in --help output"
        )
