"""Chat-completions client for TD-axis tournament trials.

Drives LM Studio's OpenAI-compatible /v1/chat/completions endpoint directly,
allowing per-trial tool-description overrides (the TD axis). Unlike
KernelMCPClient (which routes through cog_dispatch_to_harness and gets the
kernel's hardcoded tool registry), this client constructs the tools[] array
per-request and patches descriptions from the TD variant before sending.

Tool-call execution still goes through the kernel MCP (port 6931), so all
state mutations (ledger writes, attention boosts, etc.) land in the real kernel.

Architecture
------------
1. On __init__: open an MCP session with the kernel and enumerate the base
   tool list (the 7 orchestration tools in defaultLocalHarnessToolScope).
2. On dispatch():
   a. Apply TD description overrides (dict from Variant.content) to the base
      tool definitions to produce the per-trial tools[] array.
   b. Drive the LMS /v1/chat/completions turn loop (lifted from
      scripts/harness-tests/lms_orchestrator.py lines 171-264):
      - Send messages + tools[]; model responds with assistant message.
      - If tool_calls present: execute each via kernel MCP, append tool-result
        messages, loop.
      - If no tool_calls: final answer — exit loop.
3. Return AgenticResult (same shape as KernelMCPClient output).

Capped at MAX_TURNS=10 to avoid runaway loops on tasks that test retry
behaviour (task-4 invalid-URI path).

Reference: lms_orchestrator.py lines 153-264 (to_oai_tool, run_experiment).
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

# Maximum turns before we force-stop and return content so far.
MAX_TURNS = 10

# The 7 orchestration tools exposed to the model. This is the
# defaultLocalHarnessToolScope from internal/engine/local_agent_harness.go.
# Fetched at runtime from the kernel via tools/list; hardcoded names used
# as a filter so we never pass unintended tools to the LMS model.
_HARNESS_TOOL_NAMES = {
    "cog_search_memory",
    "cog_read_cogdoc",
    "cog_query_field",
    "cog_check_coherence",
    "cog_get_state",
    "cog_dispatch_to_harness",
    "cog_emit_event",
}


def _to_oai_tool(t: dict[str, Any], description_override: str | None = None) -> dict[str, Any]:
    """Convert a kernel MCP tool dict to an OpenAI tools[] entry.

    Mirrors lms_orchestrator.py:153-161 (to_oai_tool), with the addition of
    description_override for the TD-axis variant application.
    """
    desc = description_override if description_override is not None else (t.get("description") or "")
    # Cap description length to avoid token waste (lms_orchestrator.py:156 uses [:400])
    desc = desc[:500]
    return {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": desc,
            "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
        },
    }


def _parse_args(raw: Any) -> dict[str, Any]:
    """Parse tool-call arguments — may arrive as JSON string or dict.

    Mirrors lms_orchestrator.py:243-246 (json.loads + isinstance guard).
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_unparsed": raw}
    return {"_unexpected_type": str(type(raw))}


