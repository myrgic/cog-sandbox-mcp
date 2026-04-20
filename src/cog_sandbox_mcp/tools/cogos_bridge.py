"""Bridge tools that proxy to a reachable Cog OS kernel.

The Cog OS Go kernel (`.cog/cog`) exposes an HTTP API on port 5100 (by default)
with OpenAI-compat endpoints plus CogOS-native routes (`/health`, `/resolve`,
`/mutate`, `/ws/watch`, fleet/emit endpoints). When the env var COG_OS_BASE_URL
is set and the kernel is reachable, these bridge tools become available to the
agent — exposing CogOS primitives (fleet spawn, event ledger, memory query via
CQL) through MCP without reimplementing them in the sandbox.

When COG_OS_BASE_URL is unset, these tools are NOT registered at all — the
sandbox operates purely standalone with its existing surface. This is the
mediator/kernel layering made concrete: the sandbox is self-sufficient; the
bridges appear when the kernel does.

See `project_cog_os_layering.md` in memory for the architectural frame.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations


def _base_url() -> str | None:
    url = os.environ.get("COG_OS_BASE_URL", "").strip()
    return url.rstrip("/") if url else None


def is_bridge_enabled() -> bool:
    """Bridges are registered iff COG_OS_BASE_URL is set at server startup."""
    return _base_url() is not None


def _http_get_json(path: str, timeout_s: float = 10.0) -> dict[str, Any]:
    base = _base_url()
    if not base:
        raise RuntimeError(
            "COG_OS_BASE_URL is not set; bridge tools should not have been registered"
        )
    url = f"{base}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(body) if body else {}
    except json.JSONDecodeError:
        return {"_raw": body}


def _http_post_json(
    path: str, payload: dict[str, Any], timeout_s: float = 30.0
) -> dict[str, Any]:
    base = _base_url()
    if not base:
        raise RuntimeError(
            "COG_OS_BASE_URL is not set; bridge tools should not have been registered"
        )
    url = f"{base}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(body) if body else {}
    except json.JSONDecodeError:
        return {"_raw": body}


def cogos_status() -> dict[str, Any]:
    """Check whether the configured Cog OS kernel is currently reachable.

    Returns kernel health information if reachable, or a structured error if the
    HTTP request fails. This is a safe read-only probe — useful for agents to
    verify the bridge is alive before attempting fleet/emit/query operations.
    """
    base = _base_url()
    try:
        info = _http_get_json("/health")
        return {"reachable": True, "base_url": base, "info": info}
    except urllib.error.URLError as e:
        return {
            "reachable": False,
            "base_url": base,
            "error": f"{type(e).__name__}: {e}",
        }
    except Exception as e:
        return {
            "reachable": False,
            "base_url": base,
            "error": f"{type(e).__name__}: {e}",
        }


def register(mcp: FastMCP) -> None:
    """Register bridge tools with the MCP server.

    Caller must check is_bridge_enabled() first. Registration is a no-op if the
    env var is not set — skip the register() call at startup rather than gating
    inside each tool.
    """
    if not is_bridge_enabled():
        return
    mcp.tool(
        title="Cog OS kernel status",
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=True
        ),
    )(cogos_status)
