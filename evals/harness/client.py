"""LM Studio agentic chat client using /api/v1/chat with MCP plugin integration.

Unlike /v1/chat/completions (OpenAI-compat, stateless, requires tool schemas
passed in every request), /api/v1/chat runs the full multi-turn agent loop
server-side with LM Studio's loaded MCP plugins. We send one prompt, LM Studio
handles inference + tool invocation + result merging, and returns the complete
output trace.

Requires an LM Studio API token (server-side auth is enabled because plugin
use is gated). Set LMS_API_TOKEN in evals/.env or the environment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    result: str | None = None  # filled in when matched with a function_call_output
    call_id: str | None = None


@dataclass
class AgenticResult:
    content: str  # final assistant message text
    tool_calls: list[ToolCall]
    reasoning: str  # concatenated reasoning blocks (may be empty)
    output_types: list[str]  # sequence of output-item types, for debugging
    stats: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_unparsed": raw}
    return {"_unexpected_type": str(type(raw))}


def parse_output(output: list[dict[str, Any]]) -> tuple[str, list[ToolCall], str]:
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    calls: list[ToolCall] = []
    by_id: dict[str, ToolCall] = {}

    for item in output:
        t = item.get("type")
        if t == "reasoning":
            text = item.get("content") or item.get("summary") or ""
            if isinstance(text, list):
                text = "".join(
                    p.get("text", "") if isinstance(p, dict) else str(p) for p in text
                )
            reasoning_parts.append(str(text))
        elif t == "message":
            text = item.get("content") or ""
            if isinstance(text, list):
                text = "".join(
                    p.get("text", "") if isinstance(p, dict) else str(p) for p in text
                )
            content_parts.append(str(text))
        elif t in ("tool_call", "function_call", "mcp_tool_use"):
            # LM Studio's /api/v1/chat shape: single tool_call item with 'tool' (name)
            # and 'output' (result) inline. OpenAI's function_call uses 'name' in a
            # separate call; function_call_output holds the result. We handle both.
            name = item.get("tool") or item.get("name") or ""
            result = item.get("output")
            if isinstance(result, list):
                result = json.dumps(result)
            elif result is not None:
                result = str(result)
            cid = item.get("id") or item.get("call_id")
            tc = ToolCall(
                name=name,
                arguments=_parse_arguments(item.get("arguments", "{}")),
                result=result,
                call_id=cid,
            )
            calls.append(tc)
            if cid:
                by_id[cid] = tc
        elif t in ("function_call_output", "tool_result", "mcp_tool_result"):
            # OpenAI-style: result arrives as a separate item. Match by call_id.
            cid = item.get("call_id") or item.get("id")
            payload = item.get("output") or item.get("content") or ""
            if isinstance(payload, list):
                payload = json.dumps(payload)
            if cid and cid in by_id:
                by_id[cid].result = str(payload)

    return (
        "\n".join(p for p in content_parts if p),
        calls,
        "\n".join(p for p in reasoning_parts if p),
    )


class LMStudioAgenticClient:
    def __init__(
        self,
        base_url: str = "http://localhost:1234",
        api_token: str | None = None,
        timeout: float = 600.0,
    ):
        if not api_token:
            raise ValueError(
                "api_token is required. Set LMS_API_TOKEN in evals/.env or the environment."
            )
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self.http.close()

    def run(
        self,
        model: str,
        prompt: str,
        plugin_ids: list[str],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AgenticResult:
        """Send a single prompt; receive the full agent trace (multi-turn done server-side)."""
        body: dict[str, Any] = {
            "model": model,
            "input": prompt,
            "integrations": [
                {"type": "plugin", "id": pid} for pid in plugin_ids
            ],
        }
        # /api/v1/chat uses OpenAI-Responses-style field names. temperature/max_tokens
        # aren't accepted at the top level; the corresponding names here are different
        # and LM Studio-specific. Leaving these off for v0.1 — add once we identify the
        # right fields via friction.

        r = self.http.post(f"{self.base_url}/api/v1/chat", json=body)
        if r.status_code != 200:
            raise RuntimeError(
                f"/api/v1/chat returned {r.status_code}: {r.text[:500]}"
            )
        data = r.json()
        output = data.get("output", []) or []
        content, tool_calls, reasoning = parse_output(output)
        return AgenticResult(
            content=content,
            tool_calls=tool_calls,
            reasoning=reasoning,
            output_types=[item.get("type", "?") for item in output],
            stats=data.get("stats", {}) or {},
            raw=data,
        )
