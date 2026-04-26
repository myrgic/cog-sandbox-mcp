"""Claude baseline dispatch client — routes through the kernel's claude-code provider.

Sends trials to kernel /v1/chat/completions with model="sonnet", which the kernel
router maps to the claude-code provider (spawns `claude -p` subprocess using the
host's Claude Max subscription via OAuth keychain). Zero incremental API cost.

This is Path A from the implementation spec: reuse the chat-completions turn loop
from client_chat.py, redirect base_url to the kernel instead of LM Studio, and
require no LMS_API_TOKEN (kernel handles auth internally via claude-code subprocess).

Architecture
------------
1. On __init__: open an MCP session with the kernel for tool execution; no LMS
   connection needed.
2. On dispatch():
   a. Build tool list from kernel MCP (same _HARNESS_TOOL_NAMES filter).
   b. Drive kernel /v1/chat/completions turn loop with model=sonnet.
   c. Execute tool calls via kernel MCP session (same as ChatCompletionsClient).
3. Return AgenticResult (same shape as KernelMCPClient output).

NO API KEY PATH: this client MUST NOT accept or use ANTHROPIC_API_KEY. It relies
exclusively on the kernel's claude-code subprocess provider. If the kernel's
/v1/chat/completions endpoint errors, we surface the error — no fallback to direct
Anthropic API calls.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx

from evals.harness.client import AgenticResult, ToolCall
from evals.tournament.client_kernel import _MCPSession

log = logging.getLogger(__name__)

# Maximum turns before force-stop (same as ChatCompletionsClient).
MAX_TURNS = 10

# The kernel harness tool scope (same set used by ChatCompletionsClient).
# Fetched at runtime from kernel; this set is used as a filter.
_HARNESS_TOOL_NAMES = {
    "cog_search_memory",
    "cog_read_cogdoc",
    "cog_query_field",
    "cog_check_coherence",
    "cog_get_state",
    "cog_dispatch_to_harness",
    "cog_emit_event",
}

# Model name the kernel router resolves to the claude-code provider.
# Confirmed via boot log: "router: registered name=claude-code model=sonnet"
CLAUDE_CODE_MODEL = "sonnet"


def _to_oai_tool(t: dict[str, Any]) -> dict[str, Any]:
    """Convert a kernel MCP tool dict to an OpenAI tools[] entry.

    No description override needed for the Claude baseline — we always pass
    the canonical kernel tool descriptions. TD axis variants are not applicable
    to the Claude baseline (it measures raw model capability under stock descriptions).
    """
    desc = (t.get("description") or "")[:500]
    return {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": desc,
            "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
        },
    }


def _parse_args(raw: Any) -> dict[str, Any]:
    """Parse tool-call arguments — may arrive as JSON string or dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_unparsed": raw}
    return {"_unexpected_type": str(type(raw))}


