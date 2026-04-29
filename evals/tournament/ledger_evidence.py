"""Collect tool-call evidence for Claude trials from the kernel ledger.

When claude -p runs as a subprocess provider inside the kernel, it executes tools
natively via its own MCP session. The chat-completions response shape does NOT surface
these tool calls — only the final text reaches CompletionResponse. But the kernel
emits tool.call / tool.result events to the ledger for every invocation, queryable
via cog_read_tool_calls.

This module bridges that gap: given a (start_time, end_time) window and optional
filters, it queries the kernel ledger and returns a list of ToolCall objects in the
same shape that TrialRecord.tool_calls / the rubric scorer expect.

Filtering strategy
------------------
interaction_id is NOT shared across a chat-completions request — each individual
kernel tool call gets its own unique interaction_id. Time-window correlation is
therefore the primary filter.

We additionally filter source="mcp" and ownership="kernel" to exclude any
kernel-internal scheduled work (metabolic ticker, background agents, etc.) that
happens to land in the same window.

Risk: ambient MCP calls from other Claude Code sessions or background agents CAN
contaminate the window. Mitigations:
  1. Trials are run sequentially (no overlap from the runner itself).
  2. Windows are short (~3–15s per trial).
  3. A WARNING is emitted when >20 tool calls land in a single window (likely
     contamination from a concurrent session).

Known limitation: contamination from the user's live Claude Code session is real
but low-probability in practice. Document it; don't over-engineer.

Usage
-----
    collector = LedgerToolCallCollector(kernel_url="http://localhost:6931")

    start = datetime.now(timezone.utc)
    # ... run the trial ...
    end = datetime.now(timezone.utc)

    tool_calls = collector.collect(start, end)
    # Returns list[ToolCall] — populate AgenticResult.tool_calls
"""

from __future__ import annotations

import json as _json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from evals.harness.client import ToolCall
from evals.tournament.client_kernel import _MCPSession

log = logging.getLogger(__name__)

# Number of tool calls in one trial window that triggers a contamination warning.
_CONTAMINATION_WARN_THRESHOLD = 20


@dataclass
class CollectionStats:
    """Metadata about one ledger-collection pass."""
    window_start: str
    window_end: str
    raw_count: int
    returned_count: int
    warning: str = ""


