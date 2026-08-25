# Copyright (c) 2026 Matt Creigh. All rights reserved.
"""Beagle Coordination MCP Server (beagle-coord).

Exposes Beacon — the ephemeral, JIT-spawned coordination store (see
plans/beagle-beacon-coordination.xml) — as an MCP tool surface, so any
Beagle agent can see who else is live, hold file locks, announce the plan
it is executing, and open a direct peer channel to another agent.

Transport: stdio ONLY. mcp_security.ALLOWED_TRANSPORTS is
frozenset({"stdio"}) — this server never offers the streamable-http path
mcp_utility_server.py has (WP-7 instruction, recipe fact).

Tools (14, decision D-08 — the surface is frozen; adding one is a scope
change, not an improvement):
  coord_attach          coord_detach          coord_heartbeat
  coord_list_agents     coord_agent_info      coord_whoami
  coord_lock_file       coord_unlock_file     coord_register_plan
  coord_activity
  coord_open_channel    coord_accept_channel  coord_close_channel
  coord_list_channels

Every tool returns JSON. Every tool returns {"status": "disabled"} when
[coord].enabled is false (C-02) — Beacon must never become a hard
requirement of an existing session. The four channel tools additionally
return {"status": "disabled"} when [coord].channels_enabled is false.
"""

from __future__ import annotations

import collections
import contextvars
import json
import logging
import sys
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("Beagle.mcp.coord")

# ── FastMCP setup ────────────────────────────────────────────────────────────
# mcp is a hard dependency of this project (pyproject.toml [project.
# dependencies]: "mcp==1.28.1"), and this is a brand-new file with no prior
# history that would need to tolerate an environment missing mcp, so it
# fails fast and loudly (mcp_openclaw_server.py's own, equally valid pattern
# in this codebase) rather than carrying a full typed stub class whose only
# purpose would be a code path that can never actually execute.

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    logger.error("mcp package required. Install with: pip install mcp")
    sys.exit(1)

mcp = FastMCP(
    "Beagle",
    instructions=(
        "Beacon coordination surface: agent presence, file locks, plan "
        "announcement, and direct peer-to-peer channels."
    ),
)

# ── Rate limiting (mcp_utility_server.py's own pattern) ─────────────────────

_MCP_RATE_LIMIT_WINDOW = 60.0
_MCP_RATE_LIMIT_MAX_CALLS = 120
_mcp_call_timestamps: collections.deque[float] = collections.deque(maxlen=_MCP_RATE_LIMIT_MAX_CALLS)


def _check_mcp_rate_limit() -> None:
    now = time.time()
    while _mcp_call_timestamps and now - _mcp_call_timestamps[0] > _MCP_RATE_LIMIT_WINDOW:
        _mcp_call_timestamps.popleft()
    if len(_mcp_call_timestamps) >= _MCP_RATE_LIMIT_MAX_CALLS:
        msg = f"MCP rate limit exceeded: {_MCP_RATE_LIMIT_MAX_CALLS} calls per {_MCP_RATE_LIMIT_WINDOW}s window"
        raise RuntimeError(msg)
    _mcp_call_timestamps.append(now)


_correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)


def _set_correlation_id() -> str:
    cid = str(uuid.uuid4())
    _correlation_id_var.set(cid)
    return cid


# ── Beacon wiring ────────────────────────────────────────────────────────────

from beagle.beacon import contact  # ruff: ignore[E402]
from beagle.beacon.connector import CoordSession, LockResult  # ruff: ignore[E402]
from beagle.beacon.contact import Unreachable  # ruff: ignore[E402]
from beagle.beacon.keys import BeaconPaths  # ruff: ignore[E402]
from beagle.beacon.records import AgentRecord, stable_colour  # ruff: ignore[E402]
from beagle.beacon.spawn import ensure_running  # ruff: ignore[E402]
from beagle.config.config import get_config  # ruff: ignore[E402]
from beagle.infrastructure.mcp_security import enforce_transport_security  # ruff: ignore[E402]

_DISABLED = json.dumps({"status": "disabled"})

# One session per server process — matches the concept spec's "each session
# gets one MCP client -> server connection that holds the sticky lease".
_session: CoordSession | None = None
_paths: BeaconPaths | None = None


def _coord_enabled() -> bool:
    return bool(get_config().coord.enabled)


def _channels_enabled() -> bool:
    coord = get_config().coord
    return bool(coord.enabled) and bool(coord.channels_enabled)


