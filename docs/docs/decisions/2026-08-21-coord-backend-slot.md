# Decision: the coordination store backend slot goes at the command surface

**Status:** Accepted — implemented `plans/beagle-coord-backend-slot.xml`
WP-B0 through WP-B7.
**Date:** 2026-08-21 (decided), 2026-08-22 (implemented)
**Scope:** Where the coordination store's replaceable seam sits in
`beagle.beacon`.

---

## Context

Beacon (`docs/decisions/2026-08-21-ephemeral-jit-redis-coordination.md`)
shipped its coordination store hardcoded to `fakeredis` behind a unix
socket. The parent plan's D-12 puts the whole work board — issues,
comments, transitions, labels, links, and their indexes — into this same
store for performance, so the store is load-bearing for the whole system,
not a detail. A later backend (real Redis, a shared-memory store, a
remote service) needs a swap that costs one config edit, with every
existing caller unchanged.

## Decision — D-B1: the seam is the store command surface

The replaceable boundary is a closed Protocol of Redis-shaped commands
(`get`, `set`, `hset`, `sadd`, `zadd`, `lpush`, ...) with Redis semantics
and Redis return types. Callers keep writing `client.hset(...)` against
whichever backend is configured; the backend never sees a call above that
level.

**Why here, not somewhere else:** every caller already written, and
every caller the board plan specifies, is already written against this
surface. A seam at any other layer forces those callers to be rewritten
— the opposite of the "costs one TOML edit" requirement. The surface
itself stayed small and closed by construction (27 commands: the union of
what the shipped code called, MB-3, and what the board plan states it
will need, MB-4) — a closed surface is one a conformance suite can
freeze; an open one cannot be frozen at all.

## Rejected alternatives

**A coordination-operation protocol** (`acquire_lock`, `heartbeat`,
`list_agents`, `append_event`, ...). Semantically cleaner for a
non-Redis backend in isolation, and wrong here specifically: D-12 makes
the store an open-ended record surface (the whole work board lives in
it), so an operation-level protocol would need a new method for every
board feature. Every such addition would break every registered backend
at once. The switch would also force an immediate rewrite of
`apply_intent`, the connector, and the entire board plan to route through
the new operation layer — the same "rewrite every caller" cost the
command-surface seam exists to avoid.

**A RESP server seam** — a backend implements the wire protocol on the
socket. Every backend would then have to implement RESP parsing, and the
socket round trip stays on the critical path regardless of backend.
Transport is 36-68% of a socket round trip (parent plan M-4); removing it
is the main reason to want a different backend at all, and this seam
forbids exactly that win.

**Subclass `redis.Redis` and override the connection.** Ties the
contract to a `redis-py` class hierarchy and its private connection-pool
internals. A shared-memory backend would inherit a large surface it
cannot honour, and an `isinstance(client, redis.Redis)` check anywhere
in the codebase would start passing for an object that talks to no
Redis server at all — a correctness trap for exactly the kind of
non-obvious bug this whole slot exists to prevent.

## Consequences

- `src/beagle/beacon/backend.py` defines `StoreClient` (27 commands,
  `str` in/`str` out) and `BackendDriver` (liveness, stale-state
  clearing, server/client construction) as the frozen contract — see
  `docs/COORD_BACKENDS.md` for the backend-author-facing version of this
  document.
- `fakeredis_unix` (`src/beagle/beacon/backends/fakeredis_unix.py`) is
  the first implementation of the interface, not the only possible one.
  `beagle.beacon.store` (the pre-slot module) no longer exists.
- `tests/test_beacon_backend_conformance.py` parametrises over every
  registered backend and is the executable definition of "entirely
  compatible" — not an opinion, a test result.
