"""Tests for ``cogos_channel_join`` / ``cogos_channel_leave``.

These are the channel-provider RFC's session-attendance primitives (see
cog://mem/semantic/designs/channel-provider-interface § Bus topics per
channel → ``channel.<id>.attendance``). A Claude Code session calls
``cogos_channel_join`` at SessionStart to register as an attendant on a
named channel; it calls ``cogos_channel_leave`` when winding down.

These tests mock the low-level HTTP primitives (both kernel HTTP and the
optional mod3 best-effort registration) so we can assert on wire shapes
without a running kernel or mod3. Live-kernel coverage lives elsewhere.
"""

from __future__ import annotations

import json
import urllib.error
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _enable_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bridge tools require COG_OS_BASE_URL to be set. We never actually hit
    the network in these tests — primitives are patched — but the env must
    be set so the tools don't short-circuit on missing config.
    """
    monkeypatch.setenv("COG_OS_BASE_URL", "http://127.0.0.1:5100")


# ---------- helpers ----------


def _stub_post_and_get(
    monkeypatch: pytest.MonkeyPatch,
    presence_sessions: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Stub both ``_http_post_json`` and ``_http_get_any_with_params``.

    - GET ``/v1/sessions/presence`` returns ``{"sessions": presence_sessions,
      "count": len(...)}`` so session-registered checks resolve cleanly.
    - POST ``/v1/bus/send`` returns a kernel-success body with a monotonic
      ``seq`` so the tool's return payload surfaces the seq field.

    Returns ``(post_calls, get_calls)`` where each entry is
    ``{"path", "payload"|"params"}``.
    """
    from cog_sandbox_mcp.tools import cogos_bridge

    post_calls: list[dict[str, Any]] = []
    get_calls: list[dict[str, Any]] = []

    def fake_post(
        path: str, payload: dict[str, Any], timeout_s: float = 30.0
    ) -> dict[str, Any]:
        post_calls.append({"path": path, "payload": payload})
        return {"ok": True, "seq": 42 + len(post_calls), "hash": f"hash{len(post_calls)}"}

    def fake_get(
        path: str, params: dict[str, Any] | None = None, timeout_s: float = 10.0
    ) -> Any:
        get_calls.append({"path": path, "params": params})
        rows = presence_sessions if presence_sessions is not None else []
        return {"sessions": rows, "count": len(rows)}

    monkeypatch.setattr(cogos_bridge, "_http_post_json", fake_post)
    monkeypatch.setattr(cogos_bridge, "_http_get_any_with_params", fake_get)
    return post_calls, get_calls


