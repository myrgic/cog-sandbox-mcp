"""Kernel-MCP dispatch client: tournament trials go through cog_dispatch_to_harness
instead of LM Studio's plugin endpoint. Eliminates the parallel inference path.
The kernel's harness controller handles model dispatch (Ollama gemma4:e4b),
tool execution (kernel-internal registry), and ledger writes.

NOTE on sequential execution: Ollama is single-threaded — firing concurrent
dispatches would serialize at the Ollama layer (N × single-slot wall-clock)
while competing with the metabolic ticker and any background harness work.
The runner loop here is therefore sequential by design.
See memory: feedback_ollama_single_thread_constraint.md
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from evals.harness.client import AgenticResult, ToolCall

log = logging.getLogger(__name__)

# MCP JSON-RPC protocol version
_MCP_PROTO_VERSION = "2024-11-05"


# ---------------------------------------------------------------------------
# MCP session helpers (lifted from scripts/harness-tests/lms_orchestrator.py)
# ---------------------------------------------------------------------------

def _parse_sse(raw: str) -> dict:
    """Parse MCP SSE response — may be bare JSON or 'data: {...}' framing."""
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    for line in raw.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise ValueError(f"could not parse MCP response: {raw[:200]}")


class _MCPSession:
    """Lightweight MCP session over HTTP. init → call → (close)."""

    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.mcp_url = f"{self.base_url}/mcp"
        self.timeout = timeout
        self.session_id: str = ""
        self._http = httpx.Client(
            timeout=timeout,
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        )

    def initialize(self) -> None:
        """MCP initialize → notifications/initialized handshake."""
        resp = self._http.post(
            self.mcp_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _MCP_PROTO_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "tournament-kernel-client", "version": "0.1"},
                },
            },
        )
        resp.raise_for_status()
        sid = resp.headers.get("Mcp-Session-Id")
        if not sid:
            raise RuntimeError(f"kernel MCP init: no Mcp-Session-Id in response headers. body={resp.text[:300]}")
        self.session_id = sid
        # Acknowledge
        self._http.post(
            self.mcp_url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={"Mcp-Session-Id": self.session_id},
        )
        log.debug("kernel MCP session initialized: %s", self.session_id)

    def call(self, method: str, params: dict | None = None, rid: int | None = None) -> dict:
        """Execute one JSON-RPC call; return the parsed response dict."""
        if rid is None:
            rid = uuid.uuid4().int & 0xFFFFFF
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params:
            body["params"] = params
        headers: dict[str, str] = {}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        resp = self._http.post(self.mcp_url, json=body, headers=headers)
        resp.raise_for_status()
        return _parse_sse(resp.text)

    def tool_call(self, name: str, arguments: dict) -> dict:
        """Call a kernel MCP tool by name; returns the result dict."""
        result = self.call("tools/call", {"name": name, "arguments": arguments})
        if "error" in result:
            raise RuntimeError(f"kernel tool {name!r} error: {result['error']}")
        return result.get("result", {})

    def close(self) -> None:
        self._http.close()


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_dispatch_result(raw: dict) -> AgenticResult:
    """Parse DispatchBatchResult JSON into AgenticResult.

    The kernel returns:
      {"results": [{"index":0, "success":bool, "content":str,
                    "tool_calls": [{"name":str, "args_digest":str, "result_digest":str}],
                    "error":str, "duration_sec":float, "turns":int}],
       "total_duration_sec": float}

    ToolCalls from the kernel carry digests, not full args/results (the full
    transcript is in the kernel ledger). We adapt to AgenticResult.ToolCall
    with name set and arguments/result left as digest stubs — enough for
    scoring.score() which only checks .name.
    """
    # The tool result content may be nested under result.content (MCP shape) or
    # come back as plain text content. Handle both.
    content_items = raw.get("content", [])
    text_content = ""
    if isinstance(content_items, list):
        for item in content_items:
            if isinstance(item, dict) and item.get("type") == "text":
                text_content += item.get("text", "")
    elif isinstance(content_items, str):
        text_content = content_items

    # If the result is already parsed JSON, it's a DispatchBatchResult
    batch: dict[str, Any] = {}
    if text_content:
        try:
            batch = json.loads(text_content)
        except json.JSONDecodeError:
            # Not JSON — treat as raw content string (error or partial)
            return AgenticResult(
                content=text_content,
                tool_calls=[],
                reasoning="",
                output_types=[],
                stats={},
                raw=raw,
            )
    else:
        # The tool result may itself be the dict (some MCP implementations
        # return structured data rather than serialized text)
        batch = raw

    results = batch.get("results", [])
    if not results:
        return AgenticResult(
            content=batch.get("error", "kernel returned empty results"),
            tool_calls=[],
            reasoning="",
            output_types=[],
            stats={"notes": batch.get("notes", [])},
            raw=raw,
        )

    slot = results[0]  # N=1 always for tournament trials
    content = slot.get("content", "")
    error = slot.get("error", "")
    if not slot.get("success", True) and error:
        content = f"[kernel dispatch error] {error}"

    raw_tool_calls = slot.get("tool_calls", []) or []
    tool_calls = [
        ToolCall(
            name=tc.get("name", ""),
            arguments={"_digest": tc.get("args_digest", "")},
            result=tc.get("result_digest"),
            call_id=None,
        )
        for tc in raw_tool_calls
        if tc.get("name")
    ]

    return AgenticResult(
        content=content,
        tool_calls=tool_calls,
        reasoning="",
        output_types=[],
        stats={
            "duration_sec": slot.get("duration_sec", 0.0),
            "turns": slot.get("turns", 0),
            "total_duration_sec": batch.get("total_duration_sec", 0.0),
            "notes": batch.get("notes", []),
            "model_used": str(slot.get("model_used", "")),
        },
        raw=raw,
    )


# ---------------------------------------------------------------------------
# KernelMCPClient
# ---------------------------------------------------------------------------

class KernelMCPClient:
    """Tournament dispatch client via kernel's cog_dispatch_to_harness MCP tool.

    Each instance holds one MCP session (initialize once, reuse across trials).
    Call .close() when done.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:6931",
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = _MCPSession(base_url=self.base_url, timeout=self.timeout)
        self._session.initialize()

    def dispatch(
        self,
        task: str,
        system_prompt: str | None = None,
        tools: list[str] | None = None,
        model: str = "e4b",
        iss: str | None = None,
        sub: str | None = None,
        timeout_seconds: int = 240,
        no_tools: bool = False,
    ) -> AgenticResult:
        """Dispatch one trial via cog_dispatch_to_harness.

        Maps to DispatchRequest fields in internal/engine/agent_dispatch.go:
          Task, SystemPrompt, Tools, Model, TimeoutSeconds, Identity.Iss/Sub
        N is always 1 for sequential tournament trials.

        no_tools=True activates parametric mode: sends tools=[] (empty allowlist)
        so the harness exposes no tools. The system prompt is prefixed with a
        directive telling the model to answer from knowledge directly.
        This isolates parametric knowledge from tool-assisted retrieval.
        """
        # Parametric mode: empty tool allowlist + prepend a no-tool directive.
        if no_tools:
            _PARAMETRIC_DIRECTIVE = (
                "Answer directly from your knowledge. "
                "Do not attempt tool calls. "
                "If you don't know, say so."
            )
            if system_prompt:
                system_prompt = f"{_PARAMETRIC_DIRECTIVE}\n\n{system_prompt}"
            else:
                system_prompt = _PARAMETRIC_DIRECTIVE
            # tools=[] signals an empty allowlist to the harness dispatcher.
            tools = []

        args: dict[str, Any] = {
            "task": task,
            "model": model,
            "n": 1,
            "timeout_seconds": timeout_seconds,
        }
        if system_prompt:
            args["system_prompt"] = system_prompt
        # Send tools even when empty list — empty allowlist is distinct from
        # None (which lets the harness use its default registry).
        if tools is not None:
            args["tools"] = tools
        if iss:
            args["iss"] = iss
        if sub:
            args["sub"] = sub

        log.debug("kernel dispatch: model=%s iss=%s sub=%s task=%s...", model, iss, sub, task[:80])

        try:
            raw_result = self._session.tool_call("cog_dispatch_to_harness", args)
        except Exception as e:
            log.warning("kernel dispatch failed: %s", e)
            raise

        return _parse_dispatch_result(raw_result)

    def close(self) -> None:
        self._session.close()
