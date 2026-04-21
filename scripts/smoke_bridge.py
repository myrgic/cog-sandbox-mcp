"""
Smoke-test the cog-sandbox MCP server's Cog OS bridge over stdio.

Spawns the podman container with the same args LM Studio uses (per
~/.cache/lm-studio/mcp.json), speaks MCP JSON-RPC on stdio, and:

  1. Lists tools — expects cogos_status, cogos_emit, and cogos_events_read
     (confirms bridge registration saw COG_OS_BASE_URL at import time).
  2. Calls cogos_status — expects reachable=true, proving the container
     can reach the configured kernel URL.
  3. Calls cogos_emit against a probe bus — expects the kernel's JSON
     response (or a structured error, but never an unhandled raise).
  4. Calls cogos_events_read on the same bus — expects the emit's seq to
     appear in the returned events (the roundtrip assertion).

Run:
    python scripts/smoke_bridge.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PODMAN = r"C:\Program Files\RedHat\Podman\podman.exe"
IMAGE = "cog-sandbox-mcp:0.1"
WORKSPACE_MOUNT = r"C:\Users\chazm\work:/workspace:rw"
COG_OS_URL = "http://192.168.10.140:5100"
INITIAL_AUTH = "cog-workspace"
PROBE_BUS_ID = "agent-smoke-test"
PROBE_MESSAGE = "hello from desktop"


def _jsonrpc(method: str, params: dict | None, rpc_id: int | None) -> bytes:
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if rpc_id is not None:
        msg["id"] = rpc_id
    if params is not None:
        msg["params"] = params
    return (json.dumps(msg) + "\n").encode("utf-8")


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _read_response(proc: subprocess.Popen, rpc_id: int, timeout: float = 15.0) -> dict:
    assert proc.stdout is not None
    while True:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("MCP server closed stdout before responding")
        try:
            msg = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError:
            continue
        if msg.get("id") == rpc_id:
            return msg


def main() -> int:
    args = [
        PODMAN, "run", "--rm", "-i",
        "-v", WORKSPACE_MOUNT,
        "-e", f"COG_SANDBOX_INITIAL_AUTH={INITIAL_AUTH}",
        "-e", f"COG_OS_BASE_URL={COG_OS_URL}",
        IMAGE,
    ]
    _log(f"[smoke] spawning: {' '.join(args)}")

    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    assert proc.stdin is not None and proc.stdout is not None

    try:
        proc.stdin.write(_jsonrpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "smoke-bridge", "version": "0.0.1"},
        }, 1))
        proc.stdin.flush()
        init = _read_response(proc, 1)
        server_info = init.get("result", {}).get("serverInfo", {})
        _log(f"[smoke] server: {server_info.get('name')}@{server_info.get('version')}")

        proc.stdin.write(_jsonrpc("notifications/initialized", {}, None))
        proc.stdin.flush()

        proc.stdin.write(_jsonrpc("tools/list", {}, 2))
        proc.stdin.flush()
        tools = _read_response(proc, 2).get("result", {}).get("tools", [])
        names = sorted(t["name"] for t in tools)
        _log(f"[smoke] {len(names)} tools: {names}")

        required = {"cogos_status", "cogos_emit", "cogos_events_read"}
        missing = required - set(names)
        if missing:
            _log(f"[smoke] FAIL: bridge tools missing: {sorted(missing)} — COG_OS_BASE_URL didn't reach the container")
            return 2

        # ---- 1) cogos_status (read probe) ----
        proc.stdin.write(_jsonrpc("tools/call", {
            "name": "cogos_status",
            "arguments": {},
        }, 3))
        proc.stdin.flush()
        status_res = _read_response(proc, 3, timeout=20.0).get("result", {})
        _log("[smoke] cogos_status result:")
        print(json.dumps(status_res, indent=2), flush=True)

        status_text = "".join(
            c.get("text", "")
            for c in status_res.get("content", [])
            if c.get("type") == "text"
        )
        if "reachable" not in status_text:
            _log("[smoke] FAIL: cogos_status result didn't look like a bridge payload")
            return 3
        if not ('"reachable": true' in status_text or '"reachable":true' in status_text):
            _log("[smoke] FAIL: bridge registered but couldn't reach kernel")
            return 4

        # ---- 2) cogos_emit (write probe) ----
        proc.stdin.write(_jsonrpc("tools/call", {
            "name": "cogos_emit",
            "arguments": {
                "bus_id": PROBE_BUS_ID,
                "message": PROBE_MESSAGE,
                "from_sender": "desktop-smoke",
                "event_type": "smoke",
            },
        }, 4))
        proc.stdin.flush()
        emit_res = _read_response(proc, 4, timeout=20.0).get("result", {})
        _log("[smoke] cogos_emit result:")
        print(json.dumps(emit_res, indent=2), flush=True)

        emit_text = "".join(
            c.get("text", "")
            for c in emit_res.get("content", [])
            if c.get("type") == "text"
        )
        if not emit_text:
            _log("[smoke] FAIL: cogos_emit returned no content")
            return 5
        # Accept either the kernel's verbatim response or a structured error.
        # The contract is: never raise unhandled, always structured JSON.
        try:
            payload = json.loads(emit_text) if emit_text.strip().startswith("{") else None
        except json.JSONDecodeError:
            payload = None
        if payload and payload.get("success") is False:
            _log(f"[smoke] WARN: cogos_emit returned structured error: {payload.get('error')}")
            _log("[smoke] PARTIAL — tools registered + status reachable, but emit failed. "
                 f"Check laptop: curl {COG_OS_URL}/v1/bus/{PROBE_BUS_ID}/events")
            return 6

        emitted_seq = payload.get("seq") if isinstance(payload, dict) else None

        # ---- 3) cogos_events_read (roundtrip assertion) ----
        proc.stdin.write(_jsonrpc("tools/call", {
            "name": "cogos_events_read",
            "arguments": {"bus_id": PROBE_BUS_ID, "limit": 10},
        }, 5))
        proc.stdin.flush()
        read_res = _read_response(proc, 5, timeout=20.0).get("result", {})
        _log("[smoke] cogos_events_read result:")
        print(json.dumps(read_res, indent=2), flush=True)

        read_struct = read_res.get("structuredContent") or {}
        if read_struct.get("success") is False:
            _log(f"[smoke] FAIL: cogos_events_read returned error: {read_struct.get('error')}")
            return 7

        events = read_struct.get("events") or []
        if not events:
            _log(f"[smoke] FAIL: events list for {PROBE_BUS_ID} was empty after emit")
            return 8

        if emitted_seq is not None:
            seqs = [e.get("seq") for e in events]
            if emitted_seq not in seqs:
                _log(f"[smoke] FAIL: emitted seq={emitted_seq} not found in events (saw {seqs})")
                return 9
            _log(f"[smoke] roundtrip OK — emitted seq={emitted_seq} present in {len(events)} events")
        else:
            _log(f"[smoke] WARN: emit response didn't include 'seq'; skipping strict seq check")

        _log(f"[smoke] PASS — status reachable; emit→read roundtrip on {PROBE_BUS_ID}")
        return 0

    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        stderr_tail = (proc.stderr.read() or b"")[-2048:].decode("utf-8", "replace")
        if stderr_tail.strip():
            _log(f"[smoke] server stderr tail:\n{stderr_tail}")


if __name__ == "__main__":
    raise SystemExit(main())
