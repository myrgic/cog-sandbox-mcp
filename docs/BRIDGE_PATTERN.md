# Cog OS Bridge Tool Pattern

Conventions for writing new bridge tools in `src/cog_sandbox_mcp/tools/cogos_bridge.py`. Derived from v0.3.1–0.3.4 (`cogos_status`, `cogos_emit`, `cogos_events_read`, `cogos_resolve`).

> Transport-agnostic: these tools are registered on the same `FastMCP` instance regardless of whether the server is running in stdio or HTTP mode. See the "HTTP transport" section of [`README.md`](../README.md) for how to run a shared HTTP server that multiple Claude Code sessions connect to via `mcp-remote`.

## Registration

Tools are registered **iff `COG_OS_BASE_URL` is set at server startup.** The `register()` function gates on `is_bridge_enabled()` and returns early when the env var is missing. Don't gate inside each tool — the whole suite either appears or doesn't.

```python
def register(mcp: FastMCP) -> None:
    if not is_bridge_enabled():
        return
    mcp.tool(title=..., annotations=...)(cogos_tool_fn)
```

## Never-raise contract

Every bridge tool catches exceptions and returns a structured dict. Callers (LLM agents) should distinguish failure by `"success": False` in the response, not by exception.

**Required catch order:** `HTTPError → URLError → Exception`. HTTPError first because it's a subclass of URLError but carries status + body; we want to surface those before the generic URL path.

```python
try:
    return _http_post_json("/v1/...", payload)
except urllib.error.HTTPError as e:
    body = _safe_read(e)
    return {"success": False, "error": f"HTTP {e.code} {e.reason} — {body}", ...}
except urllib.error.URLError as e:
    return {"success": False, "error": f"{type(e).__name__}: {e}", ...}
except Exception as e:
    return {"success": False, "error": f"{type(e).__name__}: {e}", ...}
```

Echo request identifiers (`bus_id`, `uri`, etc.) in the error dict so the agent can reason about whether it typo'd the input or the target was missing.

## Success-path shape

Return the kernel's JSON response **verbatim** on success — no wrapping. Exception: when the kernel returns a bare list (e.g., `GET /v1/bus/{id}/events`), wrap as `{"bus_id": ..., "events": [...], "count": N}` so the shape is a dict.

## Annotations

Match the tool's semantics:

| Tool kind | `readOnlyHint` | `idempotentHint` | `openWorldHint` | Notes |
|---|---|---|---|---|
| Read (status, events_read, resolve) | `True` | `True` | `True` | |
| Write-append (emit) | `False` | `False` | `True` | **Not** `destructiveHint` — append ≠ destroy |
| Write-mutate (future mutate) | `False` | `False` | `True` | Consider `destructiveHint=True` for delete ops |

## Docstring — "CALL THIS WHEN"

Prescriptive language, not descriptive. Every tool's docstring should:

1. State the endpoint it hits and the call shape.
2. Describe the success vs failure return shape explicitly.
3. Include a `CALL THIS WHEN` paragraph telling the agent when to reach for it.
4. Name the **disambiguation** rule — what to do when the agent lacks the key input (e.g., bus_id, URI). The default should be "ask the user; do not invent." This came out of v0.3.1 evals where Gemma silently resolved ambiguous entity references instead of asking.

## Base64 decoding (for resource-fetch tools)

The kernel wire-encodes `/resolve` content as base64. Consumers want text. Convention:

- Default `decode=True`: base64 → UTF-8. On success, `content` holds decoded text.
- On decode failure (binary data, malformed): fall back gracefully — populate both `content` and `raw_content` with the base64 string, plus a `decode_error` note describing what failed. Never raise.
- `decode=False` override: skip decode, both fields hold base64, caller handles bytes.

This pattern generalizes to any future tool that fetches wire-encoded payloads.

## Smoke-script convention

Each tool ships a smoke case in `scripts/smoke_bridge.py`. The script:
- Spawns the podman container stdio-style (same args as `mcp.json`)
- Drives MCP JSON-RPC directly — **no LM Studio required**
- Asserts the tool appears in the list when `COG_OS_BASE_URL` is set
- Exercises the happy path against a real kernel
- For paired tools (emit/read), asserts roundtrip properties (seq monotonicity, hash chain integrity)

Keep smoke scripts self-contained so they can run standalone for cross-machine LAN validation.

## Tests

Per tool, at minimum:
1. Registers iff `COG_OS_BASE_URL` set, absent when unset.
2. Happy path against a threaded `http.server` mock — verifies request path, body/query, response passthrough.
3. Closed-port / timeout test — asserts structured-error shape, no exception propagation.
4. For paired tools, a **roundtrip integration test** using a single mock that services both halves.

## What's blocked on kernel-side work

These bridges need new kernel HTTP surfaces before they can be written:

- **`cogos_fleet_spawn`** — no `/v1/fleet/*` endpoints on the kernel yet; spawning is CLI-only.
- **`cogos_query` (CQL)** — CQL is CLI-only in `cog.go`; no `/v1/query` HTTP route.
- **`cogos_watch`** — `GET /v1/events/stream` (SSE) and `GET /ws/watch` (WebSocket) exist on the kernel, but MCP's request/response model doesn't map cleanly to streaming. Design decision required (polling vs dedicated streaming channel vs a snapshot-with-timeout compromise).

When the kernel-side work lands, follow the patterns above.
