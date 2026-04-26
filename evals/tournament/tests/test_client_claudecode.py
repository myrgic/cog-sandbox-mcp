"""Tests for evals.tournament.client_claudecode — ClaudeCodeClient.

Uses a stubbed HTTP server (via httpx.MockTransport) to verify:
1. Routing logic: requests go to kernel /v1/chat/completions with model=sonnet.
2. No API key required or used.
3. Tool-call loop: multi-turn dispatch with kernel MCP execution.
4. Error handling: HTTP errors surface cleanly without fallback to Anthropic API.
5. Model override: custom model name is forwarded correctly.

The MCP session is mocked via monkeypatching _MCPSession to avoid needing
a live kernel for unit tests.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from evals.tournament.client_claudecode import ClaudeCodeClient, CLAUDE_CODE_MODEL, MAX_TURNS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chat_response(
    content: str | None = None,
    tool_calls: list[dict] | None = None,
    finish_reason: str = "stop",
    model: str = "sonnet",
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> dict[str, Any]:
    """Build a minimal /v1/chat/completions response dict."""
    msg: dict[str, Any] = {"role": "assistant"}
    if content is not None:
        msg["content"] = content
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


def _tool_call_msg(name: str, args: dict, call_id: str = "call-1") -> dict:
    """Build an OpenAI tool_calls message entry."""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _make_client_stub(responses: list[dict]) -> tuple[ClaudeCodeClient, list[dict]]:
    """Create a ClaudeCodeClient with a mocked MCP session and HTTP responses.

    responses: list of /v1/chat/completions response dicts to return in order.
    Returns (client, captured_requests) — captured_requests is populated with
    each request body sent to /v1/chat/completions.
    """
    import httpx

    captured: list[dict] = []
    call_idx = [0]

    def _transport_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        idx = min(call_idx[0], len(responses) - 1)
        call_idx[0] += 1
        return httpx.Response(200, json=responses[idx])

    # Mock the MCP session to avoid needing a live kernel
    mock_mcp = MagicMock()
    mock_mcp.call.return_value = {
        "result": {
            "tools": [
                {
                    "name": "cog_get_state",
                    "description": "Get current kernel state.",
                    "inputSchema": {"type": "object", "properties": {}},
                },
                {
                    "name": "cog_check_coherence",
                    "description": "Check kernel coherence.",
                    "inputSchema": {"type": "object", "properties": {}},
                },
            ]
        }
    }
    mock_mcp.tool_call.return_value = {"status": "ok", "field_size": 100}

    with patch("evals.tournament.client_claudecode._MCPSession") as MockMCPSession:
        MockMCPSession.return_value = mock_mcp
        client = ClaudeCodeClient.__new__(ClaudeCodeClient)
        client.kernel_url = "http://localhost:6931"
        client.chat_url = "http://localhost:6931/v1/chat/completions"
        client.timeout = 30.0
        client.model = CLAUDE_CODE_MODEL
        client._mcp = mock_mcp
        client._base_tools = [
            {
                "name": "cog_get_state",
                "description": "Get current kernel state.",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ]
        import httpx as _httpx
        client._http = _httpx.Client(
            transport=_httpx.MockTransport(_transport_handler),
            timeout=30.0,
            headers={"Content-Type": "application/json"},
        )

    return client, captured


# ---------------------------------------------------------------------------
# Test: routing — requests go to kernel, model=sonnet, no Authorization header
# ---------------------------------------------------------------------------

class TestRouting:
    def test_model_is_sonnet_by_default(self):
        """Requests use model=sonnet to route to claude-code provider."""
        responses = [_make_chat_response(content="Hello from Claude.")]
        client, captured = _make_client_stub(responses)

        result = client.dispatch(task="Say hello.")

        assert len(captured) == 1
        assert captured[0]["model"] == CLAUDE_CODE_MODEL

    def test_no_authorization_header(self):
        """No Authorization header in requests — no API key path."""
        import httpx

        captured_headers: list[dict] = []

        def _transport(request: httpx.Request) -> httpx.Response:
            captured_headers.append(dict(request.headers))
            return httpx.Response(200, json=_make_chat_response(content="hi"))

        mock_mcp = MagicMock()
        mock_mcp.call.return_value = {"result": {"tools": []}}
        mock_mcp.tool_call.return_value = {}

        client = ClaudeCodeClient.__new__(ClaudeCodeClient)
        client.kernel_url = "http://localhost:6931"
        client.chat_url = "http://localhost:6931/v1/chat/completions"
        client.timeout = 30.0
        client.model = CLAUDE_CODE_MODEL
        client._mcp = mock_mcp
        client._base_tools = []
        client._http = httpx.Client(
            transport=httpx.MockTransport(_transport),
            timeout=30.0,
            headers={"Content-Type": "application/json"},
        )

        client.dispatch(task="hi")

        assert len(captured_headers) == 1
        headers_lower = {k.lower(): v for k, v in captured_headers[0].items()}
        assert "authorization" not in headers_lower, (
            "ClaudeCodeClient must NOT send an Authorization header (no API key)"
        )

    def test_custom_model_override(self):
        """dispatch(model=...) forwards custom model name to request."""
        responses = [_make_chat_response(content="custom model response")]
        client, captured = _make_client_stub(responses)

        client.dispatch(task="test", model="claude-opus-4-5")

        assert captured[0]["model"] == "claude-opus-4-5"


# ---------------------------------------------------------------------------
# Test: turn loop — tool call execution
# ---------------------------------------------------------------------------

class TestTurnLoop:
    def test_single_turn_no_tools(self):
        """Single response with no tool calls returns final content."""
        responses = [_make_chat_response(content="The state is dormant.")]
        client, captured = _make_client_stub(responses)

        result = client.dispatch(task="What is the state?")

        assert result.content == "The state is dormant."
        assert result.tool_calls == []
        assert result.stats["turns"] == 1
        assert result.stats["client"] == "claude_code"

    def test_tool_call_then_answer(self):
        """Tool call on turn 1 is executed, answer arrives on turn 2."""
        tc_msg = _tool_call_msg("cog_get_state", {}, call_id="call-abc")
        responses = [
            _make_chat_response(tool_calls=[tc_msg], finish_reason="tool_calls"),
            _make_chat_response(content="State is dormant.", finish_reason="stop"),
        ]
        client, captured = _make_client_stub(responses)

        result = client.dispatch(task="What is the state?")

        assert result.content == "State is dormant."
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "cog_get_state"
        assert result.stats["turns"] == 2

    def test_system_prompt_included(self):
        """System prompt is included as first message when provided."""
        responses = [_make_chat_response(content="ok")]
        client, captured = _make_client_stub(responses)

        client.dispatch(task="task", system_prompt="You are a test agent.")

        messages = captured[0]["messages"]
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "You are a test agent."
        assert messages[1]["role"] == "user"

    def test_no_system_prompt_skipped(self):
        """When system_prompt is None, messages start with user role."""
        responses = [_make_chat_response(content="ok")]
        client, captured = _make_client_stub(responses)

        client.dispatch(task="task")

        messages = captured[0]["messages"]
        assert messages[0]["role"] == "user"

    def test_token_counts_accumulated(self):
        """Token counts from multiple turns are summed in stats."""
        tc_msg = _tool_call_msg("cog_get_state", {})
        responses = [
            _make_chat_response(tool_calls=[tc_msg], input_tokens=20, output_tokens=10),
            _make_chat_response(content="done", input_tokens=30, output_tokens=15),
        ]
        client, captured = _make_client_stub(responses)

        result = client.dispatch(task="task")

        assert result.stats["input_tokens"] == 50
        assert result.stats["output_tokens"] == 25

    def test_max_turns_reached(self):
        """When MAX_TURNS is exhausted, returns partial result (no exception)."""
        # All turns return a tool call → never reaches final answer
        tc_msg = _tool_call_msg("cog_get_state", {})
        responses = [
            _make_chat_response(tool_calls=[tc_msg], finish_reason="tool_calls")
        ] * (MAX_TURNS + 1)
        client, captured = _make_client_stub(responses)

        result = client.dispatch(task="task")

        assert result.content in ("(max turns reached)", "")
        assert len(result.tool_calls) == MAX_TURNS


# ---------------------------------------------------------------------------
# Test: error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_http_error_returns_error_content(self):
        """HTTP error from kernel surfaces as content string, not exception."""
        import httpx

        def _bad_transport(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="Internal Server Error")

        mock_mcp = MagicMock()
        mock_mcp.call.return_value = {"result": {"tools": []}}

        client = ClaudeCodeClient.__new__(ClaudeCodeClient)
        client.kernel_url = "http://localhost:6931"
        client.chat_url = "http://localhost:6931/v1/chat/completions"
        client.timeout = 30.0
        client.model = CLAUDE_CODE_MODEL
        client._mcp = mock_mcp
        client._base_tools = []
        client._http = httpx.Client(
            transport=httpx.MockTransport(_bad_transport),
            timeout=30.0,
            headers={"Content-Type": "application/json"},
        )

        result = client.dispatch(task="task")

        assert "kernel chat error" in result.content
        assert "500" in result.content

    def test_tool_call_failure_recorded(self):
        """When a tool call fails, error is recorded in tool_calls result."""
        tc_msg = _tool_call_msg("cog_get_state", {}, call_id="call-fail")
        responses = [
            _make_chat_response(tool_calls=[tc_msg], finish_reason="tool_calls"),
            _make_chat_response(content="done", finish_reason="stop"),
        ]
        client, captured = _make_client_stub(responses)
        # Override mcp to raise on tool_call
        client._mcp.tool_call.side_effect = RuntimeError("MCP tool failed")

        result = client.dispatch(task="task")

        # Should still complete (not raise) and record the error in tool call result
        assert len(result.tool_calls) == 1
        assert "MCP tool failed" in (result.tool_calls[0].result or "")


# ---------------------------------------------------------------------------
# Test: runner integration — dispatch-mode routing logic
# ---------------------------------------------------------------------------

class TestRunnerRouting:
    """Verify that runner._run_trial dispatches to ClaudeCodeClient correctly."""

    def test_claude_code_client_dispatched_directly(self):
        """ClaudeCodeClient is dispatched without going through LMS or kernel-MCP paths."""
        from evals.tournament.runner import _run_trial
        from evals.tournament.matrix import TrialSpec
        from evals.tournament.variants import Variant

        task_variant = Variant(
            id="task-1-state-probe",
            variant_class="task",
            content={
                "prompt": "Use cog_get_state to check the kernel state.",
                "rubric": {
                    "expected_tools": ["cog_get_state"],
                },
                "max_tokens": 512,
            },
        )
        sp_variant = Variant(
            id="sp-1-production", variant_class="system-prompt", content="Be helpful."
        )
        spec = TrialSpec(
            trial_id="test-trial",
            experiment_id="test-exp",
            task_variant=task_variant,
            variant_ids={"system_prompt": "sp-1-production", "tool_description": "td-1-current"},
            system_prompt_variant=sp_variant,
            tool_description_variant=None,
            target="claude-code",
        )

        mock_client = MagicMock(spec=ClaudeCodeClient)
        from evals.harness.client import AgenticResult, ToolCall
        mock_client.dispatch.return_value = AgenticResult(
            content="The kernel state is dormant.",
            tool_calls=[ToolCall(name="cog_get_state", arguments={}, result="{}", call_id="c1")],
            reasoning="",
            output_types=["stop"],
            stats={"client": "claude_code"},
            raw={},
        )

        result, verdict = _run_trial(spec, mock_client, model="sonnet", plugin_ids=[])

        # Verify dispatch was called on the ClaudeCodeClient
        mock_client.dispatch.assert_called_once()
        call_kwargs = mock_client.dispatch.call_args
        assert call_kwargs.kwargs.get("task") or call_kwargs.args[0]
        # Verdict: cog_get_state is in expected_tools, should pass
        assert verdict.passed