class ClaudeCodeClient:
    """Tournament dispatch client via kernel /v1/chat/completions with claude-code provider.

    Routes to the kernel's OpenAI-compatible chat completions endpoint using
    model="sonnet", which the kernel router resolves to the claude-code subprocess
    provider. No LMS_API_TOKEN required — no Anthropic API key required.

    Public interface mirrors KernelMCPClient and ChatCompletionsClient:
      __init__, dispatch(task, system_prompt, ...) → AgenticResult, close()
    """

    def __init__(
        self,
        kernel_url: str = "http://localhost:6931",
        timeout: float = 120.0,
        model: str = CLAUDE_CODE_MODEL,
    ) -> None:
        self.kernel_url = kernel_url.rstrip("/")
        self.chat_url = f"{self.kernel_url}/v1/chat/completions"
        self.timeout = timeout
        self.model = model

        # HTTP client — no Authorization header needed; kernel handles auth
        # through its claude-code subprocess provider (OAuth keychain).
        self._http = httpx.Client(
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )

        # MCP session for kernel tool execution (same approach as ChatCompletionsClient)
        self._mcp = _MCPSession(base_url=self.kernel_url, timeout=timeout)
        self._mcp.initialize()

        # Fetch base tool list from kernel once; filter to harness scope
        self._base_tools: list[dict[str, Any]] = self._fetch_base_tools()
        log.debug(
            "ClaudeCodeClient: loaded %d base tools from kernel (model=%s)",
            len(self._base_tools),
            self.model,
        )

    def _fetch_base_tools(self) -> list[dict[str, Any]]:
        """Fetch tool schemas from the kernel MCP and filter to harness scope."""
        resp = self._mcp.call("tools/list")
        all_tools: list[dict[str, Any]] = resp.get("result", {}).get("tools", [])
        filtered = [t for t in all_tools if t["name"] in _HARNESS_TOOL_NAMES]
        if not filtered:
            log.warning(
                "ClaudeCodeClient: no harness tools found in kernel tool list "
                "(got %d tools total). Proceeding with empty tool set.",
                len(all_tools),
            )
        return filtered

    def dispatch(
        self,
        task: str,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int = 1024,
    ) -> AgenticResult:
        """Run a single trial via kernel /v1/chat/completions → claude-code provider.

        Args:
            task: User-role prompt (the task content).
            system_prompt: Optional system prompt override for the SP axis.
            model: Override model name (defaults to self.model = "sonnet").
            max_tokens: Per-turn token budget.

        Returns:
            AgenticResult with content, tool_calls, and stats.

        Raises:
            RuntimeError: If kernel /v1/chat/completions returns a non-2xx status
                          and no retry is possible. Caller's exception handler in
                          runner.py will record this as a FAIL trial.
        """
        effective_model = model or self.model
        oai_tools = [_to_oai_tool(t) for t in self._base_tools]

        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": task})

        collected_tool_calls: list[ToolCall] = []
        output_types: list[str] = []
        final_content = ""
        total_input_tokens = 0
        total_output_tokens = 0

        # Turn loop — same structure as ChatCompletionsClient.dispatch()
        for turn in range(MAX_TURNS):
            log.debug(
                "ClaudeCodeClient: turn %d/%d (model=%s)", turn + 1, MAX_TURNS, effective_model
            )

            try:
                resp = self._http.post(
                    self.chat_url,
                    json={
                        "model": effective_model,
                        "messages": messages,
                        "tools": oai_tools,
                        "tool_choice": "auto",
                        "max_tokens": max_tokens,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as e:
                log.warning(
                    "ClaudeCodeClient: kernel /v1/chat/completions HTTP error (turn %d): %s %s",
                    turn + 1, e.response.status_code, e.response.text[:300],
                )
                final_content = (
                    f"[kernel chat error turn {turn + 1}] "
                    f"HTTP {e.response.status_code}: {e.response.text[:200]}"
                )
                break
            except Exception as e:
                log.warning(
                    "ClaudeCodeClient: request failed (turn %d): %s", turn + 1, e
                )
                final_content = (
                    f"[kernel chat error turn {turn + 1}] {type(e).__name__}: {e}"
                )
                break

            if "choices" not in data:
                log.warning(
                    "ClaudeCodeClient: no 'choices' in response (turn %d): %s",
                    turn + 1, str(data)[:200],
                )
                final_content = f"[kernel chat error] {str(data)[:500]}"
                break

            # Accumulate token usage
            usage = data.get("usage") or {}
            total_input_tokens += usage.get("prompt_tokens", 0)
            total_output_tokens += usage.get("completion_tokens", 0)

            choice = data["choices"][0]
            msg = choice.get("message", {})
            finish_reason = choice.get("finish_reason")
            output_types.append(finish_reason or "unknown")

            # Append assistant message to history
            msg_for_history: dict[str, Any] = {"role": msg.get("role", "assistant")}
            if msg.get("content"):
                msg_for_history["content"] = msg["content"]
            if msg.get("tool_calls"):
                msg_for_history["tool_calls"] = msg["tool_calls"]
            messages.append(msg_for_history)

            # No tool_calls → final answer
            if not msg.get("tool_calls"):
                final_content = msg.get("content", "")
                log.debug(
                    "ClaudeCodeClient: final answer on turn %d (finish=%s)",
                    turn + 1, finish_reason,
                )
                break

            # Execute each tool call via kernel MCP
            for tc in msg["tool_calls"]:
                fn = tc["function"]
                name = fn["name"]
                raw_args = fn.get("arguments", "{}")
                args = _parse_args(raw_args)
                call_id = tc.get("id") or str(uuid.uuid4())

                log.debug("ClaudeCodeClient: tool call %s(%r)", name, args)
                try:
                    result_raw = self._mcp.tool_call(name, args)
                    result_text = json.dumps(result_raw)[:8000]
                    result_str: str | None = result_text
                except Exception as e:
                    log.warning("ClaudeCodeClient: tool %s failed: %s", name, e)
                    result_text = json.dumps({"error": f"{type(e).__name__}: {e}"})
                    result_str = result_text

                collected_tool_calls.append(
                    ToolCall(
                        name=name,
                        arguments=args,
                        result=result_str,
                        call_id=call_id,
                    )
                )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": result_text,
                    }
                )
        else:
            log.warning(
                "ClaudeCodeClient: reached MAX_TURNS (%d) without final answer", MAX_TURNS
            )
            final_content = final_content or "(max turns reached)"

        return AgenticResult(
            content=final_content,
            tool_calls=collected_tool_calls,
            reasoning="",
            output_types=output_types,
            stats={
                "turns": len(output_types),
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "client": "claude_code",
                "model": effective_model,
            },
            raw={},
        )

    def close(self) -> None:
        self._http.close()
        self._mcp.close()
