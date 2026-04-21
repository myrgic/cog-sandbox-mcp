import json
from pathlib import Path
from typing import Any

import pytest

from cog_sandbox_mcp import sandbox
from cog_sandbox_mcp.tools import authorization, dedup, fs


# ---------- virtualization basics ----------


def test_resolve_virtual_rejects_unauthorized_as_notfound(workspace: Path) -> None:
    with pytest.raises(FileNotFoundError):
        sandbox.resolve_virtual("other-ws/file.txt")


def test_resolve_virtual_rejects_parent_escape(workspace: Path) -> None:
    (workspace.parent / "outside.txt").write_text("secret")
    with pytest.raises(FileNotFoundError):
        sandbox.resolve_virtual("ws/../outside.txt")


def test_resolve_virtual_accepts_abs_looking_path(workspace: Path) -> None:
    (workspace / "f.txt").write_text("x")
    p = sandbox.resolve_virtual("/ws/f.txt")
    assert p == workspace / "f.txt"


# ---------- initial auth ----------


def test_initialize_auth_requires_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COG_SANDBOX_ROOT", str(tmp_path))
    monkeypatch.delenv("COG_SANDBOX_INITIAL_AUTH", raising=False)
    with pytest.raises(RuntimeError, match="COG_SANDBOX_INITIAL_AUTH"):
        sandbox.initialize_auth()


def test_initialize_auth_requires_workspace_to_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COG_SANDBOX_ROOT", str(tmp_path))
    monkeypatch.setenv("COG_SANDBOX_INITIAL_AUTH", "does-not-exist")
    with pytest.raises(FileNotFoundError):
        sandbox.initialize_auth()


def test_initialize_auth_multiple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    monkeypatch.setenv("COG_SANDBOX_ROOT", str(tmp_path))
    monkeypatch.setenv("COG_SANDBOX_INITIAL_AUTH", "alpha:beta")
    sandbox.initialize_auth()
    assert sandbox.authorized_workspace_names() == ["alpha", "beta"]


# ---------- read/write/edit ----------


def test_read_write_edit_roundtrip(workspace: Path) -> None:
    fs.write("ws/hello.txt", "hello world")
    assert fs.read("ws/hello.txt") == "hello world"
    fs.edit("ws/hello.txt", "world", "there")
    assert fs.read("ws/hello.txt") == "hello there"


def test_write_rejects_unauthorized_workspace(workspace: Path) -> None:
    with pytest.raises(FileNotFoundError):
        fs.write("other/hello.txt", "x")


def test_edit_requires_unique_match(workspace: Path) -> None:
    fs.write("ws/dup.txt", "aa bb aa bb")
    with pytest.raises(ValueError, match="matches"):
        fs.edit("ws/dup.txt", "aa", "xx")
    result = fs.edit("ws/dup.txt", "aa", "xx", replace_all=True)
    assert "2 occurrence" in result
    assert fs.read("ws/dup.txt") == "xx bb xx bb"


def test_edit_rejects_identical_strings(workspace: Path) -> None:
    fs.write("ws/f.txt", "hi")
    with pytest.raises(ValueError, match="must differ"):
        fs.edit("ws/f.txt", "hi", "hi")


# ---------- glob / list_directory / tree ----------


def test_glob_returns_virtual_paths(workspace: Path) -> None:
    fs.write("ws/a.py", "")
    fs.write("ws/b.py", "")
    fs.write("ws/c.txt", "")
    results = fs.glob("**/*.py")
    assert set(results) == {"ws/a.py", "ws/b.py"}


def test_list_directory_at_root_returns_workspaces(workspace: Path) -> None:
    result = fs.list_directory("")
    names = {e["name"] for e in result["entries"]}
    assert names == {"ws"}
    assert all(e["type"] == "directory" for e in result["entries"])


def test_list_directory_inside_workspace(workspace: Path) -> None:
    (workspace / "sub").mkdir()
    (workspace / "f.txt").write_text("hi")
    result = fs.list_directory("ws")
    types = {e["name"]: e["type"] for e in result["entries"]}
    assert types == {"sub": "directory", "f.txt": "file"}


def test_list_directory_unauthorized_is_notfound(workspace: Path) -> None:
    with pytest.raises(FileNotFoundError):
        fs.list_directory("nope")


