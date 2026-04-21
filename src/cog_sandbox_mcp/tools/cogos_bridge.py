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

import base64
import binascii
import json
import os
import urllib.error
import urllib.parse
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


def _http_get_any_with_params(
    path: str,
    params: dict[str, Any] | None = None,
    timeout_s: float = 10.0,
) -> Any:
    """GET JSON with optional query-string params.

    Returns whatever the kernel returns (dict, list, or scalar) — the caller
    owns shape validation. Keys with None or "" values are skipped so callers
    can pass Optional filters straight through without pre-filtering.
    """
    base = _base_url()
    if not base:
        raise RuntimeError(
            "COG_OS_BASE_URL is not set; bridge tools should not have been registered"
        )
    url = f"{base}{path}"
    if params:
        qs = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None and v != ""}
        )
        if qs:
            url = f"{url}?{qs}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(body) if body else None
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


def cogos_emit(
    bus_id: str,
    message: str,
    from_sender: str = "cog-sandbox",
    event_type: str = "message",
) -> dict[str, Any]:
    """Emit an event onto a Cog OS bus channel.

    POSTs to the kernel's /v1/bus/send endpoint. On success, returns the kernel's
    JSON response verbatim (typically includes an event id / acknowledgement).
    On failure, returns a structured {"success": False, "error": ..., "bus_id":
    ...} payload rather than raising — same safe-probe contract as cogos_status.

    CALL THIS WHEN the user or the agent's own task requires sending a message,
    status update, or event onto a named Cog OS bus that downstream subsystems
    (other agents, external bridges, logs) subscribe to. If you do not know the
    bus_id, ask the user — do not invent one.

    Arguments:
      bus_id:      the channel name (e.g. "agent-smoke-test", "assistant-turns").
      message:     the event payload's human-readable text body.
      from_sender: identifier for the emitter (default "cog-sandbox"). Set this
                   to a more specific handle when emitting on behalf of a named
                   sub-agent or user.
      event_type:  the event type tag (default "message"). Use this to classify
                   events for downstream filtering.
    """
    payload: dict[str, Any] = {
        "bus_id": bus_id,
        "message": message,
        "from": from_sender,
        "type": event_type,
    }
    try:
        return _http_post_json("/v1/bus/send", payload)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        detail = f"HTTP {e.code} {e.reason}"
        if body:
            detail = f"{detail} — {body}"
        return {"success": False, "error": detail, "bus_id": bus_id}
    except urllib.error.URLError as e:
        return {
            "success": False,
            "error": f"{type(e).__name__}: {e}",
            "bus_id": bus_id,
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"{type(e).__name__}: {e}",
            "bus_id": bus_id,
        }


