"""Tests for the session-lifecycle and handoff bridge tools.

Payload-shape tests: mock the low-level HTTP primitives (`_http_post_json` and
`_http_get_any_with_params`) to verify the wire shape the tools produce matches
docs/HANDOFF_PROTOCOL.md. Live-kernel tests are deliberately absent — the
roundtrip contract is already covered by the threaded-server emit→read tests
in test_tools.py; here we focus on payload fidelity.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _enable_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bridge tools all require COG_OS_BASE_URL to be set to exercise the HTTP
    path. The tools themselves never contact the network in these tests — we
    intercept `_http_post_json` / `_http_get_any_with_params` — but the env
    var must be set so the tools don't short-circuit on missing config.
    """
    monkeypatch.setenv("COG_OS_BASE_URL", "http://127.0.0.1:5100")


# ---------- helpers ----------


def _stub_emit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture every POST that would hit /v1/bus/send; return the capture list.

    Patches _http_post_json so cogos_emit sees a successful kernel response and
    records exactly what would have been wired.
    """
    from cog_sandbox_mcp.tools import cogos_bridge

    calls: list[dict[str, Any]] = []

    def fake_post(path: str, payload: dict[str, Any], timeout_s: float = 30.0) -> dict[str, Any]:
        calls.append({"path": path, "payload": payload})
        return {"ok": True, "seq": len(calls)}

    monkeypatch.setattr(cogos_bridge, "_http_post_json", fake_post)
    return calls


def _stub_get(
    monkeypatch: pytest.MonkeyPatch, events_by_bus: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Capture every GET and return canned events per bus path."""
    from cog_sandbox_mcp.tools import cogos_bridge

    calls: list[dict[str, Any]] = []

    def fake_get(path: str, params: dict[str, Any] | None = None, timeout_s: float = 10.0) -> Any:
        calls.append({"path": path, "params": params})
        # Path is /v1/bus/<bus_id>/events
        parts = path.split("/")
        if len(parts) >= 4 and parts[1] == "v1" and parts[2] == "bus":
            return events_by_bus.get(parts[3], [])
        return None

    monkeypatch.setattr(cogos_bridge, "_http_get_any_with_params", fake_get)
    return calls


def _make_event(
    seq: int, event_type: str, from_sender: str, payload_dict: dict[str, Any]
) -> dict[str, Any]:
    """Shape an event the way the kernel returns it: payload.content is a
    JSON-encoded string (since cogos_emit puts JSON in the message field)."""
    return {
        "seq": seq,
        "type": event_type,
        "from": from_sender,
        "payload": {"content": json.dumps(payload_dict)},
    }


# ---------- session.register ----------