def test_tree_bounded_by_depth(workspace: Path) -> None:
    (workspace / "a" / "b" / "c").mkdir(parents=True)
    (workspace / "a" / "b" / "c" / "deep.txt").write_text("x")
    out = fs.tree("ws", max_depth=1)
    assert "a/" in out
    # depth=1 means only a/ is shown as a child of ws, not its contents
    assert "deep.txt" not in out


def test_tree_at_root_covers_workspaces(workspace: Path) -> None:
    out = fs.tree("")
    assert "ws/" in out


# ---------- authorization tools ----------


def test_list_authorized_paths_hides_sandbox_root(workspace: Path) -> None:
    result = authorization.list_authorized_paths()
    assert result == {"authorized_paths": ["ws"]}
    # The real /workspace or tmp_path must not appear in output
    assert str(workspace.parent) not in str(result)


def test_grant_path_access_rejects_nonexistent(workspace: Path) -> None:
    with pytest.raises(FileNotFoundError):
        authorization.grant_path_access("does-not-exist", reason="probing")


def test_grant_path_access_rejects_path_separator(workspace: Path) -> None:
    (workspace.parent / "ws2").mkdir()
    with pytest.raises(ValueError, match="single component"):
        authorization.grant_path_access("ws/nested", reason="nope")


def test_grant_path_access_adds_workspace(workspace: Path) -> None:
    (workspace.parent / "beta").mkdir()
    result = authorization.grant_path_access("beta", reason="need beta")
    assert result["granted"] == "beta"
    assert set(result["authorized_paths"]) == {"ws", "beta"}
    # Now reachable
    fs.write("beta/file.txt", "x")
    assert fs.read("beta/file.txt") == "x"


def test_grant_requires_nonempty_reason(workspace: Path) -> None:
    with pytest.raises(ValueError, match="reason"):
        authorization.grant_path_access("ws", reason="")


def test_revoke_path_access_narrows_reach(workspace: Path) -> None:
    fs.write("ws/x.txt", "hi")
    result = authorization.revoke_path_access("ws")
    assert result["was_authorized"] is True
    assert result["authorized_paths"] == []
    with pytest.raises(FileNotFoundError):
        fs.read("ws/x.txt")


def test_revoke_returns_false_if_not_present(workspace: Path) -> None:
    result = authorization.revoke_path_access("never-granted")
    assert result["was_authorized"] is False


# ---------- dedup ----------


def test_find_and_consolidate_duplicates_delete(workspace: Path) -> None:
    (workspace / "a.txt").write_text("same content")
    (workspace / "b.txt").write_text("same content")
    (workspace / "c.txt").write_text("different")
    found = dedup.find_duplicates()
    assert found["duplicate_groups"] == 1
    paths = found["duplicates"][0]["paths"]
    assert all(p.startswith("ws/") for p in paths)
    applied = dedup.consolidate_duplicates(
        found["plan_id"], strategy="delete", keep="first"
    )
    assert applied["applied"] == 1
    assert applied["errors"] == []
    surviving = sorted(p.name for p in workspace.iterdir() if p.is_file())
    assert surviving == ["a.txt", "c.txt"]


def test_consolidate_unknown_plan(workspace: Path) -> None:
    with pytest.raises(ValueError, match="unknown"):
        dedup.consolidate_duplicates("nonexistent-plan")


def test_hash_file_returns_virtual_path(workspace: Path) -> None:
    (workspace / "x.bin").write_bytes(b"hello")
    result = dedup.hash_file("ws/x.bin")
    assert result["path"] == "ws/x.bin"
    assert result["size"] == 5
    assert len(result["hash"]) == 64


# ---------- cogos bridge gating ----------


def test_cogos_bridge_disabled_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COG_OS_BASE_URL", raising=False)
    from cog_sandbox_mcp.tools import cogos_bridge
    assert cogos_bridge.is_bridge_enabled() is False


def test_cogos_bridge_enabled_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COG_OS_BASE_URL", "http://localhost:5100")
    from cog_sandbox_mcp.tools import cogos_bridge
    assert cogos_bridge.is_bridge_enabled() is True