def _record_to_dict(record: AgentRecord) -> dict[str, Any]:
    d = asdict(record)
    d["files"] = list(record.files)
    return d


def _lock_result_to_dict(result: LockResult) -> dict[str, Any]:
    return {"ok": result.ok, "holder": result.holder}


def _channel_to_dict(channel: Any) -> dict[str, Any] | dict[str, str]:
    if isinstance(channel, Unreachable):
        return {"unreachable": True, "reason": channel.reason}
    return asdict(channel)


# ── Tools: liveness, roster, locks, plan (10 v1 tools) ───────────────────────


@mcp.tool()
async def coord_attach(
    workdir: str,
    model: str = "",
    phase: str = "",
    contactable: bool = True,
    accepts: str = "handoff,query",
    max_msg_bytes: int = 16384,
) -> str:
    """Attach this session to the Beacon for workdir, spawning it if needed.

    Args:
        workdir: The working directory to scope the Beacon instance to.
        model: This agent's model identifier, for the roster display.
        phase: This agent's current phase (free text), for the roster.
        contactable: Whether other agents may open a peer channel to this
            one (D-09). Default True.
        accepts: Comma-separated channel kinds this agent will answer.
        max_msg_bytes: Largest peer message this agent will accept.

    Returns:
        JSON with this session's new agent_id, or {"status": "disabled"}.

    """
    global _session, _paths
    await _run_limited()
    if not _coord_enabled():
        return _DISABLED

    paths = ensure_running(workdir)
    agent_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    record = AgentRecord(
        agent_id=agent_id,
        session_id=agent_id,
        pid=0,
        uid=0,
        host="",
        connected_at=now,
        last_seen=now,
        model=model,
        phase=phase,
        current_plan="",
        current_work="",
        files=(),
        colour=stable_colour(agent_id),
    )
    session = CoordSession(paths, agent_id)
    session.attach(record)
    session._rpc._client.hset(
        f"agent:{agent_id}",
        mapping={
            "contactable": "1" if contactable else "0",
            "accepts": accepts,
            "max_msg_bytes": str(max_msg_bytes),
        },
    )

    _session = session
    _paths = paths
    return json.dumps({"agent_id": agent_id, "dirhash": paths.dirhash})


@mcp.tool()
async def coord_detach() -> str:
    """Leave this Beacon. Idempotent — a call with no attached session no-ops."""
    global _session, _paths
    await _run_limited()
    if not _coord_enabled():
        return _DISABLED
    if _session is not None:
        _session.detach()
        _session.close()
        _session = None
        _paths = None
    return json.dumps({"status": "ok"})


@mcp.tool()
async def coord_heartbeat(
    phase: str = "", current_work: str = "", current_plan: str = "", files: str = ""
) -> str:
    """Refresh this agent's liveness TTL and update its roster fields.

    Args:
        phase: Current phase (free text).
        current_work: Short description of current work.
        current_plan: The plan id this agent is executing, if any.
        files: Comma-separated file paths this agent currently holds.

    """
    await _run_limited()
    if not _coord_enabled():
        return _DISABLED
    if _session is None:
        return json.dumps({"status": "error", "error": "not attached"})
    fields = {
        k: v
        for k, v in {
            "phase": phase,
            "current_work": current_work,
            "current_plan": current_plan,
            "files": files,
        }.items()
        if v
    }
    _session.heartbeat(**fields)
    return json.dumps({"status": "ok"})


@mcp.tool()
async def coord_list_agents() -> str:
    """Return the live roster: every agent whose lease has not expired."""
    await _run_limited()
    if not _coord_enabled():
        return _DISABLED
    if _session is None:
        return json.dumps({"status": "error", "error": "not attached"})
    agents = _session.list_agents()
    return json.dumps({"agents": [_record_to_dict(a) for a in agents]})


@mcp.tool()
async def coord_agent_info(agent_id: str) -> str:
    """Full metadata for one specific agent (not necessarily this session's own)."""
    await _run_limited()
    if not _coord_enabled():
        return _DISABLED
    if _session is None:
        return json.dumps({"status": "error", "error": "not attached"})
    record = _session.agent_info(agent_id)
    if record is None:
        return json.dumps({"status": "error", "error": f"agent {agent_id} has no live lease"})
    return json.dumps(_record_to_dict(record))


