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
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations


# Well-known bus names reserved by the handoff protocol. See
# docs/HANDOFF_PROTOCOL.md §Well-known buses. Users can still emit to other
# buses for domain channels; these are the substrate-reserved ones.
BUS_SESSIONS = "bus_sessions"
BUS_HANDOFFS = "bus_handoffs"


def _utc_now_iso() -> str:
    """RFC3339-ish UTC timestamp suitable for created_at / claimed_at fields."""
    return datetime.now(timezone.utc).isoformat()


def _new_handoff_id() -> str:
    """Generate a handoff identifier.

    Protocol recommends ULIDs for sortability; we fall back to a timestamp-
    prefixed uuid4 suffix since ulid is not a runtime dep. Format:
    ``ho-<unix-ms>-<uuid4 short>`` — monotonic by emit time within a single
    process, good enough for the first-wins-by-seq guarantee which is actually
    enforced by the kernel's event sequence anyway.
    """
    ms = int(time.time() * 1000)
    suffix = uuid.uuid4().hex[:12]
    return f"ho-{ms}-{suffix}"


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


def cogos_session_register(
    session_id: str,
    workspace: str,
    role: str,
    task: str,
    model: str | None = None,
    hostname: str | None = None,
) -> dict[str, Any]:
    """Announce a session's presence on the shared ``bus_sessions`` channel.

    Emits a ``session.register`` event with a JSON payload per
    docs/HANDOFF_PROTOCOL.md §Session lifecycle. Delegates to ``cogos_emit`` so
    the never-raise contract (structured ``{"success": False, ...}`` on failure)
    is inherited automatically. The event's ``from`` field is set to
    ``session_id`` so all downstream emissions are attributable to this session.

    CALL THIS WHEN a new agent session comes online and wants to participate in
    the cross-session substrate — e.g. at the top of a Claude Code session that
    may later receive a handoff, coordinate with other sessions, or emit
    heartbeats. Pair with ``cogos_session_heartbeat`` on an interval and
    ``cogos_session_end`` at teardown. If you do not know the ``session_id``,
    construct one per the protocol (``<hostname>-<workspace-slug>-<role-or-
    ulid>``) — do not invent arbitrary identifiers; ask the user if unsure.

    Arguments:
      session_id: stable identifier for this session (ASCII, lowercase,
                  ``[a-z0-9-]``). Used as ``from`` on every emit from this
                  session.
      workspace:  absolute path to the working directory / repo root this
                  session is operating in.
      role:       human-meaningful role label ("manager", "worker-1",
                  "researcher"). Free-form string.
      task:       one-line description of what this session is doing.
      model:      optional model identifier (e.g. "claude-opus-4-7"). Helpful
                  for cross-session triage; omit if unknown.
      hostname:   optional hostname; recommended when multiple machines
                  participate in the same bus.

    Contract: returns whatever ``cogos_emit`` returns — the kernel's response
    verbatim on success, or a ``{"success": False, "error": ..., "bus_id":
    "bus_sessions"}`` payload on failure.
    """
    payload: dict[str, Any] = {
        "session_id": session_id,
        "workspace": workspace,
        "role": role,
        "task": task,
        "started_at": _utc_now_iso(),
    }
    if model is not None:
        payload["model"] = model
    if hostname is not None:
        payload["hostname"] = hostname
    return cogos_emit(
        bus_id=BUS_SESSIONS,
        message=json.dumps(payload),
        from_sender=session_id,
        event_type="session.register",
    )


