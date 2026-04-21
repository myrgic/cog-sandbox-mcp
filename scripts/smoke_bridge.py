"""
Smoke-test the cog-sandbox MCP server's Cog OS bridge over stdio.

Spawns the podman container with the same args LM Studio uses (per
~/.cache/lm-studio/mcp.json), speaks MCP JSON-RPC on stdio, and:

  1. Lists tools — expects cogos_status to be present (confirms the
     bridge-registration path saw COG_OS_BASE_URL at import time).
  2. Calls cogos_status — expects a structured payload pointing at the
     laptop kernel, proving the container can actually reach the URL.

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

        if "cogos_status" not in names:
            _log("[smoke] FAIL: cogos_status not registered — COG_OS_BASE_URL didn't reach the container")
            return 2

        proc.stdin.write(_jsonrpc("tools/call", {
            "name": "cogos_status",
            "arguments": {},
        }, 3))
        proc.stdin.flush()
        call_res = _read_response(proc, 3, timeout=20.0).get("result", {})
        _log("[smoke] cogos_status result:")
        print(json.dumps(call_res, indent=2), flush=True)

        content = call_res.get("content", [])
        payload_text = "".join(c.get("text", "") for c in content if c.get("type") == "text")
        if "reachable" not in payload_text:
            _log("[smoke] FAIL: result didn't look like a bridge payload")
            return 3

        if '"reachable": true' in payload_text or '"reachable":true' in payload_text:
            _log("[smoke] PASS - bridge reached the laptop kernel")
            return 0
        else:
            _log("[smoke] FAIL: bridge registered but couldn't reach kernel")
            return 4

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