def _disable_mod3(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the mod3 helper so tests that don't care about mod3 don't
    make network calls. Returns the list of captured calls so individual
    tests can still assert on what the join tool tried to send to mod3.
    """
    from cog_sandbox_mcp.tools import cogos_bridge

    calls: list[dict[str, Any]] = []

    def fake_mod3(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"registered": False, "warning": "mod3 disabled in test"}

    monkeypatch.setattr(cogos_bridge, "_mod3_register_session", fake_mod3)
    return calls


# ---------- validation envelope ----------


def test_channel_join_rejects_unknown_participant_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    _disable_mod3(monkeypatch)
    _stub_post_and_get(monkeypatch, presence_sessions=[])
    r = cogos_bridge.cogos_channel_join(
        session_id="dev-laptop-cog-manager",
        channel_id="voice-room-primary",
        participant_id="cog",
        participant_type="overlord",
    )
    assert r["success"] is False
    assert "participant_type" in r["error"]
    assert r["bus_id"] == "channel.voice-room-primary.attendance"


def test_channel_join_rejects_blank_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    _disable_mod3(monkeypatch)
    _stub_post_and_get(monkeypatch, presence_sessions=[])

    r = cogos_bridge.cogos_channel_join(
        session_id="dev-laptop-cog-manager",
        channel_id="",
        participant_id="cog",
    )
    assert r["success"] is False
    assert "channel_id" in r["error"]

    r = cogos_bridge.cogos_channel_join(
        session_id="",
        channel_id="voice-room-primary",
        participant_id="cog",
    )
    assert r["success"] is False
    assert "session_id" in r["error"]

    r = cogos_bridge.cogos_channel_join(
        session_id="dev-laptop-cog-manager",
        channel_id="voice-room-primary",
        participant_id="",
    )
    assert r["success"] is False
    assert "participant_id" in r["error"]


# ---------- session-registered precondition ----------


def test_channel_join_fails_when_session_not_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the session isn't on the kernel roster, joining must be refused —
    otherwise attendance events land without a session record and confuse
    downstream rosters / attendance-EMA / handoff routing.
    """
    from cog_sandbox_mcp.tools import cogos_bridge

    _disable_mod3(monkeypatch)
    # Empty presence list — session_id won't be found.
    post_calls, get_calls = _stub_post_and_get(monkeypatch, presence_sessions=[])
    r = cogos_bridge.cogos_channel_join(
        session_id="dev-laptop-cog-manager",
        channel_id="voice-room-primary",
        participant_id="cog",
    )
    assert r["success"] is False
    assert "not registered" in r["error"]
    assert r["bus_id"] == "channel.voice-room-primary.attendance"
    # Must NOT have emitted an attendance event if the precondition failed.
    assert len(post_calls) == 0


# ---------- happy path ----------


def test_channel_join_emits_participant_joined_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core behaviour: emit ``participant.joined`` on
    ``channel.<id>.attendance`` via ``/v1/bus/send`` with the expected
    payload shape.
    """
    from cog_sandbox_mcp.tools import cogos_bridge

    _disable_mod3(monkeypatch)
    post_calls, _get_calls = _stub_post_and_get(
        monkeypatch,
        presence_sessions=[
            {"session_id": "dev-laptop-cog-manager", "active": True},
        ],
    )
    r = cogos_bridge.cogos_channel_join(
        session_id="dev-laptop-cog-manager",
        channel_id="voice-room-primary",
        participant_id="cog",
        participant_type="agent",
        preferred_voice="bm_lewis",
    )
    assert r["success"] is True
    assert r["channel_id"] == "voice-room-primary"
    assert r["attendance_event_seq"] == 43  # fake_post's first seq is 42+1
    assert r["participant_id"] == "cog"
    assert r["participant_type"] == "agent"
    assert r["joined_at"].startswith("20")  # ISO8601 from _utc_now_iso
    assert r["bus_id"] == "channel.voice-room-primary.attendance"
    assert r["mod3"] == {"registered": False, "warning": "mod3 disabled in test"}

    # Exactly one POST to /v1/bus/send with the right payload shape.
    assert len(post_calls) == 1
    c = post_calls[0]
    assert c["path"] == "/v1/bus/send"
    payload = c["payload"]
    assert payload["bus_id"] == "channel.voice-room-primary.attendance"
    assert payload["from"] == "cog"
    assert payload["type"] == "participant.joined"
    # ``message`` is a JSON-encoded string per the /v1/bus/send contract.
    body = json.loads(payload["message"])
    assert body["session_id"] == "dev-laptop-cog-manager"
    assert body["participant_id"] == "cog"
    assert body["participant_type"] == "agent"
    assert body["preferred_voice"] == "bm_lewis"
    assert body["joined_at"] == r["joined_at"]


def test_channel_join_omits_preferred_voice_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    _disable_mod3(monkeypatch)
    post_calls, _ = _stub_post_and_get(
        monkeypatch,
        presence_sessions=[{"session_id": "host-ws-role"}],
    )
    cogos_bridge.cogos_channel_join(
        session_id="host-ws-role",
        channel_id="voice-room-primary",
        participant_id="cog",
    )
    body = json.loads(post_calls[0]["payload"]["message"])
    assert "preferred_voice" not in body


def test_channel_join_accepts_provider_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The channel-provider RFC extends ``participant_type`` to include
    ``"provider"`` — mod3/discord/etc attend as providers, not agents.
    """
    from cog_sandbox_mcp.tools import cogos_bridge

    _disable_mod3(monkeypatch)
    _stub_post_and_get(
        monkeypatch,
        presence_sessions=[{"session_id": "mod3-provider-node-1"}],
    )
    r = cogos_bridge.cogos_channel_join(
        session_id="mod3-provider-node-1",
        channel_id="voice-room-primary",
        participant_id="mod3",
        participant_type="provider",
    )
    assert r["success"] is True
    assert r["participant_type"] == "provider"


# ---------- mod3 best-effort side effect ----------


def test_channel_join_calls_mod3_register(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When mod3 is reachable, the join tool must also call its
    ``/v1/sessions/register`` route so voice assignment happens in lockstep
    with attendance.
    """
    from cog_sandbox_mcp.tools import cogos_bridge

    mod3_calls = _disable_mod3(monkeypatch)
    _stub_post_and_get(
        monkeypatch,
        presence_sessions=[{"session_id": "host-ws-role"}],
    )
    cogos_bridge.cogos_channel_join(
        session_id="host-ws-role",
        channel_id="voice-room-primary",
        participant_id="cog",
        participant_type="agent",
        preferred_voice="bm_lewis",
    )
    assert len(mod3_calls) == 1
    call = mod3_calls[0]
    assert call["session_id"] == "host-ws-role"
    assert call["participant_id"] == "cog"
    assert call["participant_type"] == "agent"
    assert call["preferred_voice"] == "bm_lewis"
    assert call["channel_id"] == "voice-room-primary"


def test_channel_join_succeeds_when_mod3_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mod3 being down is non-fatal — the bus-side attendance still
    succeeds, and the response carries a warning so the caller knows voice
    assignment didn't happen.
    """
    from cog_sandbox_mcp.tools import cogos_bridge

    # Stub the kernel HTTP primitives so the bus-side emit succeeds. The
    # mod3 helper goes through ``urllib.request.urlopen`` directly (not
    # through ``_http_post_json``), so patching that separately makes the
    # mod3 side fail with URLError while the kernel side still works.
    _stub_post_and_get(
        monkeypatch,
        presence_sessions=[{"session_id": "host-ws-role"}],
    )

    def fake_urlopen(req: Any, timeout: float = 30.0) -> Any:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(cogos_bridge.urllib.request, "urlopen", fake_urlopen)
    r = cogos_bridge.cogos_channel_join(
        session_id="host-ws-role",
        channel_id="voice-room-primary",
        participant_id="cog",
    )
    assert r["success"] is True
    assert r["mod3"]["registered"] is False
    assert "unreachable" in r["mod3"]["warning"] or "URLError" in r["mod3"]["warning"]


def test_channel_join_mod3_honours_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``MOD3_URL`` env var overrides the default ``localhost:7860`` target."""
    from cog_sandbox_mcp.tools import cogos_bridge

    monkeypatch.setenv("MOD3_URL", "http://mod3.example.internal:8080")

    captured_urls: list[str] = []

    def fake_urlopen(req: Any, timeout: float = 30.0) -> Any:
        # Capture the URL the request targeted, then simulate unreachable.
        try:
            captured_urls.append(req.get_full_url())
        except AttributeError:
            captured_urls.append(str(req))
        raise urllib.error.URLError("no mod3 in test")

    monkeypatch.setattr(cogos_bridge.urllib.request, "urlopen", fake_urlopen)
    _stub_post_and_get(
        monkeypatch,
        presence_sessions=[{"session_id": "host-ws-role"}],
    )
    cogos_bridge.cogos_channel_join(
        session_id="host-ws-role",
        channel_id="voice-room-primary",
        participant_id="cog",
    )
    assert captured_urls
    assert captured_urls[0].startswith("http://mod3.example.internal:8080/")
    assert "/v1/sessions/register" in captured_urls[0]


# ---------- error passthrough ----------


def test_channel_join_never_raises_on_kernel_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the kernel rejects the emit (4xx/5xx), the tool surfaces the
    structured-error envelope rather than raising.
    """
    from cog_sandbox_mcp.tools import cogos_bridge

    _disable_mod3(monkeypatch)

    # presence succeeds so we get to the emit step…
    def fake_get(
        path: str, params: dict[str, Any] | None = None, timeout_s: float = 10.0
    ) -> Any:
        return {"sessions": [{"session_id": "host-ws-role"}], "count": 1}

    import io
    def fake_post(
        path: str, payload: dict[str, Any], timeout_s: float = 30.0
    ) -> dict[str, Any]:
        raise urllib.error.HTTPError(
            url="http://x",
            code=500,
            msg="boom",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"kernel exploded"}'),
        )

    monkeypatch.setattr(cogos_bridge, "_http_post_json", fake_post)
    monkeypatch.setattr(cogos_bridge, "_http_get_any_with_params", fake_get)

    r = cogos_bridge.cogos_channel_join(
        session_id="host-ws-role",
        channel_id="voice-room-primary",
        participant_id="cog",
    )
    assert r["success"] is False
    assert "500" in r["error"]
    assert r["bus_id"] == "channel.voice-room-primary.attendance"


# ---------- leave symmetry ----------


def test_channel_leave_emits_participant_left_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    post_calls: list[dict[str, Any]] = []

    def fake_post(
        path: str, payload: dict[str, Any], timeout_s: float = 30.0
    ) -> dict[str, Any]:
        post_calls.append({"path": path, "payload": payload})
        return {"ok": True, "seq": 77, "hash": "hash77"}

    monkeypatch.setattr(cogos_bridge, "_http_post_json", fake_post)
    r = cogos_bridge.cogos_channel_leave(
        session_id="dev-laptop-cog-manager",
        channel_id="voice-room-primary",
        participant_id="cog",
    )
    assert r["success"] is True
    assert r["channel_id"] == "voice-room-primary"
    assert r["departure_event_seq"] == 77
    assert r["bus_id"] == "channel.voice-room-primary.attendance"
    assert r["left_at"].startswith("20")

    assert len(post_calls) == 1
    c = post_calls[0]
    assert c["path"] == "/v1/bus/send"
    p = c["payload"]
    assert p["bus_id"] == "channel.voice-room-primary.attendance"
    assert p["type"] == "participant.left"
    assert p["from"] == "cog"
    body = json.loads(p["message"])
    assert body["session_id"] == "dev-laptop-cog-manager"
    assert body["participant_id"] == "cog"
    assert body["left_at"] == r["left_at"]


def test_channel_leave_defaults_sender_to_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If participant_id is omitted, the ``from`` field defaults to the
    session_id so events remain attributable.
    """
    from cog_sandbox_mcp.tools import cogos_bridge

    post_calls: list[dict[str, Any]] = []

    def fake_post(
        path: str, payload: dict[str, Any], timeout_s: float = 30.0
    ) -> dict[str, Any]:
        post_calls.append({"path": path, "payload": payload})
        return {"ok": True, "seq": 1, "hash": "h"}

    monkeypatch.setattr(cogos_bridge, "_http_post_json", fake_post)
    cogos_bridge.cogos_channel_leave(
        session_id="dev-laptop-cog-manager",
        channel_id="voice-room-primary",
    )
    assert post_calls[0]["payload"]["from"] == "dev-laptop-cog-manager"


def test_channel_leave_rejects_blank_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cog_sandbox_mcp.tools import cogos_bridge

    r = cogos_bridge.cogos_channel_leave(
        session_id="host-ws-role", channel_id=""
    )
    assert r["success"] is False
    assert "channel_id" in r["error"]

    r = cogos_bridge.cogos_channel_leave(
        session_id="", channel_id="voice-room-primary"
    )
    assert r["success"] is False
    assert "session_id" in r["error"]