def cogos_events_read(
    bus_id: str,
    after_seq: int | None = None,
    event_type: str | None = None,
    from_sender: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Read events from a Cog OS bus (read-only; does NOT create the bus).

    GETs {COG_OS_BASE_URL}/v1/bus/{bus_id}/events with optional filters. Wraps
    the kernel's event array as {"bus_id", "events", "count"}. On failure —
    including 404 if the bus does not exist — returns {"success": False,
    "error": ..., "bus_id": ...} rather than raising.

    CALL THIS WHEN you need to inspect what has been emitted to a named bus —
    for example to verify a previous cogos_emit landed, replay recent events
    for context, or filter by type/sender to focus on specific signals.

    Unlike cogos_emit (which auto-creates the bus on first emit), this tool is
    purely read — it WILL NOT create a bus that does not exist. A 404 here
    almost always means the bus_id is wrong (typo, or never emitted to) rather
    than an environmental failure.

    Arguments:
      bus_id:       the channel to read (e.g. "agent-smoke-test").
      after_seq:    if set, only events with seq > this value (for tailing).
      event_type:   filter to a specific event type tag (e.g. "message").
      from_sender:  filter to a specific emitter identity.
      limit:        max events to return (default 100, kernel-side cap).
    """
    params: dict[str, Any] = {"limit": limit}
    if after_seq is not None:
        params["after"] = after_seq
    if event_type:
        params["type"] = event_type
    if from_sender:
        params["from"] = from_sender
    try:
        events = _http_get_any_with_params(f"/v1/bus/{bus_id}/events", params)
        if not isinstance(events, list):
            return {
                "success": False,
                "error": f"unexpected kernel response shape: {type(events).__name__}",
                "bus_id": bus_id,
            }
        return {"bus_id": bus_id, "events": events, "count": len(events)}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        detail = f"HTTP {e.code} {e.reason}"
        if body:
            detail = f"{detail} — {body}"
        return {"success": False, "error": detail, "bus_id": bus_id}
    except urllib.error.URLError as e:
        return {
            "success": False,
            "error": f"{type(e).__name__}: {e}",
            "bus_id": bus_id,
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"{type(e).__name__}: {e}",
            "bus_id": bus_id,
        }


def cogos_resolve(uri: str, decode: bool = True) -> dict[str, Any]:
    """Resolve a cog:// URI against the Cog OS kernel and return its contents.

    GETs {COG_OS_BASE_URL}/resolve?uri=<url-encoded cog-uri>. The kernel
    returns JSON of the form {"uri", "content": <base64>, ...} on success.
    On a bogus / missing URI the kernel returns HTTP 500 with
    {"error": {"message", "type"}} — in that case this tool returns
    {"success": False, "error": ..., "uri": ...} rather than raising.

    CALL THIS WHEN you need to read a specific resource addressed by its
    cog:// URI — e.g. an ADR (cog://adr/085), a memory entry, a source
    artifact. If you do not know the exact URI, prefer an upstream listing
    or query tool; do not guess URIs blindly.

    Decoding contract (the kernel always wire-encodes content as base64):
    - decode=True (default): try base64-decode then UTF-8-decode. On success,
      `content` is the decoded text and `raw_content` is absent. On failure
      (binary data, malformed base64), `content` and `raw_content` both hold
      the original base64 string, plus a `decode_error` note describing what
      failed.
    - decode=False: skip the decode attempt entirely. `content` stays base64
      and `raw_content` mirrors it — use this when you know the resource is
      binary and you want to pass the bytes through unchanged.

    Arguments:
      uri:    the full cog:// URI, e.g. "cog://adr/085".
      decode: whether to base64 + UTF-8 decode the content (default True).
    """
    try:
        resp = _http_get_any_with_params("/resolve", {"uri": uri})
    except urllib.error.HTTPError as e:
        body_raw = b""
        try:
            body_raw = e.read()
        except Exception:
            pass
        body_text = body_raw.decode("utf-8", errors="replace") if body_raw else ""
        kernel_error: Any = None
        try:
            parsed = json.loads(body_text) if body_text else None
            if isinstance(parsed, dict):
                kernel_error = parsed.get("error") or parsed
        except json.JSONDecodeError:
            pass
        detail = f"HTTP {e.code} {e.reason}"
        if kernel_error and isinstance(kernel_error, dict) and kernel_error.get("message"):
            detail = f"{detail} — {kernel_error['message']}"
        elif body_text:
            detail = f"{detail} — {body_text}"
        return {"success": False, "error": detail, "uri": uri}
    except urllib.error.URLError as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}", "uri": uri}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}", "uri": uri}

    if not isinstance(resp, dict):
        return {
            "success": False,
            "error": f"unexpected kernel response shape: {type(resp).__name__}",
            "uri": uri,
        }

    raw_b64 = resp.get("content")
    if not isinstance(raw_b64, str):
        # Pass through whatever the kernel said — if there's no content field
        # we don't have anything to decode and shouldn't pretend otherwise.
        return dict(resp)

    result = dict(resp)

    if not decode:
        result["content"] = raw_b64
        result["raw_content"] = raw_b64
        return result

    try:
        decoded_bytes = base64.b64decode(raw_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        result["content"] = raw_b64
        result["raw_content"] = raw_b64
        result["decode_error"] = f"base64 decode failed: {e}"
        return result

    try:
        result["content"] = decoded_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        result["content"] = raw_b64
        result["raw_content"] = raw_b64
        result["decode_error"] = f"utf-8 decode failed: {e}"

    return result


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
    mcp.tool(
        title="Emit event to Cog OS bus",
        annotations=ToolAnnotations(
            readOnlyHint=False, idempotentHint=False, openWorldHint=True
        ),
    )(cogos_emit)
    mcp.tool(
        title="Read events from Cog OS bus",
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=True
        ),
    )(cogos_events_read)
    mcp.tool(
        title="Resolve a cog:// URI",
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=True
        ),
    )(cogos_resolve)