def test_cogos_status_reports_unreachable_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Point at an unreachable host; status should return a structured error,
    # not raise. This is the load-bearing contract — agents rely on status as a
    # safe probe they can call without fear of unhandled exceptions.
    monkeypatch.setenv("COG_OS_BASE_URL", "http://127.0.0.1:1")  # closed port
    from cog_sandbox_mcp.tools import cogos_bridge
    result = cogos_bridge.cogos_status()
    assert result["reachable"] is False
    assert "error" in result
    assert result["base_url"] == "http://127.0.0.1:1"


def test_cogos_bridge_not_registered_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Register pipeline with no COG_OS_BASE_URL — cogos_status must not appear.
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("COG_SANDBOX_ROOT", str(tmp_path))
    monkeypatch.setenv("COG_SANDBOX_INITIAL_AUTH", "ws")
    monkeypatch.delenv("COG_OS_BASE_URL", raising=False)
    sandbox.initialize_auth()
    from cog_sandbox_mcp.server import build_server
    import asyncio
    tools = asyncio.run(build_server().list_tools())
    names = [t.name for t in tools]
    assert "cogos_status" not in names


def test_cogos_bridge_registered_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("COG_SANDBOX_ROOT", str(tmp_path))
    monkeypatch.setenv("COG_SANDBOX_INITIAL_AUTH", "ws")
    monkeypatch.setenv("COG_OS_BASE_URL", "http://localhost:5100")
    sandbox.initialize_auth()
    from cog_sandbox_mcp.server import build_server
    import asyncio
    tools = asyncio.run(build_server().list_tools())
    names = [t.name for t in tools]
    assert "cogos_status" in names
    assert "cogos_emit" in names
    assert "cogos_events_read" in names
    assert "cogos_resolve" in names


def test_cogos_emit_not_registered_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("COG_SANDBOX_ROOT", str(tmp_path))
    monkeypatch.setenv("COG_SANDBOX_INITIAL_AUTH", "ws")
    monkeypatch.delenv("COG_OS_BASE_URL", raising=False)
    sandbox.initialize_auth()
    from cog_sandbox_mcp.server import build_server
    import asyncio
    tools = asyncio.run(build_server().list_tools())
    names = [t.name for t in tools]
    assert "cogos_emit" not in names