class LedgerToolCallCollector:
    """Query the kernel ledger for tool.call events in a time window.

    Opens a dedicated MCP session for ledger queries — separate from the main
    dispatch session so they don't interfere. Call close() when done.
    """

    def __init__(
        self,
        kernel_url: str = "http://localhost:6931",
        timeout: float = 30.0,
    ) -> None:
        self._kernel_url = kernel_url.rstrip("/")
        self._timeout = timeout
        self._mcp: _MCPSession | None = None

    def _ensure_session(self) -> _MCPSession:
        """Lazily create and initialize the MCP session on first use."""
        if self._mcp is None:
            self._mcp = _MCPSession(base_url=self._kernel_url, timeout=self._timeout)
            self._mcp.initialize()
            log.debug("LedgerToolCallCollector: MCP session initialized")
        return self._mcp

    def collect(
        self,
        start: datetime,
        end: datetime,
        source: str = "mcp",
        ownership: str = "kernel",
        exclude_tool_names: set[str] | None = None,
    ) -> tuple[list[ToolCall], CollectionStats]:
        """Query ledger for tool calls within [start, end].

        Args:
            start: UTC datetime when the trial dispatch began.
            end: UTC datetime when the dispatch returned.
            source: Source filter — 'mcp' to exclude kernel-internal scheduler.
            ownership: Ownership filter — 'kernel' to match kernel-registered tools.
            exclude_tool_names: Set of tool names to exclude from results (e.g. the
                collector's own 'cog_read_tool_calls' and ledger/session tools that
                are not part of the harness evaluation surface).

        Returns:
            (tool_calls, stats) — tool_calls is in ToolCall shape usable by rubric scorer.
        """
        # Convert to RFC3339 with explicit Z suffix (kernel expects this format)
        since_str = _to_rfc3339(start)
        until_str = _to_rfc3339(end)

        stats = CollectionStats(
            window_start=since_str,
            window_end=until_str,
            raw_count=0,
            returned_count=0,
        )

        try:
            mcp = self._ensure_session()
            raw_result = mcp.tool_call(
                "cog_read_tool_calls",
                {
                    "since": since_str,
                    "until": until_str,
                    "source": source,
                    "ownership": ownership,
                    "include_args": True,
                    "include_output": True,
                    "order": "asc",
                    "limit": 500,
                },
            )
            # _MCPSession.tool_call returns the MCP result dict, which may be
            # content-wrapped ({"content": [{"type": "text", "text": "..."}]})
            # rather than the parsed JSON we want. Unwrap before use.
            result = _unwrap_mcp_result(raw_result)
        except Exception as e:
            log.warning(
                "LedgerToolCallCollector: ledger query failed: %s — returning empty tool list",
                e,
            )
            stats.warning = f"ledger query failed: {e}"
            return [], stats

        all_calls: list[dict[str, Any]] = result.get("calls", [])
        # Filter out infrastructure tool names (ledger queries, session tools, etc.)
        _exclude = exclude_tool_names or set()
        calls_raw = [c for c in all_calls if c.get("tool_name", "") not in _exclude]
        stats.raw_count = len(calls_raw)

        if len(calls_raw) > _CONTAMINATION_WARN_THRESHOLD:
            msg = (
                f"WARNING: {len(calls_raw)} tool calls landed in a single trial window "
                f"[{since_str} → {until_str}] — possible contamination from concurrent "
                "sessions. Inspect results carefully."
            )
            log.warning(msg)
            stats.warning = msg

        tool_calls: list[ToolCall] = []
        for entry in calls_raw:
            name = entry.get("tool_name", "")
            if not name:
                continue

            # arguments arrives as a dict (include_args=True) or absent
            args: dict[str, Any] = entry.get("arguments") or {}

            # output_summary is a truncated string (include_output=True); use as result
            output_summary: str | None = entry.get("output_summary")

            # Map status != success to an error sentinel in result
            status = entry.get("status", "success")
            if status not in ("success", "pending"):
                result_str: str | None = f"[{status}] {output_summary or ''}"
            else:
                result_str = output_summary

            call_id: str | None = entry.get("call_id")

            tool_calls.append(
                ToolCall(
                    name=name,
                    arguments=args,
                    result=result_str,
                    call_id=call_id,
                )
            )

        stats.returned_count = len(tool_calls)
        log.debug(
            "LedgerToolCallCollector: collected %d tool calls in window [%s → %s]",
            len(tool_calls),
            since_str,
            until_str,
        )
        return tool_calls, stats

    def close(self) -> None:
        if self._mcp is not None:
            self._mcp.close()
            self._mcp = None


def _to_rfc3339(dt: datetime) -> str:
    """Convert datetime to RFC3339 string with Z suffix (UTC assumed)."""
    # Kernel accepts ISO 8601 / RFC 3339 with Z or +00:00
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _unwrap_mcp_result(raw: Any) -> dict[str, Any]:
    """Unwrap MCP content-wrapped result into a plain dict.

    _MCPSession.tool_call() returns the MCP result dict, which for text
    responses is:
        {"content": [{"type": "text", "text": "{...json...}"}]}

    We need the parsed JSON inside the text field. If the result is already
    a plain dict with a "calls" key (direct struct return), return it as-is.
    """
    if isinstance(raw, dict) and "calls" in raw:
        # Already unwrapped (direct struct response)
        return raw

    # MCP content-wrapped shape
    content = raw.get("content") if isinstance(raw, dict) else None
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                try:
                    parsed = _json.loads(text)
                    if isinstance(parsed, dict):
                        return parsed
                except (_json.JSONDecodeError, ValueError):
                    pass

    # Fallback: return raw dict or empty dict
    return raw if isinstance(raw, dict) else {}
