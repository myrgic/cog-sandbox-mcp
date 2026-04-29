"""Tests for the session-lifecycle and handoff bridge tools (v0.2+).

v0.2 migrated the tools from racy bridge-direct bus emits to kernel-native
routes under ``/v1/sessions/*`` and ``/v1/handoffs/*``. These tests mock the
low-level HTTP primitives so the wire shape the tools produce matches
HANDOFF_PROTOCOL v0.2 exactly. Live-kernel roundtrip coverage lives in
the Go integration tests under internal/engine/sessions_test.go; here we
focus on client-side payload fidelity and error passthrough.
"""

from __future__ import annotations

import urllib.error
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _enable_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bridge tools require COG_OS_BASE_URL to be set before the HTTP path
    runs. We never actually hit the network in these tests — the low-level
    primitives are patched — but the env var must be set so the tools don't
    short-circuit on missing config.
    """
    monkeypatch.setenv("COG_OS_BASE_URL", "http://127.0.0.1:5100")


# ---------- helpers ----------


def _stub_post(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture every POST to a kernel route; return the capture list.

    Each captured call is ``{"path": ..., "payload": ...}``. The fake
    returns a canonical kernel-success body the tool can pass through.
    """
    from cog_sandbox_mcp.tools import cogos_bridge

    calls: list[dict[str, Any]] = []

    def fake_post(
        path: str, payload: dict[str, Any], timeout_s: float = 30.0
    ) -> dict[str, Any]:
        calls.append({"path": path, "payload": payload})
        body = {"ok": True, "seq": len(calls), "hash": f"hash{len(calls)}"}
        # The offer route echoes handoff_id; mimic that so the wrap code is
        # exercised the same way the kernel would.
        if path == "/v1/handoffs/offer":
            body["handoff_id"] = "ho-stubbed-12345"
        if path.endswith("/claim"):
            body["handoff_id"] = path.split("/")[3]
            body["offer"] = {"bootstrap_prompt": "bp"}
            body["handoff"] = {"state": "claimed"}
        return body

    monkeypatch.setattr(cogos_bridge, "_http_post_json", fake_post)
    return calls


def _stub_get(
    monkeypatch: pytest.MonkeyPatch, result: Any
) -> list[dict[str, Any]]:
    """Capture every GET to a kernel route; fake returns ``result`` verbatim."""
    from cog_sandbox_mcp.tools import cogos_bridge

    calls: list[dict[str, Any]] = []

    def fake_get(
        path: str, params: dict[str, Any] | None = None, timeout_s: float = 10.0
    ) -> Any:
        calls.append({"path": path, "params": params})
        return result

    monkeypatch.setattr(cogos_bridge, "_http_get_any_with_params", fake_get)
    return calls


