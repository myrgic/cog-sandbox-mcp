# Changelog

## 0.4.1 — 2026-04-21

Eight new bridge tools composing over the kernel's `/v1/bus/*` endpoints to deliver the session-identity + handoff substrate. Tool count goes 4 → 12. Protocol spec lands in `docs/HANDOFF_PROTOCOL.md`.

**New tools (sessions):**
- `cogos_session_register(session_id, workspace, role, task, model=None, hostname=None)` — announces a session's presence on `bus_sessions`. Uses the `<hostname>-<workspace-slug>-<session-slug>` identity format from the spec.
- `cogos_session_heartbeat(session_id, status="active", context_usage=None, current_task=None)` — periodic keep-alive. Roster queries infer liveness from presence of a recent heartbeat.
- `cogos_session_end(session_id, reason="user-quit", handoff_id=None)` — graceful shutdown marker. Optional `handoff_id` links the chain when a session ends via handoff rather than quit.
- `cogos_sessions_list(active_within_seconds=600)` — aggregated roster from the last 500 events on `bus_sessions`; flags each entry's `active` status based on freshness.

**New tools (handoffs):**
- `cogos_handoff_offer(from_session, task, bootstrap_prompt, to_session=None, reason="explicit", ttl_seconds=3600, bus_context_refs=None, memory_refs=None)` — publishes a handoff offer to `bus_handoffs`. The `bootstrap_prompt` is the load-bearing field — given to the successor as its first user turn.
- `cogos_handoff_list_open(for_session=None, include_claimed=False)` — lists open handoffs, optionally filtered to ones targeting a specific session (plus open-to-anyone).
- `cogos_handoff_claim(handoff_id, claiming_session)` — atomic claim; first-wins-by-seq. Returns the full offer payload so the claiming session has `bootstrap_prompt`/`task`/`memory_refs` without a second round-trip.
- `cogos_handoff_complete(handoff_id, completing_session, outcome="done", notes=None, next_handoff_id=None)` — marks a handoff as finished. `outcome="reoffered"` + `next_handoff_id` links recursive relays.

**Spec:** `docs/HANDOFF_PROTOCOL.md` defines session identity format, well-known bus names (`bus_sessions` / `bus_handoffs` / `bus_broadcast`), lifecycle event shapes, claim semantics, and the sovereignty framing (CogOS domain vs host-OS domain).

All tools inherit the never-raise contract — structured `{"success": False, "error": ..., ...}` on failure. Tests: 23 payload-shape tests in `tests/test_session_handoff.py`, mock-based, no live kernel required.

## 0.4.0 — 2026-04-21

HTTP streamable transport for multi-client substrate coordination. Transport selection gated on `MCP_TRANSPORT` env var — `stdio` remains default; set `MCP_TRANSPORT=http` to enable HTTP. Host/port/path configurable via `MCP_HTTP_HOST` / `MCP_HTTP_PORT` / `MCP_HTTP_PATH`.

**Why:** stdio transport constrains cog-sandbox-mcp to one client per process. Multi-session MCP substrate coordination — multiple Claude Code sessions sharing one centralized bridge as peers — requires HTTP. This is the foundation for the 0.4.1 session/handoff layer landing alongside it.

**Tests:** 11 new tests in `tests/test_transport.py` cover transport selection end-to-end without socket binding (asserts `run()` dispatches to the correct transport handler given env var state).

**No breaking changes** to the v0.3.x tool surface. All previously-passing tests continue to pass.

## 0.3.4 — 2026-04-20

Fourth bridge tool: `cogos_resolve`. Read-only access to `cog://` URIs — ADRs, memory entries, any kernel-addressed resource. Handles the kernel's base64-over-JSON wire encoding with a decode contract that gracefully falls through on binary / malformed content.