def cogos_session_heartbeat(
    session_id: str,
    status: str = "active",
    context_usage: float | None = None,
    current_task: str | None = None,
) -> dict[str, Any]:
    """Emit a periodic keep-alive for the session's presence.

    Emits ``session.heartbeat`` on ``bus_sessions`` per HANDOFF_PROTOCOL.md.
    Downstream roster queries (``cogos_sessions_list``) infer liveness from the
    presence of a recent heartbeat. Sessions typically heartbeat every ~5
    minutes; absence of a heartbeat for 2× interval marks the session inactive.

    CALL THIS WHEN the agent wants to signal "still alive / working" so peers
    and dashboards see the session as active, or to publish a status transition
    (``active`` → ``idle`` / ``paused`` / ``ending``). Also useful for
    surfacing context_usage so handoff tooling can decide when to trigger a
    handoff before exhaustion.

    Arguments:
      session_id:     this session's identifier — must match the one registered
                      via ``cogos_session_register``.
      status:         one of ``"active" | "idle" | "paused" | "ending"``. Not
                      validated at client; passed through to the payload.
      context_usage:  fraction of context used in ``[0.0, 1.0]``. Optional.
      current_task:   short string describing what the session is doing right
                      now. Optional.

    Contract: returns whatever ``cogos_emit`` returns. Structured error on
    failure; does not raise.
    """
    payload: dict[str, Any] = {
        "session_id": session_id,
        "status": status,
        "last_tool_use_at": _utc_now_iso(),
    }
    if context_usage is not None:
        payload["context_usage"] = context_usage
    if current_task is not None:
        payload["current_task"] = current_task
    return cogos_emit(
        bus_id=BUS_SESSIONS,
        message=json.dumps(payload),
        from_sender=session_id,
        event_type="session.heartbeat",
    )


def cogos_session_end(
    session_id: str,
    reason: str = "user-quit",
    handoff_id: str | None = None,
) -> dict[str, Any]:
    """Mark a session as closing cleanly.

    Emits ``session.end`` on ``bus_sessions``. Optional but recommended — peers
    and dashboards use this to distinguish a graceful shutdown from a crashed /
    stalled session (which would appear inactive only after heartbeat gap).

    CALL THIS WHEN the session is winding down for any reason: task complete,
    context exhausted, user quit, or it has handed off to a successor. If a
    handoff was offered, pass the ``handoff_id`` so viewers can link end →
    offer → claim → complete as a chain.

    Arguments:
      session_id: this session's identifier.
      reason:     one of ``"task-complete" | "context-exhausted" | "user-quit"
                  | "handed-off" | "error"``. Not validated at client.
      handoff_id: if this end is coupled to a handoff offer, the offer's id.
                  Omit otherwise.

    Contract: returns whatever ``cogos_emit`` returns.
    """
    payload: dict[str, Any] = {
        "session_id": session_id,
        "ended_at": _utc_now_iso(),
        "reason": reason,
    }
    if handoff_id is not None:
        payload["handoff_id"] = handoff_id
    return cogos_emit(
        bus_id=BUS_SESSIONS,
        message=json.dumps(payload),
        from_sender=session_id,
        event_type="session.end",
    )