class ChatCompletionsClient:
    """Tournament dispatch client via LMS /v1/chat/completions with TD overrides.

    Mirrors KernelMCPClient's public interface:
      __init__, dispatch(task, system_prompt, tools, model, ...) → AgenticResult
      close()
    """

    def __init__(
        self,
        base_url: str = "http://localhost:1234",
        api_token: str = "",
        kernel_url: str = "http://localhost:6931",
        timeout: float = 120.0,
    ) -> None:
        if not api_token:
            raise ValueError(
                "api_token required. Set LMS_API_TOKEN in evals/.env."
            )
        self.base_url = base_url.rstrip("/")
        self.lms_url = f"{self.base_url}/v1/chat/completions"
        self.timeout = timeout

        self._http = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            },
        )

        # One persistent MCP session for kernel tool execution
        self._mcp = _MCPSession(base_url=kernel_url, timeout=timeout)
        self._mcp.initialize()

        # Fetch base tool list from kernel once; filter to harness scope
        self._base_tools: list[dict[str, Any]] = self._fetch_base_tools()
        log.debug(
            "ChatCompletionsClient: loaded %d base tools from kernel",
            len(self._base_tools),
        )

    def _fetch_base_tools(self) -> list[dict[str, Any]]:
        """Fetch tool schemas from the kernel MCP and filter to harness scope."""
        resp = self._mcp.call("tools/list")
        all_tools: list[dict[str, Any]] = resp.get("result", {}).get("tools", [])
        filtered = [t for t in all_tools if t["name"] in _HARNESS_TOOL_NAMES]
        if not filtered:
            log.warning(
                "ChatCompletionsClient: no harness tools found in kernel tool list "
                "(got %d tools total). Proceeding with empty tool set.",
                len(all_tools),
            )
        return filtered

    def _build_oai_tools(self, overrides: dict[str, str]) -> list[dict[str, Any]]:
        """Build the OpenAI tools[] array, patching descriptions from the TD variant.

        overrides: dict mapping tool_name → overridden_description string
                   (from Variant.content for tool-description variants).
        """
        return [
            _to_oai_tool(t, description_override=overrides.get(t["name"]))
            for t in self._base_tools
        ]

    def dispatch(
        self,
        task: str,
        system_prompt: str | None = None,
        td_overrides: dict[str, str] | None = None,
        model: str = "f29de68cb284ca208446e647b339569935025ef3",
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> AgenticResult:
        """Run a single trial via LMS chat completions with optional TD overrides.

        Args:
            task: User-role prompt (the task content).
            system_prompt: Optional system prompt override for the SP axis.
            td_overrides: Dict of tool_name → description string from the TD
                          variant cogdoc (Variant.content). None or {} uses
                          baseline descriptions from the kernel.
            model: LMS model ID (hash or display name).
            temperature: Sampling temperature.
            max_tokens: Per-turn token budget.

        Returns:
            AgenticResult with content, tool_calls, td_wired=True indicator
            baked into stats (the runner checks spec routing, not this field).
        """
        oai_tools = self._build_oai_tools(td_overrides or {})

        # Build initial message list (lms_orchestrator.py:184-187)
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": task})

        collected_tool_calls: list[ToolCall] = []
        output_types: list[str] = []
        final_content = ""
        total_input_tokens = 0
        total_output_tokens = 0

        # Turn loop — mirrors lms_orchestrator.py:191-264
        for turn in range(MAX_TURNS):
            log.debug("ChatCompletionsClient: turn %d/%d", turn + 1, MAX_TURNS)

            try:
                resp = self._http.post(
                    self.lms_url,
                    json={
                        "model": model,
                        "messages": messages,
                        "tools": oai_tools,
                        "tool_choice": "auto",
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                log.warning("ChatCompletionsClient: LMS request failed (turn %d): %s", turn + 1, e)
                final_content = f"[lms request error turn {turn + 1}] {type(e).__name__}: {e}"
                break

            if "choices" not in data:
                log.warning(
                    "ChatCompletionsClient: no 'choices' in LMS response (turn %d): %s",
                    turn + 1, str(data)[:200],
                )
                final_content = f"[lms error] {str(data)[:500]}"
                break

            # Accumulate token usage
            usage = data.get("usage") or {}
            total_input_tokens += usage.get("prompt_tokens", 0)
            total_output_tokens += usage.get("completion_tokens", 0)

            choice = data["choices"][0]
            msg = choice.get("message", {})
            finish_reason = choice.get("finish_reason")
            output_types.append(finish_reason or "unknown")

            # Append assistant message to history (lms_orchestrator.py:220-225)
            msg_for_history: dict[str, Any] = {"role": msg.get("role", "assistant")}
            if msg.get("content"):
                msg_for_history["content"] = msg["content"]
            if msg.get("tool_calls"):
                msg_for_history["tool_calls"] = msg["tool_calls"]
            messages.append(msg_for_history)

            # No tool_calls → final answer (lms_orchestrator.py:237-238)
            if not msg.get("tool_calls"):
                final_content = msg.get("content", "")
                log.debug(
                    "ChatCompletionsClient: final answer on turn %d (finish=%s)",
                    turn + 1, finish_reason,
                )
                break

            # Execute each tool call via kernel MCP (lms_orchestrator.py:240-262)
            for tc in msg["tool_calls"]:
                fn = tc["function"]
                name = fn["name"]
                raw_args = fn.get("arguments", "{}")
                args = _parse_args(raw_args)
                call_id = tc.get("id") or str(uuid.uuid4())

                log.debug("ChatCompletionsClient: tool call %s(%r)", name, args)
                try:
                    result_raw = self._mcp.tool_call(name, args)
                    # tool_call returns the result dict from MCP
                    result_text = json.dumps(result_raw)[:8000]
                    result_str: str | None = result_text
                except Exception as e:
                    log.warning(
                        "ChatCompletionsClient: tool %s failed: %s", name, e
                    )
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

                # Append tool result message for next turn
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": result_text,
                    }
                )
        else:
            # Exhausted MAX_TURNS
            log.warning(
                "ChatCompletionsClient: reached MAX_TURNS (%d) without final answer", MAX_TURNS
            )
            final_content = final_content or "(max turns reached)"

        return AgenticResult(
            content=final_content,
            tool_calls=collected_tool_calls,
            reasoning="",  # gemma4 has no reasoning tokens
            output_types=output_types,
            stats={
                "turns": len(output_types),
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "td_overrides_applied": len(td_overrides) if td_overrides else 0,
                "client": "chat_completions",
            },
            raw={},
        )

    def close(self) -> None:
        self._http.close()
        self._mcp.close()