**New tool:**
- `cogos_resolve(uri, decode=True)` — GETs `/resolve?uri=<url-encoded>` (no `/v1/` prefix — the kernel's resolve endpoint is at root). URI is URL-quoted via `urllib.parse.urlencode`, so cog:// URIs containing `&`, `?`, or spaces serialize safely. Returns the kernel's JSON with `content` replaced by the decoded text on success. On base64 or UTF-8 decode failure, falls back to `content=raw_content=<original base64>` plus a `decode_error` note — content is always present, never raising into the agent's loop. With `decode=False`, skips the decode entirely for binary payloads.
- Kernel 500 errors (bogus URI, not found) parse the kernel's `{"error": {"message", "type"}}` payload and surface the message through the standard `{"success": False, "error": ..., "uri": ...}` structure.

**Deferred to v0.3.5:** `cogos_mutate` (write access to cog:// via `POST /mutate`). Write exposure from a sandboxed agent to the cog:// graph has real blast-radius implications worth naming explicitly; v0.3.4 stays read-only as the conservative default.

**Tests:** 44/44 passing (was 38/38 on v0.3.3). New coverage:
- Base64 → UTF-8 decode round-trip against a mock resolve endpoint.
- `decode=False` path preserves the base64 in both `content` and `raw_content`.
- Non-UTF-8 binary payload falls back cleanly with a `decode_error` note.
- HTTP 500 from the kernel surfaces the error message through the structured-error shape with `uri` echoed back.
- URL-quoting: a URI containing `&`, `?`, and a space serializes as exactly one `uri=<percent-encoded>` parameter with no bare `&` or `?` in the query string.
- Registration visibility: present when bridge enabled, absent when disabled.

**Cross-LAN smoke:** [scripts/smoke_bridge.py](scripts/smoke_bridge.py) extended to resolve `cog://adr/085` against the laptop kernel and assert the content starts with markdown frontmatter (`---`).

**No breaking changes** to the v0.3.3 tool surface. All previously-passing tests continue to pass.

## 0.3.3 — 2026-04-20

Third bridge tool: `cogos_events_read`. Closes the emit/read roundtrip — agents can now verify their own emits, tail a bus, or filter by type/sender to focus on specific signals. Same conditional-registration pattern, same never-raise contract.

**New tool:**
- `cogos_events_read(bus_id, after_seq=None, event_type=None, from_sender=None, limit=100)` — GETs `/v1/bus/{bus_id}/events` with query-string filters. Wraps the kernel's event array as `{"bus_id", "events", "count"}`. On failure (including 404 if the bus does not exist), returns a structured error. Does NOT auto-create the bus — explicitly documented to distinguish from `cogos_emit`.

**New helper:**
- `_http_get_any_with_params(path, params)` — GET + JSON decode, returning `Any` (the kernel's bus-events endpoint returns a bare list). `None`/`""` params are filtered so callers can pass Optional filters through directly.

**Tests:** 38/38 passing (was 34/34 on v0.3.2). New coverage:
- `cogos_events_read` path + query-string serialization against a threaded mock HTTP server, including all four filter params (`after`, `type`, `from`, `limit`).
- Registration visibility: present when bridge enabled, absent when disabled.
- Structured-error contract against a closed port.
- **Roundtrip integration test**: a single mock kernel accepts POST `/v1/bus/send` (appends with monotonic seq) and GET `/v1/bus/{id}/events` (returns the list); emit → read sees the event back with the right seq/type/from/payload.

**Cross-LAN smoke:** [scripts/smoke_bridge.py](scripts/smoke_bridge.py) extended — after emit, it now reads the same bus and asserts the emit's `seq` appears in the results.

**No breaking changes** to the v0.3.2 tool surface. All previously-passing tests continue to pass.

## 0.3.2 — 2026-04-20

Second bridge tool: `cogos_emit`. Same conditional-registration pattern as `cogos_status` — only appears when `COG_OS_BASE_URL` is set — and the same never-raise contract on failure, so agents can call it as a safe side-effect without needing exception handling.

**New tool:**
- `cogos_emit(bus_id, message, from_sender="cog-sandbox", event_type="message")` — POSTs to the kernel's `/v1/bus/send`. On success, returns the kernel's JSON response verbatim. On failure (unreachable host, HTTP error, any exception), returns `{"success": False, "error": ..., "bus_id": ...}` without raising.

**Tests:** 34/34 passing (was 31/31 on v0.3.1). New coverage:
- `cogos_emit` posts the correct path + payload shape against a threaded mock HTTP server and surfaces the kernel's response verbatim.
- Unreachable-host handling returns a structured error rather than raising.
- Registration visibility: `cogos_emit` present when bridge is enabled, absent when disabled.

**Cross-LAN smoke:** [scripts/smoke_bridge.py](scripts/smoke_bridge.py) extended to exercise `cogos_emit` end-to-end against a remote kernel after the `cogos_status` probe.

**No breaking changes** to the v0.3.1 tool surface. All previously-passing tests continue to pass.

## 0.3.1 — 2026-04-20

Iteration on v0.3: description pass to address a drift behavior surfaced by the eval harness, plus the scaffold for v0.4's `cogos_*` bridge layer so future work builds on the mediator/kernel layering.

**Description pass (addresses drift gap):**
- `list_authorized_paths`, `grant_path_access`, `revoke_path_access` descriptions rewritten with prescriptive "CALL THIS WHEN" language and explicit anti-substitution directives.
- Eval improvement: 8/10 passing, up from 6/8. New drift-probe cases (`drift_probe_with_hint`, `authorized_no_spurious_grant`) pass; the remaining bare-prompt failure turns out to be ambiguity handling, not drift — a system-prompt-level issue rather than a tool-description one.

**v0.4 bridge scaffold:**
- New module `cog_sandbox_mcp/tools/cogos_bridge.py` with HTTP helpers (`_http_get_json`, `_http_post_json`) and the first tool, `cogos_status()`, which probes a reachable Cog OS kernel's `/health` endpoint.
- Conditional registration: bridge tools appear in the MCP tool list only when `COG_OS_BASE_URL` is set at server startup. The sandbox stays self-sufficient when CogOS isn't reachable — mediator stands alone.
- Pattern established for follow-on bridges (`cogos_emit`, `cogos_events_read`, `cogos_fleet_spawn`, etc.) — each is a thin wrapper around the HTTP helpers plus a `mcp.tool(...)` registration.

**Eval harness:**
- New cases under `evals/cases/`: `09_drift_probe_with_hint.yaml`, `10_authorized_no_spurious_grant.yaml`; `04_unauthorized_triggers_grant.yaml` rewritten to use `skills` as a reliably unauthorized target.

**Tests:** 31/31 passing (was 26/26 on v0.3). New coverage for bridge gating (env var on/off), unreachable-host handling in `cogos_status`, and registration visibility (bridge tools present when enabled, absent when disabled).

**No breaking changes** to the v0.3 tool surface. All previously-passing tests continue to pass.

## 0.3.0 — 2026-04-20

Initial topologically-isolated release.

- Structured filesystem tools only (no `bash`): `read`, `write`, `edit`, `glob`, `grep`, `list_directory`, `tree`, `hash_file`, `find_duplicates`, `consolidate_duplicates`.
- Workspace-level authorization: `list_authorized_paths`, `grant_path_access`, `revoke_path_access`.
- Paths resolved through a virtual "workspace name / rest" scheme; unauthorized paths mask as `FileNotFoundError` (topologically invisible).
- `COG_SANDBOX_INITIAL_AUTH` env var required at container startup.
- Container: rootless, `--network=none`, single bind mount from host workspaces root to `/workspace` inside.
- Eval harness under `evals/` using LM Studio's `/api/v1/chat` with plugin integration, structured YAML cases, rubric scoring, optional filesystem observation via `watchdog`.