def _parse_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Best-effort extract a dict payload from a bus event.

    The substrate stores the payload JSON-encoded in the event's ``payload``
    field (under ``content`` when the kernel wraps it). Try both shapes; fall
    back to an empty dict when parsing fails so aggregators stay robust against
    malformed events rather than crashing the roster read.
    """
    raw = event.get("payload")
    if isinstance(raw, dict):
        content = raw.get("content")
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        # Already a structured payload — return as-is.
        if raw and "content" not in raw:
            return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {}


def cogos_sessions_list(active_within_seconds: int = 600) -> dict[str, Any]:
    """List sessions seen recently on ``bus_sessions``.

    Reads the last 500 events on ``bus_sessions`` via ``cogos_events_read`` and
    aggregates them into a per-session roster. For each ``session_id`` we
    track the latest event and compute an ``active`` flag: a session counts as
    active iff its last register/heartbeat is within ``active_within_seconds``
    AND no ``session.end`` followed it.

    CALL THIS WHEN you need a snapshot of who else is on the bus — e.g. before
    offering a handoff to a specific session, when triaging a stuck multi-
    session workflow, or when a dashboard wants to show currently-online
    agents. For a live tail, poll this periodically or read the bus directly.

    Arguments:
      active_within_seconds: freshness window (default 600 = 10 min). Sessions
                             whose last heartbeat/register is older than this
                             are reported but flagged ``"active": False``.

    Contract: returns ``{"sessions": [...], "count": N}`` on success, or the
    underlying ``cogos_events_read`` structured error on failure.
    """
    read = cogos_events_read(bus_id=BUS_SESSIONS, limit=500)
    if read.get("success") is False:
        return read
    events = read.get("events") or []

    # Walk oldest → newest so latest wins; events_read returns in seq order.
    by_session: dict[str, dict[str, Any]] = {}
    for ev in events:
        event_type = ev.get("type", "")
        if not isinstance(event_type, str) or not event_type.startswith("session."):
            continue
        payload = _parse_payload(ev)
        sid = payload.get("session_id") or ev.get("from")
        if not isinstance(sid, str) or not sid:
            continue
        entry = by_session.setdefault(
            sid,
            {
                "session_id": sid,
                "status": None,
                "role": None,
                "task": None,
                "workspace": None,
                "last_seen": None,
                "last_event_type": None,
                "context_usage": None,
                "_ended": False,
            },
        )
        entry["last_event_type"] = event_type
        # Prefer an explicit timestamp in the payload; fall back to the event's
        # own timestamp field if present.
        ts = (
            payload.get("last_tool_use_at")
            or payload.get("started_at")
            or payload.get("ended_at")
            or ev.get("ts")
            or ev.get("time")
        )
        if ts:
            entry["last_seen"] = ts
        if event_type == "session.register":
            entry["role"] = payload.get("role", entry["role"])
            entry["task"] = payload.get("task", entry["task"])
            entry["workspace"] = payload.get("workspace", entry["workspace"])
            entry["status"] = entry["status"] or "active"
            entry["_ended"] = False
        elif event_type == "session.heartbeat":
            entry["status"] = payload.get("status", entry["status"])
            if "context_usage" in payload:
                entry["context_usage"] = payload["context_usage"]
            if "current_task" in payload:
                entry["task"] = payload["current_task"]
        elif event_type == "session.end":
            entry["status"] = "ended"
            entry["_ended"] = True

    # Compute active within the freshness window.
    now = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for entry in by_session.values():
        active = not entry["_ended"]
        last_seen = entry.get("last_seen")
        if active and isinstance(last_seen, str):
            try:
                parsed = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                active = (now - parsed).total_seconds() <= active_within_seconds
            except ValueError:
                # Unparseable timestamp — be conservative and include the row
                # but flag it as inactive.
                active = False
        elif active and last_seen is None:
            active = False
        entry["active"] = active
        entry.pop("_ended", None)
        out.append(entry)

    return {"sessions": out, "count": len(out)}


def cogos_handoff_offer(
    from_session: str,
    task: dict[str, Any],
    bootstrap_prompt: str,
    to_session: str | None = None,
    reason: str = "explicit",
    ttl_seconds: int = 3600,
    bus_context_refs: list[dict[str, Any]] | None = None,
    memory_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Publish a handoff offer onto ``bus_handoffs``.

    Writes a ``handoff.offer`` event whose payload matches
    docs/HANDOFF_PROTOCOL.md §handoff.offer exactly. The tool generates a new
    ``handoff_id``, stamps ``created_at``, and delegates the actual wire send
    to ``cogos_emit``.

    CALL THIS WHEN this session wants to hand its task off to a fresh-context
    successor — either because context is near exhausted, the task is pausing,
    or decomposing into a worker. Write the ``bootstrap_prompt`` as a brief for
    a smart colleague walking in cold: critical invariants, what's been done,
    what to do next, verification gates. The successor reads it verbatim as
    their first turn. Set ``to_session=None`` for an open offer (any fresh
    session can claim); set it to a specific session_id for a targeted handoff.

    Arguments:
      from_session:     this session's identifier.
      task:             dict per the protocol: required keys ``title``,
                        ``goal``, and a non-empty ``next_steps`` list. Optional
                        keys (may be empty lists / strings): ``progress_summary``,
                        ``files_touched``, ``files_pending``, ``decisions_made``,
                        ``open_questions``, ``verification_gates``.
      bootstrap_prompt: the load-bearing field — the text given to the
                        successor as its first user turn.
      to_session:       target session_id, or None for an open offer.
      reason:           short label for why the handoff (e.g. ``"explicit"``,
                        ``"context-exhaustion"``, ``"decomposition"``).
      ttl_seconds:      offer expiry window; after this the offer is stale and
                        should not be claimed.
      bus_context_refs: list of ``{"bus_id", "after_seq"}`` pointing at
                        conversational buses the successor should read for
                        context. Optional.
      memory_refs:      list of ``cog://`` URIs pointing at CogDocs / memory
                        entries with state too large to inline. Optional.

    Contract: returns ``{"handoff_id": "...", "emit_result": <kernel response
    or error dict>}``. Validation errors surface as ``{"success": False,
    "error": ...}`` without contacting the kernel.
    """
    # Minimal validation per the spec — title, goal, and next_steps must be
    # non-empty. Everything else is passed through verbatim.
    if not isinstance(task, dict):
        return {
            "success": False,
            "error": f"task must be a dict, got {type(task).__name__}",
        }
    title = task.get("title")
    goal = task.get("goal")
    next_steps = task.get("next_steps")
    if not isinstance(title, str) or not title.strip():
        return {"success": False, "error": "task.title must be a non-empty string"}
    if not isinstance(goal, str) or not goal.strip():
        return {"success": False, "error": "task.goal must be a non-empty string"}
    if not isinstance(next_steps, list) or not next_steps:
        return {"success": False, "error": "task.next_steps must be a non-empty list"}

    handoff_id = _new_handoff_id()
    payload: dict[str, Any] = {
        "handoff_id": handoff_id,
        "from_session": from_session,
        "to_session": to_session,
        "reason": reason,
        "created_at": _utc_now_iso(),
        "ttl_seconds": ttl_seconds,
        "task": task,
        "bootstrap_prompt": bootstrap_prompt,
        "bus_context_refs": list(bus_context_refs) if bus_context_refs else [],
        "memory_refs": list(memory_refs) if memory_refs else [],
    }
    emit_result = cogos_emit(
        bus_id=BUS_HANDOFFS,
        message=json.dumps(payload),
        from_sender=from_session,
        event_type="handoff.offer",
    )
    return {"handoff_id": handoff_id, "emit_result": emit_result}


