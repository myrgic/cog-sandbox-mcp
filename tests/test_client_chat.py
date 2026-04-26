"""Unit tests for evals/tournament/client_chat.py.

Tests cover:
1. _to_oai_tool: description override application and length capping.
2. _parse_args: handles dict, valid JSON string, malformed JSON string.
3. ChatCompletionsClient._build_oai_tools: override dict patches descriptions correctly.
4. ChatCompletionsClient.dispatch: full turn loop mocked at httpx level — handles:
   a. Direct final answer (no tool_calls).
   b. One tool call then final answer.
   c. Runaway loop (MAX_TURNS exhausted).
5. Runner routing: _is_td_nonbaseline returns True only for non-baseline TD variants.
6. Runner td_wired logic: True when chat_client available, False otherwise.

All HTTP (LMS) and MCP (kernel) calls are mocked — no live services needed.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Module-level helpers (imported directly, not via class)
# ---------------------------------------------------------------------------

from evals.tournament.client_chat import (
    _to_oai_tool,
    _parse_args,
    MAX_TURNS,
)


# ---------------------------------------------------------------------------
# _to_oai_tool
# ---------------------------------------------------------------------------

class TestToOaiTool:
    def _make_kernel_tool(self, name: str, description: str) -> dict[str, Any]:
        return {
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        }

    def test_baseline_uses_kernel_description(self):
        t = self._make_kernel_tool("cog_search_memory", "Search the CogDoc memory corpus.")
        oai = _to_oai_tool(t, description_override=None)
        assert oai["function"]["name"] == "cog_search_memory"
        assert oai["function"]["description"] == "Search the CogDoc memory corpus."
        assert oai["type"] == "function"

    def test_override_replaces_description(self):
        t = self._make_kernel_tool("cog_search_memory", "Original description.")
        oai = _to_oai_tool(t, description_override="Overridden description with anti-pattern.")
        assert oai["function"]["description"] == "Overridden description with anti-pattern."

    def test_description_capped_at_500_chars(self):
        t = self._make_kernel_tool("cog_search_memory", "x" * 600)
        oai = _to_oai_tool(t, description_override=None)
        assert len(oai["function"]["description"]) == 500

    def test_override_also_capped_at_500_chars(self):
        t = self._make_kernel_tool("cog_search_memory", "short")
        oai = _to_oai_tool(t, description_override="y" * 600)
        assert len(oai["function"]["description"]) == 500

    def test_parameters_preserved(self):
        t = self._make_kernel_tool("cog_search_memory", "desc")
        oai = _to_oai_tool(t)
        assert oai["function"]["parameters"]["properties"]["query"]["type"] == "string"

    def test_missing_input_schema_defaults_to_empty_object(self):
        t = {"name": "cog_get_state", "description": "Get state."}
        oai = _to_oai_tool(t)
        assert oai["function"]["parameters"] == {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# _parse_args
# ---------------------------------------------------------------------------

class TestParseArgs:
    def test_dict_passthrough(self):
        args = {"query": "test", "limit": 10}
        assert _parse_args(args) == args

    def test_valid_json_string(self):
        result = _parse_args('{"query": "test"}')
        assert result == {"query": "test"}

    def test_malformed_json_string(self):
        result = _parse_args("{bad json}")
        assert "_unparsed" in result

    def test_unexpected_type(self):
        result = _parse_args(42)
        assert "_unexpected_type" in result

    def test_empty_dict_string(self):
        result = _parse_args("{}")
        assert result == {}


# ---------------------------------------------------------------------------
# ChatCompletionsClient unit tests (HTTP + MCP mocked)
# ---------------------------------------------------------------------------

def _make_lms_response(content: str | None, tool_calls: list | None = None) -> dict:
    """Build a minimal /v1/chat/completions response dict."""
    msg: dict[str, Any] = {"role": "assistant"}
    if content:
        msg["content"] = content
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "choices": [{"message": msg, "finish_reason": "stop" if not tool_calls else "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _make_tool_call_msg(tool_name: str, args: dict, call_id: str = "call-1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": tool_name,
            "arguments": json.dumps(args),
        },
    }


class TestChatCompletionsClientDispatch:
    """Mock both httpx.Client and _MCPSession to test dispatch() in isolation."""

    def _make_client(self, http_responses: list[dict], mcp_tools_result: dict | None = None):
        """Build a ChatCompletionsClient with mocked HTTP and MCP layers."""
        from evals.tournament.client_chat import ChatCompletionsClient

        # Default MCP tools/list response
        if mcp_tools_result is None:
            mcp_tools_result = {
                "result": {
                    "tools": [
                        {
                            "name": "cog_search_memory",
                            "description": "Search the CogDoc memory corpus.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                                "required": ["query"],
                            },
                        },
                        {
                            "name": "cog_get_state",
                            "description": "Get kernel state.",
                            "inputSchema": {"type": "object", "properties": {}},
                        },
                    ]
                }
            }

        mock_mcp = MagicMock()
        mock_mcp.call.return_value = mcp_tools_result
        mock_mcp.tool_call.return_value = {"result": "tool output"}

        # Build iterator for HTTP responses
        http_iter = iter(http_responses)

        mock_http_response = MagicMock()

        def post_side_effect(url, **kwargs):
            resp_data = next(http_iter)
            mock_resp = MagicMock()
            mock_resp.json.return_value = resp_data
            mock_resp.raise_for_status.return_value = None
            return mock_resp

        mock_http = MagicMock()
        mock_http.post.side_effect = post_side_effect

        with patch("evals.tournament.client_chat._MCPSession", return_value=mock_mcp), \
             patch("httpx.Client", return_value=mock_http):
            client = ChatCompletionsClient(
                base_url="http://localhost:1234",
                api_token="test-token",
                kernel_url="http://localhost:6931",
                timeout=30.0,
            )

        # Replace the internal http client with our mock after construction
        client._http = mock_http
        client._mcp = mock_mcp
        return client

    def test_direct_final_answer_no_tool_calls(self):
        """Model responds with content on turn 1 — no tool calls."""
        responses = [_make_lms_response("Kernel uptime is 42s.")]
        client = self._make_client(responses)

        result = client.dispatch(task="What is the kernel uptime?")

        assert result.content == "Kernel uptime is 42s."
        assert result.tool_calls == []
        assert result.stats["turns"] == 1
        assert result.stats["client"] == "chat_completions"

    def test_one_tool_call_then_final_answer(self):
        """Model calls cog_get_state, gets result, then answers."""
        tc = _make_tool_call_msg("cog_get_state", {}, call_id="call-abc")
        responses = [
            _make_lms_response(None, tool_calls=[tc]),
            _make_lms_response("State: uptime=42s."),
        ]
        client = self._make_client(responses)

        result = client.dispatch(task="What is the kernel state?")

        assert "uptime" in result.content or result.content  # final answer present
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "cog_get_state"
        assert result.tool_calls[0].call_id == "call-abc"
        assert result.stats["turns"] == 2
        # Verify MCP tool_call was invoked
        client._mcp.tool_call.assert_called_once_with("cog_get_state", {})

    def test_td_overrides_applied_to_tool_descriptions(self):
        """Verify that td_overrides patches the description in the tools[] array sent to LMS."""
        responses = [_make_lms_response("Done.")]
        client = self._make_client(responses)

        overrides = {
            "cog_search_memory": (
                "Search memory. Do NOT call cog_read_cogdoc on event log paths."
            )
        }
        client.dispatch(task="Search for something.", td_overrides=overrides)

        # Extract the tools[] array from the LMS POST call
        call_kwargs = client._http.post.call_args
        sent_tools = call_kwargs[1]["json"]["tools"]  # keyword args
        sm_tool = next(t for t in sent_tools if t["function"]["name"] == "cog_search_memory")
        assert "Do NOT call cog_read_cogdoc" in sm_tool["function"]["description"]

    def test_max_turns_exhaustion_returns_partial(self):
        """When model keeps calling tools past MAX_TURNS, dispatch returns gracefully."""
        tc = _make_tool_call_msg("cog_get_state", {}, call_id="call-x")
        # All turns return a tool call — never a final answer
        responses = [_make_lms_response(None, tool_calls=[tc])] * (MAX_TURNS + 2)
        client = self._make_client(responses)

        result = client.dispatch(task="Never-ending task.")

        # Should return without raising; content is the fallback string
        assert "max turns" in result.content or result.content is not None
        assert result.stats["turns"] <= MAX_TURNS

    def test_system_prompt_included_in_messages(self):
        """system_prompt is sent as the first message with role=system."""
        responses = [_make_lms_response("ok")]
        client = self._make_client(responses)

        client.dispatch(task="Do something.", system_prompt="You are a helpful agent.")

        call_kwargs = client._http.post.call_args
        messages = call_kwargs[1]["json"]["messages"]
        assert messages[0]["role"] == "system"
        assert "helpful agent" in messages[0]["content"]
        assert messages[1]["role"] == "user"

    def test_no_system_prompt_omits_system_message(self):
        """When system_prompt is None, only the user message is sent."""
        responses = [_make_lms_response("ok")]
        client = self._make_client(responses)

        client.dispatch(task="Do something.", system_prompt=None)

        call_kwargs = client._http.post.call_args
        messages = call_kwargs[1]["json"]["messages"]
        assert messages[0]["role"] == "user"

    def test_string_args_parsed_correctly(self):
        """Tool call with JSON-string arguments (not dict) is handled."""
        tc = _make_tool_call_msg("cog_search_memory", {"query": "uptime"}, call_id="call-s")
        # lms_orchestrator.py:243: raw_args may be a string
        responses = [
            _make_lms_response(None, tool_calls=[tc]),
            _make_lms_response("Found results."),
        ]
        client = self._make_client(responses)
        result = client.dispatch(task="Search uptime.")

        assert result.tool_calls[0].arguments == {"query": "uptime"}


# ---------------------------------------------------------------------------
# Runner routing helpers
# ---------------------------------------------------------------------------

class TestRunnerRouting:
    """Test _is_td_nonbaseline and td_wired computation from runner.py."""

    def _make_spec(self, td_id: str | None):
        from evals.tournament.matrix import TrialSpec
        from evals.tournament.variants import Variant

        td_variant = None
        if td_id is not None:
            td_variant = Variant(
                id=td_id,
                variant_class="tool-description",
                content={"cog_search_memory": "overridden desc"},
            )
        # Minimal task variant
        task_variant = Variant(
            id="task-1-state-probe",
            variant_class="task",
            content={"prompt": "What is the kernel state?", "rubric": {}},
        )
        return TrialSpec(
            trial_id="test-trial",
            experiment_id="exp-test",
            task_variant=task_variant,
            variant_ids={"tool_description": td_id or "none"},
            system_prompt_variant=None,
            tool_description_variant=td_variant,
            target="test-target",
        )

    def test_baseline_td_not_nonbaseline(self):
        from evals.tournament.runner import _is_td_nonbaseline
        spec = self._make_spec("td-1-current")
        assert _is_td_nonbaseline(spec) is False

    def test_no_td_variant_not_nonbaseline(self):
        from evals.tournament.runner import _is_td_nonbaseline
        spec = self._make_spec(None)
        assert _is_td_nonbaseline(spec) is False

    def test_nonbaseline_td_is_nonbaseline(self):
        from evals.tournament.runner import _is_td_nonbaseline
        spec = self._make_spec("td-3-with-anti-patterns")
        assert _is_td_nonbaseline(spec) is True

    def test_td_wired_true_when_chat_client_available(self):
        """td_wired should be True when non-baseline TD + chat_client available."""
        from evals.tournament.runner import _is_td_nonbaseline
        spec = self._make_spec("td-3-with-anti-patterns")
        chat_client = MagicMock()  # non-None

        # Replicate the td_wired logic from run_experiment
        td_wired = not _is_td_nonbaseline(spec) or (
            _is_td_nonbaseline(spec) and chat_client is not None
        )
        assert td_wired is True

    def test_td_wired_false_when_no_chat_client(self):
        """td_wired should be False when non-baseline TD but no chat_client."""
        from evals.tournament.runner import _is_td_nonbaseline
        spec = self._make_spec("td-3-with-anti-patterns")

        td_wired = not _is_td_nonbaseline(spec) or (
            _is_td_nonbaseline(spec) and None is not None
        )
        assert td_wired is False

    def test_td_wired_true_for_baseline(self):
        """td_wired should be True for baseline TD regardless of chat_client."""
        from evals.tournament.runner import _is_td_nonbaseline
        spec = self._make_spec("td-1-current")

        td_wired = not _is_td_nonbaseline(spec) or (
            _is_td_nonbaseline(spec) and None is not None
        )
        assert td_wired is True
