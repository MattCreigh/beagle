# Concept Spec: Ephemeral JIT Redis Coordination DB (`beagle-coord`)

**Status:** Concept / proposal — not yet implemented.
**Date:** 2026-08-21
**Author:** goose session (idea from operator)
**Scope:** A Beagle plugin (`beagle-coord`) that JIT-spawns a short-lived, in-memory
Redis instance per working directory, collects checkpoint + agent-activity state from
every concurrent session/agent that touches that directory, and — when the *last*
agent leaves — persists a snapshot of the database into a durable append log.

---

## 1. Problem

Beagle's coordination state today is scattered and file-based:

| Concern | Current home | Problem |
|---|---|---|
| Session progress | `.beagle/progress.xml` (per-dir) | One writer; a second goose reading mid-write can misread it (the `RUN_ORDER.xml` C5 correction). |
| Run tracking | `~/.beagle/tracking.db` (SQLite) | Durable, but machine-wide only, no live view. |
| Lifecycle checkpoint | `.beagle/checkpoints/restart_checkpoint.json` | Process-local restart recovery, not inter-session. |
| Task queue | `~/.local/share/openclaw/tasks.db` (SQLite) | Task lifecycle, not live agent/checkpoint coordination. |
| Session archive | `~/.cache/beagle/memory/sessions/*.json` | Durable memory, not live coordination. |
| Goose sessions | `~/.local/share/goose/sessions/sessions.db` | Records *that* sessions exist; does not coordinate them. |

None of these give a **live**, shared, low-latency registry that answers the
questions a concurrent working group needs:

- Who else is actively working in this directory right now?
- Which files are being edited by another agent at this moment?
- What is the current shared checkpoint / phase consensus?
- Who holds a lock on a file or a plan?

Today the answer is "hope nobody else is running" — `RUN_ORDER.xml` literally
contains a constraint `one-plan-at-a-time`: *"Never start a stage while another
goose run is live."* That constraint exists because there was **no mechanism**
to see that another run was live. This plugin is that mechanism.

---

## 2. Proposed model

A **JIT-spawned, ephemeral, in-memory Redis** instance anchored to a working
directory. It is created on demand by the first agent that becomes active in a
directory, it is **sticky** (later agents join the existing instance), and it is
torn down by the **last** agent to leave, which flushes a snapshot into a
durable append-only log.

```text
  Agent A (first in dir)          Agent B (joins)             Agent C (joins)
        │                              │                            │
        │  spawn JIT redis             │                            │
        │  (data-dir = tmpfs)          │                            │
        ▼                              ▼                            ▼
   ┌──────────────────────────────────────────────────────────────┐
   │   shared in-memory registry   (one instance per working dir)  │
   │                                                                │
   │   • agent roster  (who is live, liveness TTL)                  │
   │   • checkpoints  (latest phase / completed nodes consensus)    │
   │   • file locks   (paths currently being edited)                │
   │   • plan lock    (one-plan-at-a-time becomes enforced)         │
   │   • event log    (append-only coordination history)            │
   └───────────────┬──────────────────────────────────────────────┘
                   │   last agent disconnects
                   ▼
        snapshot + flush → durable log file
        (~/.beagle/coord-logs/<dir-hash>/<ts>.log.json)
```

### 2.1 JIT spawn

The plugin does **not** run as a background daemon. It spawns `redis-server`
on first use, with a **random unix socket or high port**, an `--appendonly no`
config (memory-only), and a data directory on `tmpfs`/`get_runtime_dir()`.
A random credential + socket avoids clashing with any existing Redis and any
pre-existing instance. If a live instance already exists for the directory, the
agent **does not spawn a second one** — it just connects (this is the *sticky*
join).

### 2.2 Liveness and the last-out teardown

Each agent holds a lease key in the registry: `coord:agents:<agent_id>` with a
short TTL (default ~30 s), renewed by a background heartbeat. A membership set
`coord:agents` tracks every live agent id.

- An agent **joins** by adding itself to the set and writing its lease.
- An agent **leaves** by removing itself from the set (or letting its lease TTL
  out when it crashes).
- **Last-out teardown**: when an agent removes itself and observes the set is
  now empty (atomicity via a small Lua/`WATCH`/`MULTI` script), it is the *last
  agent*. It:
  1. `SAVE` / `BGSAVE` a snapshot, or simply reads all keys,
  2. serialises the full registry to JSON,
  3. appends it to the durable log file,
  4. `SHUTDOWN` the redis process and removes the instance marker.

If an agent **crashes** (no graceful leave), the TTL-based lease expiry means
the membership set eventually empties, and the next agent to observe an empty
set and a still-alive instance can elect itself to run the flush+shutdown. This
makes teardown robust to the last agent dying without disconnecting cleanly.

---

## 3. What it stores

