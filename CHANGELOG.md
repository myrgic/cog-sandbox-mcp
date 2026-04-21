# Changelog

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