def _aggregate_handoffs(
    events: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Group bus_handoffs events by handoff_id and compute current state.

    State machine: ``open`` → ``claimed`` → ``complete``. The latest event wins
    (events are seq-ordered by the kernel). Unknown event types are ignored.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for ev in events:
        event_type = ev.get("type", "")
        if not isinstance(event_type, str) or not event_type.startswith("handoff."):
            continue
        payload = _parse_payload(ev)
        hid = payload.get("handoff_id")
        if not isinstance(hid, str) or not hid:
            continue
        entry = by_id.setdefault(
            hid,
            {
                "handoff_id": hid,
                "state": None,
                "offer_payload": None,
                "claim_payload": None,
                "complete_payload": None,
                "from_session": None,
                "to_session": None,
                "reason": None,
                "created_at": None,
                "ttl_seconds": None,
                "task_title": None,
            },
        )
        if event_type == "handoff.offer":
            entry["state"] = "open"
            entry["offer_payload"] = payload
            entry["from_session"] = payload.get("from_session")
            entry["to_session"] = payload.get("to_session")
            entry["reason"] = payload.get("reason")
            entry["created_at"] = payload.get("created_at")
            entry["ttl_seconds"] = payload.get("ttl_seconds")
            task = payload.get("task")
            if isinstance(task, dict):
                entry["task_title"] = task.get("title")
        elif event_type == "handoff.claim":
            entry["state"] = "claimed"
            entry["claim_payload"] = payload
        elif event_type == "handoff.complete":
            entry["state"] = "complete"
            entry["complete_payload"] = payload
    return by_id


def cogos_handoff_list_open(
    for_session: str | None = None,
    include_claimed: bool = False,
) -> dict[str, Any]:
    """List handoff offers that are currently available to claim.

    Reads ``bus_handoffs`` (up to 500 events) and groups by ``handoff_id`` to
    derive state. By default returns offers in state ``open`` (no claim yet).
    Set ``include_claimed=True`` to also see handoffs that have been claimed
    but not yet completed — useful for observing in-flight work.

    CALL THIS WHEN a fresh session is starting and wants to know if there's
    inherited work to pick up, when a manager wants a roster of pending
    handoffs, or when triaging why a handoff chain has stalled.

    Arguments:
      for_session:     if set, only include offers whose ``to_session``
                       matches this id OR whose ``to_session`` is null (open).
                       Leave None to see every open offer.
      include_claimed: include claimed-but-not-complete handoffs alongside
                       open offers (default False — open only).

    Contract: returns ``{"handoffs": [...], "count": N}``. Each entry has
    ``{handoff_id, from_session, to_session, reason, created_at, ttl_seconds,
    state, task_title}``. Structured error from ``cogos_events_read`` on
    failure.
    """
    read = cogos_events_read(bus_id=BUS_HANDOFFS, limit=500)
    if read.get("success") is False:
        return read
    events = read.get("events") or []
    grouped = _aggregate_handoffs(events)

    wanted_states = {"open"}
    if include_claimed:
        wanted_states.add("claimed")

    out: list[dict[str, Any]] = []
    for hid, entry in grouped.items():
        if entry["state"] not in wanted_states:
            continue
        if for_session is not None:
            to_sess = entry.get("to_session")
            if to_sess is not None and to_sess != for_session:
                continue
        out.append(
            {
                "handoff_id": hid,
                "from_session": entry.get("from_session"),
                "to_session": entry.get("to_session"),
                "reason": entry.get("reason"),
                "created_at": entry.get("created_at"),
                "ttl_seconds": entry.get("ttl_seconds"),
                "state": entry.get("state"),
                "task_title": entry.get("task_title"),
            }
        )

    return {"handoffs": out, "count": len(out)}


def cogos_handoff_claim(handoff_id: str, claiming_session: str) -> dict[str, Any]:
    """Claim an open handoff offer and retrieve its full payload.

    Emits a ``handoff.claim`` event on ``bus_handoffs`` then returns the
    corresponding offer's full payload (fetched via ``cogos_events_read``) so
    the claiming session can immediately read ``bootstrap_prompt``, ``task``,
    ``bus_context_refs``, and ``memory_refs`` without an extra round-trip.

    Claim is first-wins-by-seq: the lowest-seq claim for a given
    ``handoff_id`` is the valid claimant. Other would-be claimants should
    detect the earlier claim via ``cogos_handoff_list_open(include_claimed=
    True)`` before starting work.

    CALL THIS WHEN a fresh session is picking up a handoff identified via
    ``cogos_handoff_list_open``. Emit ``cogos_session_register`` for yourself
    first so you're visible on the roster, THEN claim.

    Arguments:
      handoff_id:       the offer's id.
      claiming_session: this session's identifier (must be registered).

    Contract: on success returns ``{"handoff_id", "claim_emitted": <emit
    result>, "offer": <full offer payload>}``. If the offer cannot be found in
    the bus, returns ``{"success": False, "error": "...", "handoff_id":
    ...}`` WITHOUT emitting the claim (avoid polluting the bus with claims
    against phantom offers).
    """
    read = cogos_events_read(bus_id=BUS_HANDOFFS, limit=500)
    if read.get("success") is False:
        return {
            "success": False,
            "error": f"could not read bus_handoffs: {read.get('error')}",
            "handoff_id": handoff_id,
        }
    events = read.get("events") or []
    offer_payload: dict[str, Any] | None = None
    for ev in events:
        if ev.get("type") != "handoff.offer":
            continue
        payload = _parse_payload(ev)
        if payload.get("handoff_id") == handoff_id:
            offer_payload = payload
            break
    if offer_payload is None:
        return {
            "success": False,
            "error": f"no handoff.offer found for handoff_id={handoff_id}",
            "handoff_id": handoff_id,
        }

    claim_payload = {
        "handoff_id": handoff_id,
        "claiming_session": claiming_session,
        "previous_session": offer_payload.get("from_session"),
        "claimed_at": _utc_now_iso(),
    }
    claim_emitted = cogos_emit(
        bus_id=BUS_HANDOFFS,
        message=json.dumps(claim_payload),
        from_sender=claiming_session,
        event_type="handoff.claim",
    )
    return {
        "handoff_id": handoff_id,
        "claim_emitted": claim_emitted,
        "offer": offer_payload,
    }


def cogos_handoff_complete(
    handoff_id: str,
    completing_session: str,
    outcome: str = "done",
    notes: str | None = None,
    next_handoff_id: str | None = None,
) -> dict[str, Any]:
    """Mark a handoff as finished.

    Emits ``handoff.complete`` on ``bus_handoffs``. Closes the offer → claim
    → complete chain. If the work has been re-offered to yet another session,
    pass ``outcome="reoffered"`` and ``next_handoff_id`` to link the chain.

    CALL THIS WHEN the session that claimed the handoff has finished the work
    (``outcome="done"``), decided the task cannot be completed
    (``"abandoned"``), or itself handed off (``"reoffered"``).

    Arguments:
      handoff_id:         the handoff this completes.
      completing_session: the session emitting completion (should be the
                          claimant, but not enforced).
      outcome:            one of ``"done" | "reoffered" | "abandoned"``.
      notes:              short free-form summary for observers. Optional.
      next_handoff_id:    when ``outcome="reoffered"``, the new offer's id.

    Contract: returns whatever ``cogos_emit`` returns.
    """
    payload: dict[str, Any] = {
        "handoff_id": handoff_id,
        "completing_session": completing_session,
        "outcome": outcome,
        "completed_at": _utc_now_iso(),
    }
    if notes is not None:
        payload["notes"] = notes
    if next_handoff_id is not None:
        payload["next_handoff_id"] = next_handoff_id
    return cogos_emit(
        bus_id=BUS_HANDOFFS,
        message=json.dumps(payload),
        from_sender=completing_session,
        event_type="handoff.complete",
    )


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
    mcp.tool(
        title="Register Cog OS session presence",
        annotations=ToolAnnotations(
            readOnlyHint=False, idempotentHint=False, openWorldHint=True
        ),
    )(cogos_session_register)
    mcp.tool(
        title="Emit Cog OS session heartbeat",
        annotations=ToolAnnotations(
            readOnlyHint=False, idempotentHint=False, openWorldHint=True
        ),
    )(cogos_session_heartbeat)
    mcp.tool(
        title="End Cog OS session",
        annotations=ToolAnnotations(
            readOnlyHint=False, idempotentHint=False, openWorldHint=True
        ),
    )(cogos_session_end)
    mcp.tool(
        title="List active Cog OS sessions",
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=True
        ),
    )(cogos_sessions_list)
    mcp.tool(
        title="Offer Cog OS handoff",
        annotations=ToolAnnotations(
            readOnlyHint=False, idempotentHint=False, openWorldHint=True
        ),
    )(cogos_handoff_offer)
    mcp.tool(
        title="List open Cog OS handoffs",
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=True
        ),
    )(cogos_handoff_list_open)
    mcp.tool(
        title="Claim Cog OS handoff",
        annotations=ToolAnnotations(
            readOnlyHint=False, idempotentHint=False, openWorldHint=True
        ),
    )(cogos_handoff_claim)
    mcp.tool(
        title="Complete Cog OS handoff",
        annotations=ToolAnnotations(
            readOnlyHint=False, idempotentHint=False, openWorldHint=True
        ),
    )(cogos_handoff_complete)