```
Key                                        Type        TTL      Purpose
─────────────────────────────────────────────────────────────────────────────
coord:agents:<agent_id>                    string      30s      liveness lease
coord:agents                                set         -       live membership
coord:agent:<agent_id>:info                hash        -       cwd, model, pid, start_ts
coord:checkpoint:<agent_id>                hash        -       latest phase, completed_nodes
coord:plan                                  hash        -       active plan lock (owner, ts)
coord:file:<path>                          hash        -       file edit lock (owner, mode)
coord:events                                stream      -       append-only coordination log
```

All TTLs make this a **fading memory**: an instance that a whole team abandoned
causes every entry to expire within seconds of the last heartbeat, so the
in-memory registry never lingers after the group leaves.

---

## 4. Behaviors

### 4.1 Who is here

`coord:agents` + each lease answers "which agents/sessions are live in this dir".

### 4.2 Checkpoints

Each agent writes its own checkpoint hash on every `beagle_progress_update`.
The current `progress.xml` remains the per-agent durable record; the registry
provides the **aggregate** — what the group has collectively completed.

### 4.3 File / plan locks

The `one-plan-at-a-time` rule becomes an **enforced** advisory lock:
`coord:plan` held by the first agent that starts a plan, others wait or fail
fast instead of silently editing the same tree. Per-file edit locks let an agent
avoid the RUN-010 mid-write misreading of shared files.

### 4.4 Lead election

When the first agent spawns the instance it becomes the "leader" (holds
`coord:plan`). On its departure, the next live agent can take leadership.

---

## 5. Durable log format

One append-only JSONL file per directory under `~/.beagle/coord/<dir-hash>/`.
Each **teardown** appends one line — a snapshot of the final registry:

```jsonl
{"ts": "2026-08-21T15:00:00Z", "dir": "beagle", "reason": "last-agent-left",
 "agents": ["a1","a2"], "checkpoints": {...}, "plan": {...}, "events": [...]}
```

This is the "keep it for future reference" requirement: the ephemeral DB is
thrown away, but the compressed snapshot survives as an audit trail and as a
seed for the next instance.

---

## 6. Scope question

Two candidate scopes; proposal is **per-directory** (matches "agents working in
the same dir"):

- **Per-directory** (default): one instance per working-dir. Bounded, no
  cross-project contention.
- **Machine-wide** (optional flag): one instance per machine, namespace keys by
  directory. Enable with `--machine-wide`. Default remains per-dir.

---

## 7. Plugin shape

The user asked for "a beagle plugin". This plugin lives outside the Beagle
wheel as its own package `beagle-coord`, registering:

- `beagle.runtimes` — an entry point (existing plugin group) OR
- a small **MCP utility server** `mcp_coord_server` that exposes
  `coord_status`, `coord_join`, `coord_leave`, `coord_lock_file`,
  `coord_unlock_file`, `coord_claim_plan`, `coord_release_plan`,
  `coord_checkpoint`, `coord_events`.

The MCP surface is the natural integration: goose already exposes MCP servers
via `.mcp.json` (`beagle-rag`, `beagle-openclaw`, `beagle-utility`). `beagle-coord`
becomes `beagle-coord` in `.mcp.json` and any agent (goose, OpenClaw, a future
`pi`) connects to it the same way.

Packaging: separate repo + wheel (`beagle-coord`), like the `skylon_plugin_*`
pattern. Redis is already a runtime dependency only if `redis-server` is on
PATH; the plugin ships a pure-python `redis` client and looks up the binary via
`resolve_executable("redis-server")`. If no redis binary, the plugin degrades to
no-op/off (does not break the session).

---

## 8. Risks & open questions

| Item | Open question |
|---|---|
| Atomic last-out teardown | Race where two agents both believe they are last. Needs `WATCH`/Lua balance. |
| TTL vs graceful leave | A crashed last agent leaves a few seconds of "orphan" instance before TTL cleans it. Acceptable. |
| Port/socket conflict | Random unix socket path removes conflict risk. |
| Security | Registry may hold file paths & agent models — no secrets. Log file should be `0600`. |
| Scope default | Confirm per-dir is the right default; machine-wide as opt-in. |
| Enforce vs advisory plan lock | Advisory first; enforcement (refuse the second plan) later. |

---

## 9. Acceptance criteria (future)

1. First agent in a dir spawns exactly one redis instance; second agent joins it.
2. Both agents' liveness leases appear in `coord:agents`.
3. When the last agent leaves (graceful) or its lease TTLs out (crash), the
   instance flushes a snapshot JSONL to the durable log and shuts down.
4. `one-plan-at-a-time` is enforced via `coord:plan` — a second agent that tries
   to claim the plan while it is held gets an explicit "held by X" failure.
5. Running two goose sessions in one dir produces coordinated locks and a
   surviving audit log, not a silent overwrite.

---

## 10. Verification

No code yet. This spec is a proposal; the next step is a spike that spawns
`redis-server`, runs two concurrent clients, exercises the join/sticky/last-out
lifecycle, and confirms the durable-log flush. Only then does `beagle-coord`
move from this concept doc into a plugin implementation plan.
