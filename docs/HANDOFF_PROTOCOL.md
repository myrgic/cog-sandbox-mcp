# Handoff Protocol

_Version 0.2.1 — participant_type discriminant (2026-04-22)._

Spec for session identity, presence, and handoff events over the CogOS bus. Implemented in two complementary MCP surfaces (see §Implementation: two MCP surfaces):

- `cogos_session_*` / `cogos_handoff_*` tools in `cog-sandbox-mcp` (Python, the current MCP config) — thin shims over the kernel routes below.
- `cog_register_session` / `cog_offer_handoff` / etc. native kernel tools served directly at the kernel's `/mcp` endpoint (Go, for future native clients).

Both surfaces read and write the same kernel-native session & handoff registries; the bus (`bus_sessions` and `bus_handoffs`) is still the append-only ground truth.

## Sovereignty context

This protocol lives entirely within the **CogOS domain** — the distributed cognitive substrate, which is sovereign over sessions, buses, handoffs, and cognitive identity. It assumes the **host OS domain** (per-node filesystem, processes, users, network) retains sovereignty over its own concerns. Authorization is orthogonal: host ACLs gate filesystem/process operations; this protocol's authorization (role grants, rate limits, claim semantics) gates CogOS-domain operations. Neither second-guesses the other. Full frame: `cog://mem/semantic/architecture/membrane`.

The protocol is **distributed by design**. Handoffs can in principle cross nodes (session on laptop offers → session on desktop claims) — single-node operation is a degenerate case of the general model, not the canonical case. V0.1 implementations can be single-node, but the schema must not bake in single-node assumptions.

## Motivation

A single Claude Code session has a bounded context window. When it approaches the limit, the session degrades (auto-compaction, repetition, lost context). The substrate-mediated handoff converts that hard ceiling into a relay: Session A writes its state and the "next thing to do" into the shared bus; Session B (fresh context) reads that state and resumes. Applied recursively, work continues indefinitely across fresh sessions — and, when the bus federates, across fresh nodes.

Cross-session visibility and communication are necessary scaffolding; **handoff is the unlock**.

## Session identity

Every session that participates in the substrate has a **session_id** — a stable string unique within the bus's lifetime.

**Format:** `<hostname>-<workspace-slug>-<session-slug>` — three components that map cleanly onto the OS/CogOS boundary:

| Component | Domain | Meaning |
|---|---|---|
| `hostname` | Host OS | Which node (laptop, desktop, server). Host-sovereign identifier. |
| `workspace-slug` | CogOS | Which workspace-as-user (portable across nodes). Cognitive identity. |
| `session-slug` | CogOS | Which session (ephemeral process identity within workspace+node). |

Examples:
- `dev-laptop-cog-manager`
- `dev-laptop-cogos-refactor-001`
- `dev-desktop-loro-eval-01JQTZ...` (auto-generated ULID)

Rules:
- ASCII, lowercase, `[a-z0-9-]` only. No underscores (keep separator unambiguous).
- Stable for the life of the session. Not reused after the session ends.
- Used as the `from_sender` field in `cogos_emit` calls so all emissions are attributable.
- The `workspace-slug` is the **portable** part: a workspace on laptop and the same workspace on desktop both use the same slug, letting CogOS recognize them as one cognitive user. The `hostname` differentiates runtime sovereign.

## Well-known buses

The substrate reserves three bus names. Tools enforce or default to these; users can still emit to other buses for domain-specific channels.

| Bus | Purpose | Retention |
|-----|---------|-----------|
| `bus_sessions` | Presence, heartbeat, status transitions | last 24h, then archive |
| `bus_handoffs` | Handoff offer / claim / complete events | last 72h, then archive |
| `bus_broadcast` | Cross-session announcements (low frequency) | last 7d |

Per-session conversational buses (`bus_chat_<session_id>`) are **unchanged** — still created by `cog infer --session <slug>` and used for per-session message logs. Handoff references can point at these via `bus_context_refs`.

## Session lifecycle events

All events on `bus_sessions` use `event_type` values from this set:

### `session.register`

First event a session emits on connection. Announces presence.

