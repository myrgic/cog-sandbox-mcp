"""Tests for evals.tournament.ledger_evidence — LedgerToolCallCollector.

All tests use mocked MCP sessions so no live kernel is required.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from evals.harness.client import ToolCall
from evals.tournament.ledger_evidence import (
    LedgerToolCallCollector,
    _to_rfc3339,
    _CONTAMINATION_WARN_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ledger_entry(
    tool_name: str = "cog_get_state",
    status: str = "success",
    call_id: str = "call-1",
    arguments: dict | None = None,
    output_summary: str | None = "{}",
) -> dict:
    return {
        "call_id": call_id,
        "tool_name": tool_name,
        "session_id": "sess-abc",
        "source": "mcp",
        "ownership": "kernel",
        "called_at": "2026-04-26T10:00:00Z",
        "completed_at": "2026-04-26T10:00:01Z",
        "duration_ms": 100,
        "status": status,
        "output_length": len(output_summary or ""),
        "arguments": arguments or {},
        "output_summary": output_summary,
        "interaction_id": "some-interaction-id",
    }


def _make_collector_with_mock(ledger_response: dict) -> LedgerToolCallCollector:
    """Create a LedgerToolCallCollector with a mocked MCP session."""
    collector = LedgerToolCallCollector.__new__(LedgerToolCallCollector)
    collector._kernel_url = "http://localhost:6931"
    collector._timeout = 30.0

    mock_mcp = MagicMock()
    mock_mcp.tool_call.return_value = ledger_response
    collector._mcp = mock_mcp
    return collector


# ---------------------------------------------------------------------------
# Tests: _to_rfc3339
# ---------------------------------------------------------------------------

class TestToRfc3339:
    def test_utc_datetime(self):
        dt = datetime(2026, 4, 26, 10, 30, 0, tzinfo=timezone.utc)
        assert _to_rfc3339(dt) == "2026-04-26T10:30:00Z"

    def test_naive_datetime_treated_as_utc(self):
        dt = datetime(2026, 4, 26, 10, 30, 0)
        result = _to_rfc3339(dt)
        assert result == "2026-04-26T10:30:00Z"

    def test_subsecond_truncated(self):
        dt = datetime(2026, 4, 26, 10, 30, 45, 123456, tzinfo=timezone.utc)
        assert _to_rfc3339(dt) == "2026-04-26T10:30:45Z"


# ---------------------------------------------------------------------------
# Tests: collect — basic happy path
# ---------------------------------------------------------------------------

class TestCollectBasic:
    def test_single_tool_call_returned(self):
        entry = _make_ledger_entry("cog_get_state", output_summary='{"field_size":100}')
        collector = _make_collector_with_mock({"calls": [entry]})

        start = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 26, 10, 0, 5, tzinfo=timezone.utc)
        tool_calls, stats = collector.collect(start, end)

        assert len(tool_calls) == 1
        tc = tool_calls[0]
        assert tc.name == "cog_get_state"
        assert tc.call_id == "call-1"
        assert stats.raw_count == 1
        assert stats.returned_count == 1

    def test_multiple_tool_calls(self):
        entries = [
            _make_ledger_entry("cog_get_state", call_id="c1"),
            _make_ledger_entry("cog_check_coherence", call_id="c2"),
        ]
        collector = _make_collector_with_mock({"calls": entries})

        start = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 26, 10, 0, 10, tzinfo=timezone.utc)
        tool_calls, stats = collector.collect(start, end)

        assert len(tool_calls) == 2
        assert tool_calls[0].name == "cog_get_state"
        assert tool_calls[1].name == "cog_check_coherence"
        assert stats.returned_count == 2

    def test_empty_calls_list(self):
        collector = _make_collector_with_mock({"calls": []})

        start = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 26, 10, 0, 5, tzinfo=timezone.utc)
        tool_calls, stats = collector.collect(start, end)

        assert tool_calls == []
        assert stats.raw_count == 0
        assert stats.returned_count == 0

    def test_arguments_populated(self):
        entry = _make_ledger_entry(
            "cog_read_cogdoc",
            arguments={"uri": "cog://memory/test.md"},
        )
        collector = _make_collector_with_mock({"calls": [entry]})

        start = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 26, 10, 0, 5, tzinfo=timezone.utc)
        tool_calls, stats = collector.collect(start, end)

        assert tool_calls[0].arguments == {"uri": "cog://memory/test.md"}

    def test_output_summary_used_as_result(self):
        entry = _make_ledger_entry(output_summary='{"state":"dormant"}')
        collector = _make_collector_with_mock({"calls": [entry]})

        start = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 26, 10, 0, 5, tzinfo=timezone.utc)
        tool_calls, _ = collector.collect(start, end)

        assert tool_calls[0].result == '{"state":"dormant"}'


# ---------------------------------------------------------------------------
# Tests: collect — time-window filter passed to MCP
# ---------------------------------------------------------------------------

class TestTimeWindowFiltering:
    def test_since_and_until_passed_to_mcp(self):
        collector = _make_collector_with_mock({"calls": []})

        start = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 26, 10, 0, 15, tzinfo=timezone.utc)
        collector.collect(start, end)

        call_args = collector._mcp.tool_call.call_args
        assert call_args is not None
        name_called, kwargs_passed = call_args.args
        assert name_called == "cog_read_tool_calls"
        assert kwargs_passed["since"] == "2026-04-26T10:00:00Z"
        assert kwargs_passed["until"] == "2026-04-26T10:00:15Z"

    def test_source_and_ownership_filters_passed(self):
        collector = _make_collector_with_mock({"calls": []})

        start = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 26, 10, 0, 5, tzinfo=timezone.utc)
        collector.collect(start, end, source="mcp", ownership="kernel")

        call_args = collector._mcp.tool_call.call_args
        _, kwargs_passed = call_args.args
        assert kwargs_passed["source"] == "mcp"
        assert kwargs_passed["ownership"] == "kernel"

    def test_include_args_and_output_requested(self):
        """Collector always requests full arguments and output_summary."""
        collector = _make_collector_with_mock({"calls": []})

        start = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 26, 10, 0, 5, tzinfo=timezone.utc)
        collector.collect(start, end)

        _, kwargs_passed = collector._mcp.tool_call.call_args.args
        assert kwargs_passed["include_args"] is True
        assert kwargs_passed["include_output"] is True


# ---------------------------------------------------------------------------
# Tests: collect — error status mapping
# ---------------------------------------------------------------------------

class TestStatusMapping:
    def test_error_status_prefixed_in_result(self):
        entry = _make_ledger_entry(status="error", output_summary="tool failed")
        collector = _make_collector_with_mock({"calls": [entry]})

        start = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 26, 10, 0, 5, tzinfo=timezone.utc)
        tool_calls, _ = collector.collect(start, end)

        assert tool_calls[0].result is not None
        assert "[error]" in tool_calls[0].result

    def test_timeout_status_prefixed(self):
        entry = _make_ledger_entry(status="timeout", output_summary=None)
        collector = _make_collector_with_mock({"calls": [entry]})

        start = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 26, 10, 0, 5, tzinfo=timezone.utc)
        tool_calls, _ = collector.collect(start, end)

        assert "[timeout]" in (tool_calls[0].result or "")

    def test_success_status_uses_output_directly(self):
        entry = _make_ledger_entry(status="success", output_summary="clean result")
        collector = _make_collector_with_mock({"calls": [entry]})

        start = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 26, 10, 0, 5, tzinfo=timezone.utc)
        tool_calls, _ = collector.collect(start, end)

        assert tool_calls[0].result == "clean result"

    def test_pending_status_treated_as_success(self):
        entry = _make_ledger_entry(status="pending", output_summary="in-progress")
        collector = _make_collector_with_mock({"calls": [entry]})

        start = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 26, 10, 0, 5, tzinfo=timezone.utc)
        tool_calls, _ = collector.collect(start, end)

        assert tool_calls[0].result == "in-progress"


# ---------------------------------------------------------------------------
# Tests: collect — contamination warning
# ---------------------------------------------------------------------------

class TestContaminationWarning:
    def test_warning_when_over_threshold(self):
        entries = [
            _make_ledger_entry(call_id=f"c{i}")
            for i in range(_CONTAMINATION_WARN_THRESHOLD + 1)
        ]
        collector = _make_collector_with_mock({"calls": entries})

        start = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 26, 10, 0, 5, tzinfo=timezone.utc)
        tool_calls, stats = collector.collect(start, end)

        assert len(tool_calls) == _CONTAMINATION_WARN_THRESHOLD + 1
        assert "WARNING" in stats.warning

    def test_no_warning_at_threshold(self):
        entries = [
            _make_ledger_entry(call_id=f"c{i}")
            for i in range(_CONTAMINATION_WARN_THRESHOLD)
        ]
        collector = _make_collector_with_mock({"calls": entries})

        start = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 26, 10, 0, 5, tzinfo=timezone.utc)
        _, stats = collector.collect(start, end)

        assert stats.warning == ""


# ---------------------------------------------------------------------------
# Tests: collect — ledger query failure
# ---------------------------------------------------------------------------

class TestQueryFailure:
    def test_returns_empty_on_mcp_error(self):
        collector = _make_collector_with_mock({})
        collector._mcp.tool_call.side_effect = RuntimeError("MCP unreachable")

        start = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 26, 10, 0, 5, tzinfo=timezone.utc)
        tool_calls, stats = collector.collect(start, end)

        assert tool_calls == []
        assert "ledger query failed" in stats.warning

    def test_entries_with_empty_tool_name_skipped(self):
        entries = [
            _make_ledger_entry(""),  # no name
            _make_ledger_entry("cog_get_state"),
        ]
        collector = _make_collector_with_mock({"calls": entries})

        start = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 26, 10, 0, 5, tzinfo=timezone.utc)
        tool_calls, stats = collector.collect(start, end)

        assert len(tool_calls) == 1
        assert tool_calls[0].name == "cog_get_state"
        # raw_count includes the empty entry; returned_count does not
        assert stats.raw_count == 2
        assert stats.returned_count == 1


# ---------------------------------------------------------------------------
# Tests: ClaudeCodeClient ledger integration
# ---------------------------------------------------------------------------

class TestClaudeCodeClientLedgerIntegration:
    """Verify that ClaudeCodeClient populates tool_calls from ledger evidence."""

    def _make_client_with_ledger(
        self,
        chat_responses: list[dict],
        ledger_calls: list[dict],
    ):
        """Build a ClaudeCodeClient with mocked HTTP + MCP + ledger."""
        import httpx
        from evals.tournament.client_claudecode import ClaudeCodeClient, CLAUDE_CODE_MODEL

        call_idx = [0]

        def _transport(request: httpx.Request) -> httpx.Response:
            idx = min(call_idx[0], len(chat_responses) - 1)
            call_idx[0] += 1
            return httpx.Response(200, json=chat_responses[idx])

        mock_mcp = MagicMock()
        mock_mcp.call.return_value = {
            "result": {
                "tools": [
                    {"name": "cog_get_state", "description": "Get state.", "inputSchema": {}},
                ]
            }
        }
        mock_mcp.tool_call.return_value = {"status": "ok"}

        mock_ledger = MagicMock()
        from evals.tournament.ledger_evidence import CollectionStats
        mock_ledger.collect.return_value = (
            [ToolCall(name=e["tool_name"], arguments=e.get("arguments", {}),
                      result=e.get("output_summary"), call_id=e.get("call_id"))
             for e in ledger_calls],
            CollectionStats(
                window_start="2026-04-26T10:00:00Z",
                window_end="2026-04-26T10:00:10Z",
                raw_count=len(ledger_calls),
                returned_count=len(ledger_calls),
            ),
        )

        client = ClaudeCodeClient.__new__(ClaudeCodeClient)
        client.kernel_url = "http://localhost:6931"
        client.chat_url = "http://localhost:6931/v1/chat/completions"
        client.timeout = 30.0
        client.model = CLAUDE_CODE_MODEL
        client._mcp = mock_mcp
        client._ledger = mock_ledger
        client._base_tools = [
            {"name": "cog_get_state", "description": ".", "inputSchema": {}}
        ]
        client._http = httpx.Client(
            transport=httpx.MockTransport(_transport),
            timeout=30.0,
            headers={"Content-Type": "application/json"},
        )
        return client

    def _make_final_only_response(self, content: str) -> dict:
        """Chat response with no tool_calls — simulates subprocess internal tool use."""
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": "sonnet",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    def test_ledger_calls_populate_tool_calls(self):
        """When ledger finds tool calls and chat-completions has none, tool_calls is populated."""
        ledger_entries = [_make_ledger_entry("cog_get_state", call_id="ledger-c1")]
        client = self._make_client_with_ledger(
            chat_responses=[self._make_final_only_response("The state is dormant.")],
            ledger_calls=ledger_entries,
        )

        result = client.dispatch(task="Check the kernel state.")

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "cog_get_state"
        # Ledger stats should appear in AgenticResult.stats
        assert "ledger_returned_count" in result.stats
        assert result.stats["ledger_returned_count"] == 1

    def test_no_ledger_calls_returns_empty(self):
        """When ledger finds nothing and chat-completions has none, tool_calls is empty."""
        client = self._make_client_with_ledger(
            chat_responses=[self._make_final_only_response("Done.")],
            ledger_calls=[],
        )

        result = client.dispatch(task="Simple task.")

        assert result.tool_calls == []
        assert result.stats["ledger_returned_count"] == 0

    def test_ledger_preferred_over_chat_completions(self):
        """When both ledger and chat-completions return calls, ledger is preferred (merged)."""
        from evals.tournament.client_claudecode import ClaudeCodeClient, CLAUDE_CODE_MODEL
        import httpx

        # Chat response that also reports a tool call
        tc_msg = {
            "id": "cc-call-1",
            "type": "function",
            "function": {"name": "cog_check_coherence", "arguments": "{}"},
        }
        chat_with_tool = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": "sonnet",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "tool_calls": [tc_msg]},
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        final_response = self._make_final_only_response("Coherence OK.")

        ledger_entries = [_make_ledger_entry("cog_get_state", call_id="ledger-c1")]
        client = self._make_client_with_ledger(
            chat_responses=[chat_with_tool, final_response],
            ledger_calls=ledger_entries,
        )

        result = client.dispatch(task="Check everything.")

        # Tool calls should include ledger call + chat-completions call (different call_id)
        names = [tc.name for tc in result.tool_calls]
        assert "cog_get_state" in names

    def test_ledger_collect_called_with_time_window(self):
        """collect() is invoked with start < end datetimes."""
        ledger_entries = [_make_ledger_entry("cog_get_state")]
        client = self._make_client_with_ledger(
            chat_responses=[self._make_final_only_response("Done.")],
            ledger_calls=ledger_entries,
        )

        client.dispatch(task="Check state.")

        assert client._ledger.collect.call_count == 1
        call_kwargs = client._ledger.collect.call_args
        start_arg = call_kwargs.kwargs["start"]
        end_arg = call_kwargs.kwargs["end"]
        assert isinstance(start_arg, datetime)
        assert isinstance(end_arg, datetime)
        assert end_arg > start_arg