def test_session_register_emits_correct_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_emit(monkeypatch)
    result = cogos_bridge.cogos_session_register(
        session_id="slowbro-laptop-cog-manager",
        workspace="/Users/slowbro/workspaces/cog",
        role="manager",
        task="coordinating wave 2",
        model="claude-opus-4-7",
        hostname="slowbro-laptop",
    )

    assert result == {"ok": True, "seq": 1}
    assert len(calls) == 1
    c = calls[0]
    assert c["path"] == "/v1/bus/send"
    assert c["payload"]["bus_id"] == "bus_sessions"
    assert c["payload"]["type"] == "session.register"
    assert c["payload"]["from"] == "slowbro-laptop-cog-manager"

    payload = json.loads(c["payload"]["message"])
    assert payload["session_id"] == "slowbro-laptop-cog-manager"
    assert payload["workspace"] == "/Users/slowbro/workspaces/cog"
    assert payload["role"] == "manager"
    assert payload["task"] == "coordinating wave 2"
    assert payload["model"] == "claude-opus-4-7"
    assert payload["hostname"] == "slowbro-laptop"
    # started_at must be a parseable RFC3339-ish UTC timestamp.
    parsed = datetime.fromisoformat(payload["started_at"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_session_register_omits_optional_fields_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_emit(monkeypatch)
    cogos_bridge.cogos_session_register(
        session_id="s-1",
        workspace="/tmp/ws",
        role="worker",
        task="do thing",
    )
    payload = json.loads(calls[0]["payload"]["message"])
    assert "model" not in payload
    assert "hostname" not in payload


# ---------- session.heartbeat ----------


def test_session_heartbeat_default_status(monkeypatch: pytest.MonkeyPatch) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_emit(monkeypatch)
    cogos_bridge.cogos_session_heartbeat(session_id="s-1")
    c = calls[0]
    assert c["payload"]["type"] == "session.heartbeat"
    payload = json.loads(c["payload"]["message"])
    assert payload["status"] == "active"
    assert payload["session_id"] == "s-1"
    assert "last_tool_use_at" in payload


def test_session_heartbeat_all_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_emit(monkeypatch)
    cogos_bridge.cogos_session_heartbeat(
        session_id="s-2",
        status="ending",
        context_usage=0.93,
        current_task="winding down",
    )
    payload = json.loads(calls[0]["payload"]["message"])
    assert payload["status"] == "ending"
    assert payload["context_usage"] == 0.93
    assert payload["current_task"] == "winding down"


def test_session_heartbeat_passes_unknown_status(monkeypatch: pytest.MonkeyPatch) -> None:
    # No client-side validation — the tool just passes through.
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_emit(monkeypatch)
    cogos_bridge.cogos_session_heartbeat(session_id="s-3", status="mystery-state")
    payload = json.loads(calls[0]["payload"]["message"])
    assert payload["status"] == "mystery-state"


# ---------- session.end ----------


def test_session_end_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_emit(monkeypatch)
    cogos_bridge.cogos_session_end(
        session_id="s-1", reason="handed-off", handoff_id="ho-abc"
    )
    c = calls[0]
    assert c["payload"]["type"] == "session.end"
    payload = json.loads(c["payload"]["message"])
    assert payload["reason"] == "handed-off"
    assert payload["handoff_id"] == "ho-abc"
    assert "ended_at" in payload


def test_session_end_default_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_emit(monkeypatch)
    cogos_bridge.cogos_session_end(session_id="s-1")
    payload = json.loads(calls[0]["payload"]["message"])
    assert payload["reason"] == "user-quit"
    assert "handoff_id" not in payload


# ---------- sessions_list aggregation ----------


def test_sessions_list_aggregates_latest_status(monkeypatch: pytest.MonkeyPatch) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    # Recent (within default 600s freshness window) — use "now" for timestamps.
    now = datetime.now(timezone.utc).isoformat()
    events = [
        _make_event(
            1,
            "session.register",
            "s-1",
            {
                "session_id": "s-1",
                "workspace": "/ws",
                "role": "manager",
                "task": "orig task",
                "started_at": now,
            },
        ),
        _make_event(
            2,
            "session.heartbeat",
            "s-1",
            {
                "session_id": "s-1",
                "status": "active",
                "context_usage": 0.42,
                "last_tool_use_at": now,
            },
        ),
        _make_event(
            3,
            "session.register",
            "s-2",
            {
                "session_id": "s-2",
                "workspace": "/other",
                "role": "worker",
                "task": "side task",
                "started_at": now,
            },
        ),
        _make_event(
            4,
            "session.end",
            "s-2",
            {"session_id": "s-2", "ended_at": now, "reason": "task-complete"},
        ),
    ]
    _stub_get(monkeypatch, {"bus_sessions": events})
    result = cogos_bridge.cogos_sessions_list()

    assert result["count"] == 2
    by_id = {s["session_id"]: s for s in result["sessions"]}
    assert by_id["s-1"]["active"] is True
    assert by_id["s-1"]["status"] == "active"
    assert by_id["s-1"]["context_usage"] == 0.42
    assert by_id["s-1"]["last_event_type"] == "session.heartbeat"
    assert by_id["s-2"]["active"] is False
    assert by_id["s-2"]["status"] == "ended"


def test_sessions_list_flags_stale_as_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    old_ts = "2020-01-01T00:00:00+00:00"
    events = [
        _make_event(
            1,
            "session.register",
            "s-old",
            {
                "session_id": "s-old",
                "workspace": "/ws",
                "role": "worker",
                "task": "ancient",
                "started_at": old_ts,
            },
        ),
    ]
    _stub_get(monkeypatch, {"bus_sessions": events})
    result = cogos_bridge.cogos_sessions_list(active_within_seconds=600)
    assert result["count"] == 1
    assert result["sessions"][0]["active"] is False


def test_sessions_list_propagates_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    def fake_get(*_args: Any, **_kwargs: Any) -> Any:
        raise cogos_bridge.urllib.error.URLError("boom")

    monkeypatch.setattr(cogos_bridge, "_http_get_any_with_params", fake_get)
    result = cogos_bridge.cogos_sessions_list()
    assert result["success"] is False
    assert "error" in result


# ---------- handoff.offer ----------


def _valid_task() -> dict[str, Any]:
    return {
        "title": "Refactor context engine",
        "goal": "Extract inference-independent context construction.",
        "progress_summary": "Wave 1 complete.",
        "files_touched": ["a.go"],
        "files_pending": ["b.go"],
        "decisions_made": [{"decision": "use foo", "rationale": "bar"}],
        "open_questions": ["q1?"],
        "next_steps": ["1. run go build", "2. run tests"],
        "verification_gates": ["go build ./..."],
    }


def test_handoff_offer_emits_full_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_emit(monkeypatch)
    task = _valid_task()
    result = cogos_bridge.cogos_handoff_offer(
        from_session="s-a",
        task=task,
        bootstrap_prompt="You are picking up from Session A...",
        to_session=None,
        reason="context-exhaustion",
        ttl_seconds=7200,
        bus_context_refs=[{"bus_id": "bus_chat_s-a", "after_seq": 100}],
        memory_refs=["cog://mem/working/state.md"],
    )

    assert result["handoff_id"].startswith("ho-")
    assert result["emit_result"] == {"ok": True, "seq": 1}
    assert len(calls) == 1
    c = calls[0]
    assert c["payload"]["bus_id"] == "bus_handoffs"
    assert c["payload"]["type"] == "handoff.offer"
    assert c["payload"]["from"] == "s-a"

    payload = json.loads(c["payload"]["message"])
    assert payload["handoff_id"] == result["handoff_id"]
    assert payload["from_session"] == "s-a"
    assert payload["to_session"] is None
    assert payload["reason"] == "context-exhaustion"
    assert payload["ttl_seconds"] == 7200
    assert payload["task"] == task
    assert payload["bootstrap_prompt"] == "You are picking up from Session A..."
    assert payload["bus_context_refs"] == [{"bus_id": "bus_chat_s-a", "after_seq": 100}]
    assert payload["memory_refs"] == ["cog://mem/working/state.md"]
    # Timestamp is RFC3339-ish parseable.
    datetime.fromisoformat(payload["created_at"].replace("Z", "+00:00"))


def test_handoff_offer_defaults_empty_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_emit(monkeypatch)
    cogos_bridge.cogos_handoff_offer(
        from_session="s-a",
        task=_valid_task(),
        bootstrap_prompt="go",
    )
    payload = json.loads(calls[0]["payload"]["message"])
    assert payload["bus_context_refs"] == []
    assert payload["memory_refs"] == []
    assert payload["ttl_seconds"] == 3600
    assert payload["reason"] == "explicit"


def test_handoff_offer_rejects_missing_title(monkeypatch: pytest.MonkeyPatch) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_emit(monkeypatch)
    task = _valid_task()
    task["title"] = ""
    result = cogos_bridge.cogos_handoff_offer(
        from_session="s-a", task=task, bootstrap_prompt="go"
    )
    assert result["success"] is False
    assert "title" in result["error"]
    assert calls == []  # no HTTP call on validation failure


def test_handoff_offer_rejects_empty_next_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_emit(monkeypatch)
    task = _valid_task()
    task["next_steps"] = []
    result = cogos_bridge.cogos_handoff_offer(
        from_session="s-a", task=task, bootstrap_prompt="go"
    )
    assert result["success"] is False
    assert "next_steps" in result["error"]
    assert calls == []


def test_handoff_offer_rejects_non_dict_task(monkeypatch: pytest.MonkeyPatch) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_emit(monkeypatch)
    result = cogos_bridge.cogos_handoff_offer(
        from_session="s-a", task="not a dict", bootstrap_prompt="go"  # type: ignore[arg-type]
    )
    assert result["success"] is False
    assert calls == []


# ---------- handoff_list_open ----------


def test_handoff_list_open_returns_open_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    events = [
        _make_event(
            1,
            "handoff.offer",
            "s-a",
            {
                "handoff_id": "ho-1",
                "from_session": "s-a",
                "to_session": None,
                "reason": "explicit",
                "created_at": "2026-04-21T10:00:00+00:00",
                "ttl_seconds": 3600,
                "task": {"title": "Task 1"},
            },
        ),
        _make_event(
            2,
            "handoff.offer",
            "s-a",
            {
                "handoff_id": "ho-2",
                "from_session": "s-a",
                "to_session": None,
                "reason": "explicit",
                "created_at": "2026-04-21T10:05:00+00:00",
                "ttl_seconds": 3600,
                "task": {"title": "Task 2"},
            },
        ),
        _make_event(
            3,
            "handoff.claim",
            "s-b",
            {"handoff_id": "ho-2", "claiming_session": "s-b"},
        ),
    ]
    _stub_get(monkeypatch, {"bus_handoffs": events})
    result = cogos_bridge.cogos_handoff_list_open()
    assert result["count"] == 1
    assert result["handoffs"][0]["handoff_id"] == "ho-1"
    assert result["handoffs"][0]["state"] == "open"
    assert result["handoffs"][0]["task_title"] == "Task 1"


def test_handoff_list_open_include_claimed(monkeypatch: pytest.MonkeyPatch) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    events = [
        _make_event(
            1,
            "handoff.offer",
            "s-a",
            {
                "handoff_id": "ho-1",
                "from_session": "s-a",
                "to_session": None,
                "reason": "explicit",
                "created_at": "2026-04-21T10:00:00+00:00",
                "ttl_seconds": 3600,
                "task": {"title": "Task 1"},
            },
        ),
        _make_event(
            2,
            "handoff.claim",
            "s-b",
            {"handoff_id": "ho-1", "claiming_session": "s-b"},
        ),
        _make_event(
            3,
            "handoff.complete",
            "s-b",
            {"handoff_id": "ho-1", "completing_session": "s-b", "outcome": "done"},
        ),
    ]
    _stub_get(monkeypatch, {"bus_handoffs": events})
    result = cogos_bridge.cogos_handoff_list_open(include_claimed=True)
    # Completed handoff should not appear even with include_claimed.
    assert result["count"] == 0


def test_handoff_list_open_filters_by_session(monkeypatch: pytest.MonkeyPatch) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    events = [
        _make_event(
            1,
            "handoff.offer",
            "s-a",
            {
                "handoff_id": "ho-open",
                "from_session": "s-a",
                "to_session": None,
                "reason": "explicit",
                "created_at": "2026-04-21T10:00:00+00:00",
                "ttl_seconds": 3600,
                "task": {"title": "Open to any"},
            },
        ),
        _make_event(
            2,
            "handoff.offer",
            "s-a",
            {
                "handoff_id": "ho-targeted-b",
                "from_session": "s-a",
                "to_session": "s-b",
                "reason": "explicit",
                "created_at": "2026-04-21T10:01:00+00:00",
                "ttl_seconds": 3600,
                "task": {"title": "Targeted at s-b"},
            },
        ),
        _make_event(
            3,
            "handoff.offer",
            "s-a",
            {
                "handoff_id": "ho-targeted-c",
                "from_session": "s-a",
                "to_session": "s-c",
                "reason": "explicit",
                "created_at": "2026-04-21T10:02:00+00:00",
                "ttl_seconds": 3600,
                "task": {"title": "Targeted at s-c"},
            },
        ),
    ]
    _stub_get(monkeypatch, {"bus_handoffs": events})
    result = cogos_bridge.cogos_handoff_list_open(for_session="s-b")
    ids = {h["handoff_id"] for h in result["handoffs"]}
    # s-b should see open offer + targeted-at-s-b; NOT targeted-at-s-c.
    assert ids == {"ho-open", "ho-targeted-b"}


# ---------- handoff.claim ----------


def test_handoff_claim_returns_offer_and_emits(monkeypatch: pytest.MonkeyPatch) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    offer_payload = {
        "handoff_id": "ho-claim-test",
        "from_session": "s-a",
        "to_session": None,
        "reason": "explicit",
        "created_at": "2026-04-21T10:00:00+00:00",
        "ttl_seconds": 3600,
        "task": {"title": "Pick me up", "goal": "g", "next_steps": ["s"]},
        "bootstrap_prompt": "Resume this.",
        "bus_context_refs": [],
        "memory_refs": [],
    }
    events = [_make_event(1, "handoff.offer", "s-a", offer_payload)]
    _stub_get(monkeypatch, {"bus_handoffs": events})
    emits = _stub_emit(monkeypatch)

    result = cogos_bridge.cogos_handoff_claim(
        handoff_id="ho-claim-test", claiming_session="s-b"
    )

    assert result["handoff_id"] == "ho-claim-test"
    assert result["offer"] == offer_payload
    assert result["claim_emitted"] == {"ok": True, "seq": 1}
    assert len(emits) == 1
    c = emits[0]
    assert c["payload"]["type"] == "handoff.claim"
    assert c["payload"]["from"] == "s-b"
    claim_payload = json.loads(c["payload"]["message"])
    assert claim_payload["handoff_id"] == "ho-claim-test"
    assert claim_payload["claiming_session"] == "s-b"
    assert claim_payload["previous_session"] == "s-a"
    assert "claimed_at" in claim_payload


def test_handoff_claim_missing_offer_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    _stub_get(monkeypatch, {"bus_handoffs": []})
    emits = _stub_emit(monkeypatch)

    result = cogos_bridge.cogos_handoff_claim(
        handoff_id="does-not-exist", claiming_session="s-b"
    )
    assert result["success"] is False
    assert result["handoff_id"] == "does-not-exist"
    # No claim should have been emitted against a phantom offer.
    assert emits == []


# ---------- handoff.complete ----------


def test_handoff_complete_default_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_emit(monkeypatch)
    cogos_bridge.cogos_handoff_complete(
        handoff_id="ho-x", completing_session="s-b"
    )
    c = calls[0]
    assert c["payload"]["bus_id"] == "bus_handoffs"
    assert c["payload"]["type"] == "handoff.complete"
    payload = json.loads(c["payload"]["message"])
    assert payload["outcome"] == "done"
    assert payload["handoff_id"] == "ho-x"
    assert payload["completing_session"] == "s-b"
    assert "completed_at" in payload
    assert "notes" not in payload
    assert "next_handoff_id" not in payload


def test_handoff_complete_with_reoffer(monkeypatch: pytest.MonkeyPatch) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_emit(monkeypatch)
    cogos_bridge.cogos_handoff_complete(
        handoff_id="ho-x",
        completing_session="s-b",
        outcome="reoffered",
        notes="passed to worker",
        next_handoff_id="ho-y",
    )
    payload = json.loads(calls[0]["payload"]["message"])
    assert payload["outcome"] == "reoffered"
    assert payload["notes"] == "passed to worker"
    assert payload["next_handoff_id"] == "ho-y"


# ---------- registration ----------


def test_all_12_tools_registered_when_enabled(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirm the four original tools PLUS the eight new session/handoff tools
    all appear in the registered tool list when the bridge is enabled."""
    from cog_sandbox_mcp import sandbox
    from cog_sandbox_mcp.server import build_server
    import asyncio

    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("COG_SANDBOX_ROOT", str(tmp_path))
    monkeypatch.setenv("COG_SANDBOX_INITIAL_AUTH", "ws")
    monkeypatch.setenv("COG_OS_BASE_URL", "http://localhost:5100")
    sandbox.initialize_auth()
    tools = asyncio.run(build_server().list_tools())
    names = {t.name for t in tools}
    expected = {
        "cogos_status",
        "cogos_emit",
        "cogos_events_read",
        "cogos_resolve",
        "cogos_session_register",
        "cogos_session_heartbeat",
        "cogos_session_end",
        "cogos_sessions_list",
        "cogos_handoff_offer",
        "cogos_handoff_list_open",
        "cogos_handoff_claim",
        "cogos_handoff_complete",
    }
    missing = expected - names
    assert not missing, f"missing bridge tools: {missing}"
