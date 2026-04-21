# Handoff Protocol

Spec for session identity, presence, and handoff events over the CogOS bus. Implemented by the `cogos_session_*` and `cogos_handoff_*` tool families in `cog-sandbox-mcp`.

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
- `slowbro-laptop-cog-manager`
- `slowbro-laptop-cogos-refactor-001`
- `slowbro-desktop-loro-eval-01JQTZ...` (auto-generated ULID)

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
    "session_id": "slowbro-laptop-cog-manager",
    "workspace": "/Users/slowbro/workspaces/cog",
    "hostname": "slowbro-laptop",
    "started_at": "2026-04-21T10:00:00Z",
    "model": "claude-opus-4-6",
    "role": "manager",
    "task": "coordinating wave 2 of cross-session MCP rollout"
  }
}
```

### `session.heartbeat`

Periodic keep-alive. Sessions emit every N minutes (default 5). Absence of heartbeat for 2× interval → session presumed inactive.

```json
{
  "type": "session.heartbeat",
  "payload": {
    "session_id": "slowbro-laptop-cog-manager",
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
    "session_id": "slowbro-laptop-cog-manager",
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
    "from_session": "slowbro-laptop-cog-manager",
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
      {"bus_id": "bus_chat_slowbro-laptop-cog-manager", "after_seq": 104}
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
    "claiming_session": "slowbro-laptop-cog-relay-2",
    "previous_session": "slowbro-laptop-cog-manager",
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
    "completing_session": "slowbro-laptop-cog-relay-2",
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

These tools (to be added in Wave 1) implement the protocol:

| Tool | Event emitted / read | Notes |
|------|----------------------|-------|
| `cogos_session_register(session_id, workspace, role, task)` | writes `session.register` | Idempotent; re-registering updates status |
| `cogos_session_heartbeat(session_id, status, context_usage, current_task)` | writes `session.heartbeat` | Call every ~5 min |
| `cogos_sessions_list(active_within_seconds)` | reads `bus_sessions` + aggregates | Returns active session roster |
| `cogos_handoff_offer(task, bootstrap_prompt, refs, ...)` | writes `handoff.offer` | Returns `handoff_id` |
| `cogos_handoff_list_open(for_session)` | reads `bus_handoffs` + filters | Returns offers without claims |
| `cogos_handoff_claim(handoff_id, claiming_session)` | writes `handoff.claim`, returns offer | First-wins |
| `cogos_handoff_complete(handoff_id, outcome, notes)` | writes `handoff.complete` | |

All tools thread `session_id` through as the `from_sender` of the underlying `cogos_emit` call, so every substrate action is attributable.

## Non-goals for v1

- **No central coordinator.** All state lives in the bus; no daemon tracks it. Consistent with the distributed-by-design principle.
- **No strong claim enforcement.** First-wins-by-seq is advisory; a misbehaving client could double-claim. For v1 we trust the participants. (A future version may require role-based auth for `handoff.claim` against a `workspace-owner` or `node-admin` role.)
- **No bus federation.** The schema is cross-node-ready but v0.1 operates on a single node's bus. Federation (laptop bus ↔ desktop bus sharing `bus_handoffs`) is deferred — it's a bus-layer concern, not a protocol-layer concern.
- **No cross-node handoff auth.** Handoffs within a node rely on the host OS + workspace trust model. Cross-node handoff will require cryptographic workspace identity; that layer is not yet built.
- **No automatic handoff triggering.** Sessions decide when to offer; no kernel-side "context is 90% full, auto-handoff" logic yet.
- **No memory sync.** Sessions are responsible for writing state to CogDocs themselves via `cog memory write` and referencing in `memory_refs`. CogDoc replication across nodes is a future CogOS-layer concern, not a handoff-protocol one.

**Not a non-goal:** identity portability. `workspace-slug` is already stable across nodes by design — sessions from the "same" workspace on different machines are recognizable as the same cognitive user, even though their runtime hosts are sovereign over separate Unix processes. This is a founding primitive, not a v2 addition.

## Versioning

This protocol is v0.1. Breaking changes require a new `protocol_version` field in event payloads. For now, absence of the field implies v0.1.