@mcp.tool()
async def coord_whoami() -> str:
    """Return this session's own agent metadata."""
    await _run_limited()
    if not _coord_enabled():
        return _DISABLED
    if _session is None:
        return json.dumps({"status": "error", "error": "not attached"})
    record = _session.whoami()
    if record is None:
        return json.dumps({"status": "error", "error": "this agent's own lease has expired"})
    return json.dumps(_record_to_dict(record))


@mcp.tool()
async def coord_lock_file(path: str) -> str:
    """Acquire a file lock. Synchronous (invariant I-2) — never dropped, never racy.

    Args:
        path: A repo-relative file path.

    Returns:
        JSON {"ok": bool, "holder": agent_id} — ok is False when another
        live agent already holds the lock; holder names who.

    """
    await _run_limited()
    if not _coord_enabled():
        return _DISABLED
    if _session is None:
        return json.dumps({"status": "error", "error": "not attached"})
    return json.dumps(_lock_result_to_dict(_session.lock_file(path)))


@mcp.tool()
async def coord_unlock_file(path: str) -> str:
    """Release a file lock this agent holds. A no-op if held by someone else."""
    await _run_limited()
    if not _coord_enabled():
        return _DISABLED
    if _session is None:
        return json.dumps({"status": "error", "error": "not attached"})
    _session.unlock_file(path)
    return json.dumps({"status": "ok"})


@mcp.tool()
async def coord_register_plan(plan_id: str, status: str) -> str:
    """Announce the plan this agent is executing, enforcing one-plan-at-a-time visibility."""
    await _run_limited()
    if not _coord_enabled():
        return _DISABLED
    if _session is None:
        return json.dumps({"status": "error", "error": "not attached"})
    _session.register_plan(plan_id, status)
    return json.dumps({"status": "ok"})


@mcp.tool()
async def coord_activity(limit: int = 50) -> str:
    """Read the bounded activity log."""
    await _run_limited()
    if not _coord_enabled():
        return _DISABLED
    if _session is None:
        return json.dumps({"status": "error", "error": "not attached"})
    return json.dumps({"events": _session.activity(limit)})


# ── Tools: contact directory and peer rendezvous (4 tools, D-09) ────────────


@mcp.tool()
async def coord_open_channel(peer_id: str, kind: str = "handoff") -> str:
    """Open a pairwise channel to peer_id. See beagle.beacon.contact.open_channel.

    Args:
        peer_id: The agent to contact.
        kind: The channel kind — must be one peer_id's `accepts` list names.

    Returns:
        JSON with channel_id/a2b_path/b2a_path/state on success, or
        {"unreachable": true, "reason": ...} — never a stale path (I-5).

    """
    await _run_limited()
    if not _channels_enabled():
        return _DISABLED
    if _session is None:
        return json.dumps({"status": "error", "error": "not attached"})
    return json.dumps(_channel_to_dict(_session.open_channel(peer_id, kind)))


@mcp.tool()
async def coord_accept_channel() -> str:
    """Drain this agent's own pending channel offers (pushed to its out-ring).

    Returns:
        JSON {"offers": [...]}. An empty list is routine — there is no
        separate accept/reject handshake (D-09): once an offer is seen, the
        callee may simply start reading/writing on the given ring paths.

    """
    await _run_limited()
    if not _channels_enabled():
        return _DISABLED
    if _session is None:
        return json.dumps({"status": "error", "error": "not attached"})
    return json.dumps({"offers": _session.poll_offers()})


@mcp.tool()
async def coord_close_channel(channel_id: str) -> str:
    """Close a channel on demand and unlink its ring files. Idempotent."""
    await _run_limited()
    if not _channels_enabled():
        return _DISABLED
    if _session is None:
        return json.dumps({"status": "error", "error": "not attached"})
    closed = _session.close_channel(channel_id)
    return json.dumps({"closed": closed})


@mcp.tool()
async def coord_list_channels() -> str:
    """List every channel this agent is currently a party to."""
    await _run_limited()
    if not _channels_enabled():
        return _DISABLED
    if _session is None:
        return json.dumps({"status": "error", "error": "not attached"})
    channels = contact.list_channels(_session._rpc._client, _session.agent_id)
    return json.dumps({"channels": [asdict(c) for c in channels]})


async def _run_limited() -> None:
    _set_correlation_id()
    _check_mcp_rate_limit()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Consistent --version across dev-tool entry points.
    from .mcp_common import maybe_print_version

    if maybe_print_version():
        raise SystemExit(0)

    enforce_transport_security("stdio")
    logger.info("[MCP] Starting beagle-coord MCP server (stdio)")
    mcp.run(transport="stdio")