def test_cogos_emit_posts_bus_send_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Stand up a tiny HTTP server, point the bridge at it, emit once, verify
    # that the wire path + body + response pass through faithfully.
    import http.server
    import threading

    captured: dict[str, Any] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 — stdlib name
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            captured["path"] = self.path
            captured["body"] = json.loads(body)
            resp = json.dumps({"success": True, "event_id": "evt-123"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
            pass  # silence per-request logging in the test output

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("COG_OS_BASE_URL", f"http://127.0.0.1:{port}")
        from cog_sandbox_mcp.tools import cogos_bridge

        result = cogos_bridge.cogos_emit(
            bus_id="test-bus",
            message="hello from test",
            from_sender="pytest",
            event_type="smoke",
        )
    finally:
        server.shutdown()
        server.server_close()

    assert captured["path"] == "/v1/bus/send"
    assert captured["body"] == {
        "bus_id": "test-bus",
        "message": "hello from test",
        "from": "pytest",
        "type": "smoke",
    }
    # Kernel's response comes back verbatim on success.
    assert result == {"success": True, "event_id": "evt-123"}


def test_cogos_emit_returns_structured_error_on_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Closed port — should come back as a structured error, not raise, so the
    # agent can reason about the failure without trampolining out of its loop.
    monkeypatch.setenv("COG_OS_BASE_URL", "http://127.0.0.1:1")
    from cog_sandbox_mcp.tools import cogos_bridge

    result = cogos_bridge.cogos_emit(bus_id="abandoned", message="into the void")
    assert result["success"] is False
    assert "error" in result
    assert result["bus_id"] == "abandoned"


def test_cogos_events_read_not_registered_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("COG_SANDBOX_ROOT", str(tmp_path))
    monkeypatch.setenv("COG_SANDBOX_INITIAL_AUTH", "ws")
    monkeypatch.delenv("COG_OS_BASE_URL", raising=False)
    sandbox.initialize_auth()
    from cog_sandbox_mcp.server import build_server
    import asyncio
    tools = asyncio.run(build_server().list_tools())
    names = [t.name for t in tools]
    assert "cogos_events_read" not in names


def test_cogos_events_read_gets_with_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mock server asserts path + query-string serialization, replies with a
    # canned events list so we can verify the {bus_id, events, count} wrapper.
    import http.server
    import threading
    from urllib.parse import parse_qs, urlparse

    captured: dict[str, Any] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — stdlib name
            u = urlparse(self.path)
            captured["path"] = u.path
            captured["query"] = parse_qs(u.query)
            events = [
                {"seq": 1, "type": "message", "from": "a", "payload": {"content": "one"}},
                {"seq": 2, "type": "message", "from": "a", "payload": {"content": "two"}},
            ]
            body = json.dumps(events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("COG_OS_BASE_URL", f"http://127.0.0.1:{port}")
        from cog_sandbox_mcp.tools import cogos_bridge

        result = cogos_bridge.cogos_events_read(
            bus_id="my-bus",
            after_seq=5,
            event_type="message",
            from_sender="alice",
            limit=25,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert captured["path"] == "/v1/bus/my-bus/events"
    # urllib query parse returns list values.
    assert captured["query"] == {
        "limit": ["25"],
        "after": ["5"],
        "type": ["message"],
        "from": ["alice"],
    }
    assert result["bus_id"] == "my-bus"
    assert result["count"] == 2
    assert result["events"][0]["payload"]["content"] == "one"


def test_cogos_events_read_returns_structured_error_on_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COG_OS_BASE_URL", "http://127.0.0.1:1")
    from cog_sandbox_mcp.tools import cogos_bridge

    result = cogos_bridge.cogos_events_read(bus_id="nowhere")
    assert result["success"] is False
    assert "error" in result
    assert result["bus_id"] == "nowhere"


def test_cogos_emit_then_read_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    # Single mock kernel that accepts POSTs to /v1/bus/send (appends) and GETs
    # to /v1/bus/<id>/events (returns current list for that bus). Verifies the
    # emit → read loop end-to-end against our own wrapper logic.
    import http.server
    import threading
    from urllib.parse import urlparse

    buses: dict[str, list[dict[str, Any]]] = {}
    seq_counter = {"n": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/bus/send":
                self.send_response(404); self.end_headers(); return
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            seq_counter["n"] += 1
            event = {
                "seq": seq_counter["n"],
                "bus_id": body["bus_id"],
                "type": body.get("type"),
                "from": body.get("from"),
                "payload": {"content": body["message"]},
            }
            buses.setdefault(body["bus_id"], []).append(event)
            resp = json.dumps({"ok": True, "seq": event["seq"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def do_GET(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            parts = u.path.split("/")
            # /v1/bus/<id>/events
            if len(parts) != 5 or parts[:3] != ["", "v1", "bus"] or parts[4] != "events":
                self.send_response(404); self.end_headers(); return
            bus_id = parts[3]
            events = list(buses.get(bus_id, []))
            body = json.dumps(events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("COG_OS_BASE_URL", f"http://127.0.0.1:{port}")
        from cog_sandbox_mcp.tools import cogos_bridge

        emit_result = cogos_bridge.cogos_emit(
            bus_id="round-trip",
            message="ping",
            from_sender="tester",
            event_type="probe",
        )
        read_result = cogos_bridge.cogos_events_read(bus_id="round-trip")
    finally:
        server.shutdown()
        server.server_close()

    assert emit_result == {"ok": True, "seq": 1}
    assert read_result["bus_id"] == "round-trip"
    assert read_result["count"] == 1
    event = read_result["events"][0]
    assert event["seq"] == 1
    assert event["from"] == "tester"
    assert event["type"] == "probe"
    assert event["payload"]["content"] == "ping"


def test_cogos_resolve_not_registered_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("COG_SANDBOX_ROOT", str(tmp_path))
    monkeypatch.setenv("COG_SANDBOX_INITIAL_AUTH", "ws")
    monkeypatch.delenv("COG_OS_BASE_URL", raising=False)
    sandbox.initialize_auth()
    from cog_sandbox_mcp.server import build_server
    import asyncio
    tools = asyncio.run(build_server().list_tools())
    names = [t.name for t in tools]
    assert "cogos_resolve" not in names


def _start_resolve_mock(
    handler_payload: dict[str, Any] | None = None,
    status: int = 200,
    raw_body: bytes | None = None,
):
    """Spin up a threaded HTTP server that captures /resolve requests.

    Returns (server, captured_dict). Caller is responsible for server.shutdown().
    """
    import http.server
    import threading
    from urllib.parse import parse_qs, urlparse

    captured: dict[str, Any] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            captured["path"] = u.path
            captured["query"] = parse_qs(u.query)
            captured["raw_query"] = u.query
            if status >= 400:
                body = raw_body or json.dumps(
                    {"error": {"message": "bogus URI", "type": "not_found"}}
                ).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = json.dumps(handler_payload or {}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, captured


def test_cogos_resolve_decodes_base64_to_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64 as b64

    text = "---\ntitle: ADR-085\n---\n\n# Body"
    encoded = b64.b64encode(text.encode("utf-8")).decode("ascii")
    server, captured = _start_resolve_mock(
        {"uri": "cog://adr/085", "content": encoded, "etag": "abc123"}
    )
    try:
        monkeypatch.setenv("COG_OS_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
        from cog_sandbox_mcp.tools import cogos_bridge

        result = cogos_bridge.cogos_resolve("cog://adr/085")
    finally:
        server.shutdown()
        server.server_close()

    assert captured["path"] == "/resolve"
    assert captured["query"] == {"uri": ["cog://adr/085"]}
    assert result["uri"] == "cog://adr/085"
    assert result["etag"] == "abc123"
    assert result["content"] == text
    assert "raw_content" not in result
    assert "decode_error" not in result


def test_cogos_resolve_no_decode_preserves_base64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64 as b64

    encoded = b64.b64encode(b"binary-bytes-here").decode("ascii")
    server, _ = _start_resolve_mock(
        {"uri": "cog://blob/abc", "content": encoded}
    )
    try:
        monkeypatch.setenv("COG_OS_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
        from cog_sandbox_mcp.tools import cogos_bridge

        result = cogos_bridge.cogos_resolve("cog://blob/abc", decode=False)
    finally:
        server.shutdown()
        server.server_close()

    assert result["content"] == encoded
    assert result["raw_content"] == encoded
    assert "decode_error" not in result


def test_cogos_resolve_falls_back_on_non_utf8_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64 as b64

    # Random binary bytes that aren't valid UTF-8.
    encoded = b64.b64encode(bytes([0xff, 0xfe, 0x00, 0x80])).decode("ascii")
    server, _ = _start_resolve_mock(
        {"uri": "cog://blob/xyz", "content": encoded}
    )
    try:
        monkeypatch.setenv("COG_OS_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
        from cog_sandbox_mcp.tools import cogos_bridge

        result = cogos_bridge.cogos_resolve("cog://blob/xyz")
    finally:
        server.shutdown()
        server.server_close()

    assert result["content"] == encoded
    assert result["raw_content"] == encoded
    assert "decode_error" in result
    assert "utf-8" in result["decode_error"].lower()


def test_cogos_resolve_returns_structured_error_on_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, _ = _start_resolve_mock(status=500)
    try:
        monkeypatch.setenv("COG_OS_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
        from cog_sandbox_mcp.tools import cogos_bridge

        result = cogos_bridge.cogos_resolve("cog://nonsense/999")
    finally:
        server.shutdown()
        server.server_close()

    assert result["success"] is False
    assert result["uri"] == "cog://nonsense/999"
    assert "error" in result
    # Kernel's error.message should be surfaced through the detail string.
    assert "bogus URI" in result["error"]


def test_cogos_resolve_url_quotes_special_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64 as b64

    # URI with &, ?, and space — naive concatenation would break the
    # querystring. Verify the tool url-encodes all of it and the mock server
    # receives exactly one `uri` parameter with the full literal value.
    gnarly = "cog://notes/2026-04-20?draft&title=Plan A"
    encoded = b64.b64encode(b"hello").decode("ascii")
    server, captured = _start_resolve_mock(
        {"uri": gnarly, "content": encoded}
    )
    try:
        monkeypatch.setenv("COG_OS_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
        from cog_sandbox_mcp.tools import cogos_bridge

        result = cogos_bridge.cogos_resolve(gnarly)
    finally:
        server.shutdown()
        server.server_close()

    assert captured["query"] == {"uri": [gnarly]}
    # Raw querystring must NOT contain a bare & or ? beyond the first separator —
    # everything after `uri=` should be a single percent-encoded blob.
    raw = captured["raw_query"]
    assert raw.startswith("uri=")
    assert raw.count("&") == 0
    assert raw.count("?") == 0
    assert result["content"] == "hello"