def _stub_post_http_error(
    monkeypatch: pytest.MonkeyPatch, status: int, body: str
) -> None:
    """Make every POST raise an HTTPError so we can test the never-raise
    envelope preserves the right bus_id."""
    from cog_sandbox_mcp.tools import cogos_bridge

    def fake_post(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise urllib.error.HTTPError(
            url="http://x", code=status, msg="test", hdrs=None, fp=None
        )

    # Replace fp so .read() on the error works.
    import io
    real_error = urllib.error.HTTPError(
        url="http://x", code=status, msg="test", hdrs=None, fp=io.BytesIO(body.encode())
    )

    def fake_post2(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise real_error

    monkeypatch.setattr(cogos_bridge, "_http_post_json", fake_post2)


# ---------- session.register ----------


def test_session_register_calls_kernel_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_post(monkeypatch)
    result = cogos_bridge.cogos_session_register(
        session_id="slowbro-laptop-cog-manager",
        workspace="/Users/slowbro/workspaces/cog",
        role="manager",
        task="coordinating wave 2",
        model="claude-opus-4-7",
        hostname="slowbro-laptop",
    )

    assert result == {"ok": True, "seq": 1, "hash": "hash1"}
    assert len(calls) == 1
    c = calls[0]
    assert c["path"] == "/v1/sessions/register"
    p = c["payload"]
    assert p["session_id"] == "slowbro-laptop-cog-manager"
    assert p["workspace"] == "/Users/slowbro/workspaces/cog"
    assert p["role"] == "manager"
    assert p["task"] == "coordinating wave 2"
    assert p["model"] == "claude-opus-4-7"
    assert p["hostname"] == "slowbro-laptop"


def test_session_register_omits_optional_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_post(monkeypatch)
    cogos_bridge.cogos_session_register(
        session_id="host-ws-role", workspace="/a", role="r", task="t"
    )
    p = calls[0]["payload"]
    assert "model" not in p
    assert "hostname" not in p


def test_session_register_never_raises_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    _stub_post_http_error(monkeypatch, 400, '{"error":"bad"}')
    result = cogos_bridge.cogos_session_register(
        session_id="host-ws-role", workspace="/a", role="r", task="t"
    )
    assert result["success"] is False
    assert "400" in result["error"]
    assert result["bus_id"] == "bus_sessions"


def test_session_register_default_participant_type_is_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Back-compat invariant: when participant_type is left at the "agent"
    default, it must NOT appear on the wire. The kernel (and any downstream
    reading bus_sessions directly) should see the exact byte-for-byte payload
    existing agent callers have always sent."""
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_post(monkeypatch)
    cogos_bridge.cogos_session_register(
        session_id="host-ws-role", workspace="/a", role="r", task="t"
    )
    p = calls[0]["payload"]
    assert "participant_type" not in p
    assert "metadata" not in p


def test_session_register_explicit_agent_also_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit participant_type="agent" is treated the same as the default:
    omitted from the wire so the kernel's registered payload is identical
    across "default agent" and "explicit agent" callers."""
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_post(monkeypatch)
    cogos_bridge.cogos_session_register(
        session_id="host-ws-role",
        workspace="/a",
        role="r",
        task="t",
        participant_type="agent",
    )
    assert "participant_type" not in calls[0]["payload"]


def test_session_register_provider_includes_participant_type_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Channel-provider RFC shape: participant_type="provider" plus metadata
    carrying provider_id and kinds travel on the wire so the kernel's session
    registry can distinguish providers from agents."""
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_post(monkeypatch)
    result = cogos_bridge.cogos_session_register(
        session_id="slowbro-laptop-mod3-provider",
        workspace="/Users/slowbro/workspaces/cogos-dev/mod3",
        role="audio-provider",
        task="mediating voice-room-primary",
        participant_type="provider",
        metadata={"provider_id": "mod3", "kinds": ["audio"]},
    )
    assert result["ok"] is True
    p = calls[0]["payload"]
    assert p["participant_type"] == "provider"
    assert p["metadata"] == {"provider_id": "mod3", "kinds": ["audio"]}
    assert p["session_id"] == "slowbro-laptop-mod3-provider"


def test_session_register_user_participant_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Human-driven sessions can declare participant_type="user" and it travels
    on the wire (only the "agent" default is elided)."""
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_post(monkeypatch)
    cogos_bridge.cogos_session_register(
        session_id="host-ws-chaz",
        workspace="/a",
        role="operator",
        task="live REPL",
        participant_type="user",
    )
    assert calls[0]["payload"]["participant_type"] == "user"


def test_session_register_rejects_unknown_participant_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Client-side validation short-circuits with the never-raise envelope on
    an obvious client bug (typo, unsupported value) — no kernel round-trip."""
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_post(monkeypatch)
    r = cogos_bridge.cogos_session_register(
        session_id="host-ws-role",
        workspace="/a",
        role="r",
        task="t",
        participant_type="robot",
    )
    assert r["success"] is False
    assert "participant_type" in r["error"]
    assert "robot" in r["error"]
    assert r["bus_id"] == "bus_sessions"
    # No HTTP call should have been made on a client-side validation failure.
    assert calls == []


def test_session_register_empty_metadata_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty metadata dict (or None) is not serialized — keep the payload
    minimal when there's nothing to say."""
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_post(monkeypatch)
    cogos_bridge.cogos_session_register(
        session_id="host-ws-role",
        workspace="/a",
        role="r",
        task="t",
        participant_type="provider",
        metadata={},
    )
    p = calls[0]["payload"]
    assert "metadata" not in p
    assert p["participant_type"] == "provider"


# ---------- session.heartbeat ----------


def test_session_heartbeat_calls_kernel_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_post(monkeypatch)
    cogos_bridge.cogos_session_heartbeat(
        session_id="host-ws-role",
        status="active",
        context_usage=0.42,
        current_task="writing tests",
    )
    c = calls[0]
    assert c["path"] == "/v1/sessions/host-ws-role/heartbeat"
    assert c["payload"] == {
        "status": "active",
        "context_usage": 0.42,
        "current_task": "writing tests",
    }


def test_session_heartbeat_omits_absent_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_post(monkeypatch)
    cogos_bridge.cogos_session_heartbeat(session_id="host-ws-role")
    p = calls[0]["payload"]
    assert p == {"status": "active"}


# ---------- session.end ----------


def test_session_end_calls_kernel_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_post(monkeypatch)
    cogos_bridge.cogos_session_end(
        session_id="host-ws-role", reason="task-complete", handoff_id="ho-123"
    )
    c = calls[0]
    assert c["path"] == "/v1/sessions/host-ws-role/end"
    assert c["payload"] == {"reason": "task-complete", "handoff_id": "ho-123"}


# ---------- sessions_list (presence) ----------


def test_sessions_list_delegates_to_presence_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    fake_body = {
        "sessions": [
            {
                "session_id": "a-b-c",
                "workspace": "/x",
                "role": "r",
                "active": True,
            },
        ],
        "count": 1,
    }
    calls = _stub_get(monkeypatch, fake_body)
    result = cogos_bridge.cogos_sessions_list(active_within_seconds=300)

    assert result == fake_body
    c = calls[0]
    assert c["path"] == "/v1/sessions/presence"
    assert c["params"]["active_within_seconds"] == 300


def test_sessions_list_include_ended_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_get(monkeypatch, {"sessions": [], "count": 0})
    cogos_bridge.cogos_sessions_list(include_ended=True)
    assert calls[0]["params"]["include_ended"] == "true"


# ---------- handoff.offer ----------


def test_handoff_offer_validates_task(monkeypatch: pytest.MonkeyPatch) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    _stub_post(monkeypatch)  # shouldn't even be called

    r = cogos_bridge.cogos_handoff_offer(
        from_session="a-b-c",
        task={"title": "", "goal": "g", "next_steps": ["s"]},
        bootstrap_prompt="bp",
    )
    assert r["success"] is False
    assert "title" in r["error"]

    r = cogos_bridge.cogos_handoff_offer(
        from_session="a-b-c",
        task={"title": "t", "goal": "", "next_steps": ["s"]},
        bootstrap_prompt="bp",
    )
    assert r["success"] is False
    assert "goal" in r["error"]

    r = cogos_bridge.cogos_handoff_offer(
        from_session="a-b-c",
        task={"title": "t", "goal": "g", "next_steps": []},
        bootstrap_prompt="bp",
    )
    assert r["success"] is False
    assert "next_steps" in r["error"]


def test_handoff_offer_calls_kernel_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_post(monkeypatch)
    r = cogos_bridge.cogos_handoff_offer(
        from_session="a-b-c",
        task={"title": "T", "goal": "G", "next_steps": ["s1"]},
        bootstrap_prompt="bp-text",
        to_session="x-y-z",
        reason="context-exhaustion",
        ttl_seconds=1800,
        bus_context_refs=[{"bus_id": "bus_chat", "after_seq": 42}],
        memory_refs=["cog://mem/a.cog.md"],
    )
    # v0.1-shaped back-compat: {handoff_id, emit_result}
    assert r["handoff_id"] == "ho-stubbed-12345"
    assert r["emit_result"]["ok"] is True
    assert r["emit_result"]["hash"]

    c = calls[0]
    assert c["path"] == "/v1/handoffs/offer"
    p = c["payload"]
    assert p["from_session"] == "a-b-c"
    assert p["to_session"] == "x-y-z"
    assert p["reason"] == "context-exhaustion"
    assert p["ttl_seconds"] == 1800
    assert p["task"]["title"] == "T"
    assert p["bootstrap_prompt"] == "bp-text"
    assert p["bus_context_refs"] == [{"bus_id": "bus_chat", "after_seq": 42}]
    assert p["memory_refs"] == ["cog://mem/a.cog.md"]


# ---------- handoff.list_open ----------


def test_handoff_list_open_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    fake_body = {
        "handoffs": [
            {
                "handoff_id": "ho-1",
                "from_session": "a-b-c",
                "to_session": None,
                "reason": "explicit",
                "created_at": "2026-04-22T10:00:00Z",
                "ttl_seconds": 3600,
                "state": "open",
                "offer": {"task": {"title": "Investigate"}},
            },
        ],
        "count": 1,
    }
    calls = _stub_get(monkeypatch, fake_body)

    r = cogos_bridge.cogos_handoff_list_open()
    assert calls[0]["path"] == "/v1/handoffs"
    assert calls[0]["params"].get("state") == "open"
    assert r["count"] == 1
    entry = r["handoffs"][0]
    assert entry["handoff_id"] == "ho-1"
    assert entry["task_title"] == "Investigate"  # reshaped from offer.task.title


def test_handoff_list_open_for_session_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_get(monkeypatch, {"handoffs": [], "count": 0})
    cogos_bridge.cogos_handoff_list_open(for_session="x-y-z")
    assert calls[0]["params"]["for_session"] == "x-y-z"


# ---------- handoff.claim ----------


def test_handoff_claim_calls_kernel_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_post(monkeypatch)
    r = cogos_bridge.cogos_handoff_claim(
        handoff_id="ho-42", claiming_session="dst-session-abc"
    )
    # v0.1-shaped back-compat
    assert r["handoff_id"] == "ho-42"
    assert r["claim_emitted"]["ok"] is True
    assert r["offer"]["bootstrap_prompt"] == "bp"
    c = calls[0]
    assert c["path"] == "/v1/handoffs/ho-42/claim"
    assert c["payload"] == {"claiming_session": "dst-session-abc"}


def test_handoff_claim_propagates_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kernel returns 409 when the offer is already claimed. The tool must
    surface that as the never-raise ``{"success": False, ...}`` envelope so
    clients can detect racy claims without a try/except."""
    from cog_sandbox_mcp.tools import cogos_bridge

    _stub_post_http_error(monkeypatch, 409, '{"error":"already_claimed"}')
    r = cogos_bridge.cogos_handoff_claim(
        handoff_id="ho-42", claiming_session="loser-session-abc"
    )
    assert r["success"] is False
    assert "409" in r["error"]
    assert r["bus_id"] == "bus_handoffs"
    assert r["handoff_id"] == "ho-42"


# ---------- handoff.complete ----------


def test_handoff_complete_calls_kernel_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_post(monkeypatch)
    cogos_bridge.cogos_handoff_complete(
        handoff_id="ho-42",
        completing_session="dst-session-abc",
        outcome="done",
        notes="wrapped up cleanly",
        next_handoff_id="ho-43",
    )
    c = calls[0]
    assert c["path"] == "/v1/handoffs/ho-42/complete"
    assert c["payload"] == {
        "completing_session": "dst-session-abc",
        "outcome": "done",
        "notes": "wrapped up cleanly",
        "next_handoff_id": "ho-43",
    }


def test_handoff_complete_omits_optionals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    calls = _stub_post(monkeypatch)
    cogos_bridge.cogos_handoff_complete(
        handoff_id="ho-42", completing_session="dst-session-abc"
    )
    p = calls[0]["payload"]
    assert p == {"completing_session": "dst-session-abc", "outcome": "done"}