```json
{
  "type": "session.register",
  "payload": {
    "session_id": "dev-laptop-cog-manager",
    "workspace": "${COGOS_WORKSPACE}",
    "hostname": "dev-laptop",
    "started_at": "2026-04-21T10:00:00Z",
    "model": "claude-opus-4-6",
    "role": "manager",
    "task": "coordinating wave 2 of cross-session MCP rollout",
    "participant_type": "agent"
  }
}
```

`participant_type` is an optional discriminant, one of `"agent" | "user" | "provider"`. It defaults to `"agent"` on the wire (and is omitted from the payload by the bridge when the default applies, preserving byte-compat with pre-v0.2.1 emissions). Values:

- `"agent"` — an autonomous session (Claude Code, sub-agent, worker). The canonical case; this is what every session was before v0.2.1.
- `"user"` — a human-driven session (a person at a REPL or CLI emitting as themselves).
- `"provider"` — a **channel provider** per the [channel-provider RFC](cog://mem/semantic/designs/channel-provider-interface): mod3 (audio), discord (text-platform), repl / watch-TUI (terminal), gateway (OpenAI-compat). Providers use the same `cogos_session_register` primitive agents use and typically carry a `metadata` sub-object with `provider_id` and `kinds` so consumers can filter:

```json
{
  "type": "session.register",
  "payload": {
    "session_id": "dev-laptop-mod3-provider",
    "workspace": "${COGOS_WORKSPACE}/../mod3",
    "role": "audio-provider",
    "task": "mediating voice-room-primary",
    "participant_type": "provider",
    "metadata": {
      "provider_id": "mod3",
      "kinds": ["audio"]
    }
  }
}
```

Roster/presence consumers that do not care about the distinction can keep treating every row uniformly; consumers that do (e.g. "do not offer handoffs to providers") filter on `participant_type`.

### `session.heartbeat`

Periodic keep-alive. Sessions emit every N minutes (default 5). Absence of heartbeat for 2× interval → session presumed inactive.

```json
{
  "type": "session.heartbeat",
  "payload": {
    "session_id": "dev-laptop-cog-manager",
    "status": "active",
    "context_usage": 0.62,
    "current_task": "drafting handoff protocol spec",
    "last_tool_use_at": "2026-04-21T10:17:33Z"
  }
}
```

`status` is one of: `active`, `idle`, `paused`, `ending`.

### `session.end`

Final event before a session closes cleanly. Optional but recommended.

```json
{
  "type": "session.end",
  "payload": {
    "session_id": "dev-laptop-cog-manager",
    "ended_at": "2026-04-21T12:04:00Z",
    "reason": "task-complete|context-exhausted|user-quit|handed-off",
    "handoff_id": "ho-01JQ..."
  }
}
```

## Handoff events

All events on `bus_handoffs` use `event_type` values from this set.

### `handoff.offer`

Session A writes this when it wants to hand off — either because context is exhausted, the task paused, or it's decomposing into a fresh-context worker.

```json
{
  "type": "handoff.offer",
  "payload": {
    "handoff_id": "ho-01JQTZ7P8X9M0A7K5ZQXJ3BWVF",
    "from_session": "dev-laptop-cog-manager",
    "to_session": null,
    "reason": "context-exhaustion",
    "created_at": "2026-04-21T11:45:00Z",
    "ttl_seconds": 3600,
    "task": {
      "title": "Refactor context engine EA/EFM split",
      "goal": "Extract inference-independent context construction from serve.go into a reusable package.",
      "progress_summary": "Wave 1 complete (3/3 tasks landed). Wave 2 pending: smoke tests + launchd plist.",
      "files_touched": [
        "cogos/serve_context_build.go",
        "cogos/serve_context_build_test.go"
      ],
      "files_pending": [
        "cogos/serve.go  // route registration needs verification",
        "~/.config/launchd/com.cogos.kernel.plist"
      ],
      "decisions_made": [
        {"decision": "Accept 200 OR 503 from /health", "rationale": "503 is the tamper signal per protocol.go:200-203"}
      ],
      "open_questions": [
        "Do we need per-session memory endpoints, or is bus sufficient for MVP?"
      ],
      "next_steps": [
        "1. Run `go build ./...` in cogos repo; confirm clean",
        "2. Run integration tests: `go test -tags integration -count=1`",
        "3. If green, proceed to F1 (launchd plist)"
      ],
      "verification_gates": [
        "go build ./...",
        "go test ./...",
        "go test -tags integration ./..."
      ]
    },
    "bootstrap_prompt": "You are picking up Session A's work on the EA/EFM context-engine split. Wave 1 landed cleanly; you are starting Wave 2. Read the files listed in files_touched for full context. Critical invariants: /health may return 200 or 503 (both are valid); the context engine must remain inference-independent. First action: run `go build ./...` and report. If clean, proceed to the integration tests per next_steps.",
    "bus_context_refs": [
      {"bus_id": "bus_chat_dev-laptop-cog-manager", "after_seq": 104}
    ],
    "memory_refs": [
      "cog://mem/working/handoff-state-01JQTZ7P8X.cog.md"
    ]
  }
}
```

**Design notes:**
- `to_session: null` = **open offer**, any fresh session can claim. If set to a specific `session_id`, only that session should claim (still advisory — bus enforces nothing).
- `bootstrap_prompt` is the most important field. It's the actual text that the successor session will be given as its first user turn. Write it like you'd write a brief for a smart colleague who just walked in.
- `bus_context_refs` lets the successor read the outgoing session's full conversation via `cogos_events_read`.
- `memory_refs` point at CogDocs for state too large to inline in the handoff (long logs, generated artifacts).
- `ttl_seconds` indicates when the handoff becomes stale. After expiry, a fresh session should not claim; a human should be notified.

### `handoff.claim`

Session B posts this when it begins work on an open offer. Establishes ownership.

```json
{
  "type": "handoff.claim",
  "payload": {
    "handoff_id": "ho-01JQTZ7P8X9M0A7K5ZQXJ3BWVF",
    "claiming_session": "dev-laptop-cog-relay-2",
    "previous_session": "dev-laptop-cog-manager",
    "claimed_at": "2026-04-21T11:52:00Z"
  }
}
```

Claim is **first-wins by seq**: the lowest-seq claim event for a given handoff_id is the valid claimant. Other would-be claimants see the earlier claim when they read the bus and should abort.

### `handoff.complete`

Session B emits this when the handed-off work is done (or when B itself re-offers).

```json
{
  "type": "handoff.complete",
  "payload": {
    "handoff_id": "ho-01JQTZ7P8X9M0A7K5ZQXJ3BWVF",
    "completing_session": "dev-laptop-cog-relay-2",
    "outcome": "done",
    "next_handoff_id": null,
    "completed_at": "2026-04-21T13:10:00Z",
    "notes": "Wave 2 landed, all gates green. No further handoff needed."
  }
}
```

`outcome` ∈ `{done, reoffered, abandoned}`. If `reoffered`, `next_handoff_id` points at the new offer.

## Canonical flow

```
Session A (near context limit)
    │
    ├─ emit session.heartbeat { status: "ending" }
    │
    ├─ write memory: cog://mem/working/handoff-state-<id>.cog.md
    │      (full dump: conversation summary, artifacts, decisions)
    │
    ├─ emit handoff.offer { bootstrap_prompt, task, refs: ... }
    │
    └─ emit session.end { reason: "handed-off", handoff_id: ... }

User opens fresh Claude Code session (Session B)
    │
    ├─ [via skill or manual] cogos_handoff_list_open()
    │      → returns list of open offers
    │
    ├─ cogos_handoff_claim(handoff_id)
    │      → emits handoff.claim
    │      → returns the full offer payload
    │
    ├─ Read bootstrap_prompt as first instruction
    │      → read referenced memory docs
    │      → read bus_context_refs if deeper context needed
    │
    ├─ Execute next_steps
    │
    └─ emit handoff.complete { outcome: "done" | "reoffered" }
```

## Tool mapping

Each protocol operation is served by a pair of MCP tools — the Python bridge tool (ergonomic shim, current MCP config) and the kernel-native tool (no Python dependency, future native clients). Both hit the same kernel registries.

| Bridge tool (`cogos_*`) | Kernel tool (`cog_*`) | HTTP route | Event emitted |
|---|---|---|---|
| `cogos_session_register(session_id, workspace, role, task, participant_type?, metadata?)` | `cog_register_session` | `POST /v1/sessions/register` | `session.register` |
| `cogos_session_heartbeat(session_id, status, context_usage, current_task)` | `cog_heartbeat_session` | `POST /v1/sessions/{id}/heartbeat` | `session.heartbeat` |
| `cogos_session_end(session_id, reason, handoff_id?)` | `cog_end_session` | `POST /v1/sessions/{id}/end` | `session.end` |
| `cogos_sessions_list(active_within_seconds, include_ended?)` | `cog_list_sessions` | `GET /v1/sessions/presence` | — (read) |
| `cogos_handoff_offer(from_session, task, bootstrap_prompt, ...)` | `cog_offer_handoff` | `POST /v1/handoffs/offer` | `handoff.offer` |
| `cogos_handoff_list_open(for_session, include_claimed?)` | `cog_list_handoffs` | `GET /v1/handoffs` | — (read) |
| `cogos_handoff_claim(handoff_id, claiming_session)` | `cog_claim_handoff` | `POST /v1/handoffs/{id}/claim` | `handoff.claim` or `handoff.claim_rejected` |
| `cogos_handoff_complete(handoff_id, outcome, notes)` | `cog_complete_handoff` | `POST /v1/handoffs/{id}/complete` | `handoff.complete` |

All tools thread `session_id` through as the `from` field on every bus event, so every substrate action is attributable.

## Implementation: two MCP surfaces

v0.2 moved invariance enforcement from the Python bridge to the kernel. There are now **two MCP surfaces** that consumers can use interchangeably:

1. **Python bridge (`cogos_*`)** — served by `cog-sandbox-mcp` on its configured MCP transport. Current MCP config uses these. They keep the never-raise contract (`{"success": False, "error": ..., "bus_id": ...}` on any HTTP failure) and provide ergonomic payload composition, JSON-wrapping, etc. Internally they call the kernel routes above.
2. **Kernel-native (`cog_*`)** — served by the kernel's own `/mcp` endpoint. No Python dependency. Uses the kernel's own MCP handler pattern, same `cog_*` prefix convention as the rest of the kernel's tools.

Both surfaces share the **same kernel truth**: a single in-memory session registry and handoff registry, each guarded by a mutex and backed by the bus as authoritative ground truth. A `cogos_*` call from the bridge and a `cog_*` call from a native client compete on the same lock and honor the same first-wins semantics. The duplication is in presentation (how the agent invokes it), not in state.

**When to use which:**

- Use `cogos_*` if the agent is already configured against the Python bridge. It's the default for Claude Code today.
- Use `cog_*` if a client wants to skip the Python hop (e.g. a future native desktop app, Wave widget, or direct `cog` CLI invocation).

Both are fully supported; neither is "the future" and the other "legacy." The bus is the portable layer; the MCP tool names are just two doorways to it.

## Atomic claim (closed in v0.2)

The v0.1 bridge did a read-then-emit dance: read `bus_handoffs` to find the offer, then emit a `handoff.claim`. This was racy — two claimants could read the bus simultaneously, see the offer un-claimed, and each emit. The seq ordering in the bus guaranteed a canonical winner, but losers had to detect their loss out-of-band by re-reading the bus. Nothing prevented them from starting work first.

v0.2 encloses the check-and-emit under a kernel mutex (`HandoffRegistry`). The kernel's `ApplyClaim` atomically:

1. Confirms the offer exists (else `404`).
2. Confirms no other claim has landed (else `409 already_claimed`).
3. Confirms TTL hasn't expired (else `409 ttl_expired`).
4. Transitions the in-memory row to `claimed` and records the winner.
5. Emits the `handoff.claim` event to `bus_handoffs`.

Concurrent claim attempts produce exactly one `200` response and N-1 `409`s. No wasted work.

### `handoff.claim_rejected` event

Every rejected claim also emits a `handoff.claim_rejected` event to `bus_handoffs` for audit (added in v0.2). Payload:

```json
{
  "handoff_id": "ho-1732136400000-abc123def456",
  "attempting_session": "dev-laptop-loser-session",
  "reason": "already_claimed",
  "rejected_at": "2026-04-22T12:00:00.123456Z",
  "conflicting_session": "dev-laptop-winner-session"
}
```

`reason` is one of `already_claimed | ttl_expired | offer_not_found | out_of_order`. `conflicting_session` is set when `reason == already_claimed`. Observers filtering by `type: handoff.claim_rejected` can see every racy-claim attempt — useful for diagnosing why a handoff didn't land where expected.

## Non-goals for v1

- **No central coordinator outside the local kernel.** All cross-node state still lives in the federatable bus; the per-kernel in-memory registries are derived views rebuilt from bus replay on restart. ~~No daemon tracks it.~~ Updated v0.2: the local kernel tracks it, but only as a cache — the bus remains authoritative. The distributed-by-design principle is unchanged for multi-node scenarios; single-node invariants are now enforced server-side.
- ~~**No strong claim enforcement.**~~ **Closed in v0.2 for single-node.** Atomic claim is now enforced by the kernel's `HandoffRegistry` mutex. Cross-node claim races still depend on bus-seq ordering until BEP sync ships, so the v0.1 advisory wording still applies to that case. A future version may add role-based auth for `handoff.claim` against a `workspace-owner` or `node-admin` role.
- **No bus federation.** The schema is cross-node-ready but v0.1/v0.2 operate on a single node's bus. Federation (laptop bus ↔ desktop bus sharing `bus_handoffs`) is deferred — it's a bus-layer concern, not a protocol-layer concern.
- **No cross-node handoff auth.** Handoffs within a node rely on the host OS + workspace trust model. Cross-node handoff will require cryptographic workspace identity; that layer is not yet built.
- **No automatic handoff triggering.** Sessions decide when to offer; no kernel-side "context is 90% full, auto-handoff" logic yet.
- **No memory sync.** Sessions are responsible for writing state to CogDocs themselves via `cog memory write` and referencing in `memory_refs`. CogDoc replication across nodes is a future CogOS-layer concern, not a handoff-protocol one.

**Not a non-goal:** identity portability. `workspace-slug` is already stable across nodes by design — sessions from the "same" workspace on different machines are recognizable as the same cognitive user, even though their runtime hosts are sovereign over separate Unix processes. This is a founding primitive, not a v2 addition.

## Versioning

### v0.2.1 (2026-04-22) — current

- `cogos_session_register` and the `session.register` payload now accept an
  optional `participant_type` discriminant (`"agent" | "user" | "provider"`,
  default `"agent"`) and an optional free-form `metadata` dict. Enables the
  channel-provider RFC
  ([cog://mem/semantic/designs/channel-provider-interface](cog://mem/semantic/designs/channel-provider-interface))
  to register providers (mod3, discord, repl, gateway, watch-TUI) through the
  same primitive agents use.
- Fully back-compat: the bridge omits `participant_type` from the wire when
  it equals the `"agent"` default, so the kernel sees the identical payload
  existing callers have been sending. Existing agent callers do not need to
  change anything.
- Kernel-side: if the kernel accepts/ignores unknown keys (the v0.2 contract),
  this is purely a bridge-side change. If the kernel strictly validates the
  payload shape, `participant_type` and `metadata` need to be added to the
  accepted keys on `POST /v1/sessions/register`. Flagged in the task report;
  not changed here.

### v0.2 (2026-04-22)

- Kernel-native hybrid landed. Session & handoff registries now live in the kernel (`internal/engine/sessions.go`, `serve_sessions_mgmt.go`).
- Atomic claim enforced server-side (`409 already_claimed` / `409 ttl_expired` / `404 offer_not_found`).
- `handoff.claim_rejected` event added for observability.
- Two MCP surfaces coexist (`cogos_*` bridge tools + `cog_*` kernel-native tools); same kernel truth, two doorways.
- Bridge tools refactored to shim over kernel routes — MCP signatures unchanged; no breaking change to clients.

### v0.1 (prior)

Bridge-only implementation: all eight tools composed over `POST /v1/bus/send` and `GET /v1/bus/{bus_id}/events`. Session-lifecycle invariants enforced by convention, not by the kernel. Claim was racy.

Breaking changes require a new `protocol_version` field in event payloads. For now, absence of the field implies v0.1/v0.2 (wire-compatible).
