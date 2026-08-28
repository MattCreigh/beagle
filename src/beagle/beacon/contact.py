# Copyright (c) 2026 Matt Creigh. Released under the MIT License.
# SPDX-License-Identifier: MIT
"""Contact directory and pairwise peer rendezvous channels.

See plans/beagle-beacon-coordination.xml WP-5B, decisions D-09/D-10/D-11,
invariants I-4/I-5, and measured facts M-11/M-12.

Beacon brokers the introduction and then gets out of the way (invariant
I-4): `open_channel` allocates a pairwise ring in each direction (M-11 —
rings are SPSC, so a bidirectional channel is two rings, never one),
returns both paths to the opener, and pushes an offer to the callee's
out-ring. After that, payload traffic never touches the store or the
Beacon process.

Contact fields live INSIDE `agent:<id>` (invariant I-5), never in a
separate `contact:<id>` key — a second key would carry a second TTL, and
the two would drift. A lookup against an agent whose lease has expired
therefore returns Unreachable by construction: the hash is simply gone.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# orpheus is the optional proprietary ring transport. Without it, channel
# rings cannot be provisioned or pushed — but the channel itself is fully
# recorded in the store and reachable over the socket RPC (see _push_offer's
# documented fallback), so orpheus is never a hard dependency.
try:
    import orpheus
except ImportError:  # pragma: no cover - exercised only when the wheel is absent
    orpheus = None  # type: ignore[assignment]

from beagle.beacon.backend import StoreClient
from beagle.beacon.keys import BeaconPaths

logger = logging.getLogger("Beagle.beacon.contact")

_CHANNEL_SLOT_SIZE = 16384
_CHANNEL_SLOT_COUNT = 32
_CHANNEL_FILE_MODE = 0o600
# The out-ring is an AGENT ring (server.py's _RING_SLOT_SIZE/_RING_SLOT_COUNT),
# not a channel ring — it carries small push notifications, not peer payloads.
# It must be opened at the SAME slot_size/slot_count RingPoller.attach() used
# to create it, or orpheus raises a header mismatch.
_OUT_RING_SLOT_SIZE = 4096
_OUT_RING_SLOT_COUNT = 64
_DEFAULT_ACCEPTS = ("handoff", "query")
_ENVELOPE_VERSION = 1


@dataclass(frozen=True)
class ContactCard:
    """What a channel opener learns about a prospective peer."""

    agent_id: str
    contactable: bool
    inbox_ring: str
    accepts: tuple[str, ...]
    max_msg_bytes: int
    reason: str = ""


@dataclass(frozen=True)
class Channel:
    """A pairwise peer channel: two rings, one per direction."""

    channel_id: str
    a_id: str
    b_id: str
    a2b_path: str
    b2a_path: str
    state: str
    created_at: str


@dataclass(frozen=True)
class Unreachable:
    """`open_channel` could not reach the requested peer. Never a stale path."""

    reason: str


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _channel_key(channel_id: str) -> str:
    return f"chan:{channel_id}"


def _index_key(agent_id: str) -> str:
    return f"chan:index:{agent_id}"


def read_contact_card(client: StoreClient, agent_id: str) -> ContactCard | None:
    """Read a peer's contact card from its (live) agent record.

    Returns:
        None if the agent's lease has expired (the ``agent:<id>`` hash is
        gone) — this is what makes I-5 hold: there is no separate,
        independently-TTLed key to return a stale value from.

    """
    data = client.hgetall(f"agent:{agent_id}")
    if not data:
        return None
    return ContactCard(
        agent_id=agent_id,
        contactable=data.get("contactable", "1") == "1",
        inbox_ring=data.get("inbox_ring", ""),
        accepts=tuple(a for a in data.get("accepts", ",".join(_DEFAULT_ACCEPTS)).split(",") if a),
        max_msg_bytes=int(data.get("max_msg_bytes", _CHANNEL_SLOT_SIZE)),
    )


def open_channel(
    client: StoreClient,
    paths: BeaconPaths,
    opener: str,
    peer_id: str,
    kind: str,
    *,
    max_channels_per_agent: int = 16,
) -> Channel | Unreachable:
    """Allocate a pairwise channel between opener and peer_id.

    Args:
        client: A redis client connected to this Beacon's store.
        paths: This Beacon instance's filesystem paths.
        opener: The agent requesting the channel (becomes Channel.a_id).
        peer_id: The agent being contacted (becomes Channel.b_id).
        kind: The channel kind the peer must list in its `accepts`.
        max_channels_per_agent: Per-agent channel cap (D-11).

    Returns:
        A Channel with state "open", or Unreachable(reason) when the peer's
        lease has expired, it declared itself not contactable, it does not
        accept `kind`, or either party is already at the channel cap.
        NEVER returns a stale address (I-5) — it either allocates a fresh,
        live channel or explains why it could not.

    """
    card = read_contact_card(client, peer_id)
    if card is None:
        return Unreachable(reason=f"agent {peer_id} has no live lease")
    if not card.contactable:
        return Unreachable(reason=f"agent {peer_id} is not contactable: {card.reason}")
    if kind not in card.accepts:
        return Unreachable(reason=f"agent {peer_id} does not accept channel kind {kind!r}")

    for agent_id in (opener, peer_id):
        if client.scard(_index_key(agent_id)) >= max_channels_per_agent:
            return Unreachable(
                reason=f"agent {agent_id} is at its channel cap ({max_channels_per_agent})"
            )

    channel_id = str(uuid.uuid4())
    channel_dir = paths.channel_dir(channel_id)
    channel_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    a2b_path = channel_dir / "a2b.ring"
    b2a_path = channel_dir / "b2a.ring"

    # Beacon provisions both rings once (reset=True); each side later opens
    # its own end with reset=False, mirroring the in-ring convention (WP-4)
    # so two independent processes never race to reset the same file.
    if orpheus is not None:  # pragma: no cover - orpheus absent
        for p in (a2b_path, b2a_path):
            ring = orpheus.OrpheusRing(
                str(p), "writer", True, _CHANNEL_SLOT_SIZE, _CHANNEL_SLOT_COUNT, "fifo"
            )
            del ring
            p.chmod(_CHANNEL_FILE_MODE)

    channel = Channel(
        channel_id=channel_id,
        a_id=opener,
        b_id=peer_id,
        a2b_path=str(a2b_path),
        b2a_path=str(b2a_path),
        state="open",
        created_at=_now_iso(),
    )

    client.hset(
        _channel_key(channel_id),
        mapping={
            "a_id": channel.a_id,
            "b_id": channel.b_id,
            "a2b_path": channel.a2b_path,
            "b2a_path": channel.b2a_path,
            "state": channel.state,
            "created_at": channel.created_at,
        },
    )
    client.sadd(_index_key(opener), channel_id)
    client.sadd(_index_key(peer_id), channel_id)

    _push_offer(client, card, channel, kind)

    return channel


def _push_offer(client: StoreClient, card: ContactCard, channel: Channel, kind: str) -> None:
    """Push a channel-offer notification to the callee's out-ring.

    Best-effort: the callee's connector surfaces this on its next poll. If
    the callee's out-ring cannot be reached (not yet attached, or full),
    the offer is still fully recorded in the store (chan:<id>) — the
    callee's periodic roster/channel poll over the socket RPC path is the
    fallback, so a dropped push notification never loses the channel
    itself, only its immediacy.
    """
    if not card.inbox_ring or orpheus is None:  # pragma: no cover - orpheus absent
        return
    envelope = json.dumps(
        {
            "v": _ENVELOPE_VERSION,
            "type": "channel_offer",
            "channel_id": channel.channel_id,
            "peer_id": channel.a_id,
            "kind": kind,
            "b2a_path": channel.b2a_path,
            "a2b_path": channel.a2b_path,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        ring = orpheus.OrpheusRing(
            card.inbox_ring, "writer", False, _OUT_RING_SLOT_SIZE, _OUT_RING_SLOT_COUNT, "fifo"
        )
        buf = ring.reserve()
        if buf is None:
            logger.warning(
                "out-ring full for %s, offer not pushed (channel still open)", card.agent_id
            )
            return
        buf[: len(envelope)] = envelope
        ring.commit(len(envelope))
    except (OSError, RuntimeError) as exc:
        logger.warning("could not push channel offer to %s: %s", card.agent_id, exc)


def sweep_channels(
    client: StoreClient,
    paths: BeaconPaths,
    *,
    channel_offer_ttl_s: int = 30,
) -> list[str]:
    """Close and unlink channels whose party died or whose offer timed out.

    Called on every heartbeat tick (D-11). Unlinks the ring FILES, not just
    the store record — a channel left as a dangling store entry with live
    ring files leaks tmpfs (D-11).

    Args:
        client: A redis client connected to this Beacon's store.
        paths: This Beacon instance's filesystem paths.
        channel_offer_ttl_s: How long an unaccepted "offered" channel may
            live before it is swept.

    Returns:
        The channel ids that were closed.

    """
    closed: list[str] = []
    for key in client.scan_iter(match="chan:*"):
        if key.startswith("chan:index:"):
            continue
        channel_id = key.removeprefix("chan:")
        data = client.hgetall(key)
        if not data:
            continue

        a_alive = bool(client.exists(f"agent:{data['a_id']}"))
        b_alive = bool(client.exists(f"agent:{data['b_id']}"))
        stale_offer = data.get("state") == "offered" and _older_than(
            data.get("created_at", ""), channel_offer_ttl_s
        )

        if a_alive and b_alive and not stale_offer:
            continue

        client.delete(key)
        client.srem(_index_key(data["a_id"]), channel_id)
        client.srem(_index_key(data["b_id"]), channel_id)
        for ring_path in (data.get("a2b_path"), data.get("b2a_path")):
            if ring_path:
                Path(ring_path).unlink(missing_ok=True)
        channel_dir = paths.channel_dir(channel_id)
        if channel_dir.exists() and not any(channel_dir.iterdir()):
            channel_dir.rmdir()
        closed.append(channel_id)

    return closed


def _older_than(created_at_iso: str, max_age_s: float) -> bool:
    if not created_at_iso:
        return True
    try:
        created = datetime.fromisoformat(created_at_iso)
    except ValueError:
        return True
    return (datetime.now(UTC) - created).total_seconds() > max_age_s


def send(ring: Any, payload: bytes) -> bool:
    """Write one message to a channel ring. Never blocks, never retries.

    Args:
        ring: An already-open orpheus.OrpheusRing, this side's writer end.
        payload: The message bytes. Must fit within the channel slot size.

    Returns:
        True if committed. False on a full ring (M-12: reserve() returns
        None rather than blocking) — this is a DROP (decision D-10), not an
        error. The caller records a channel_drop event on its own Beacon
        ring and continues; it must not loop or sleep-and-retry here.

    """
    buf = ring.reserve()
    if buf is None:
        return False
    buf[: len(payload)] = payload
    ring.commit(len(payload))
    return True


def poll_offers(ring: Any) -> list[dict[str, Any]]:
    """Drain any pending channel-offer notifications from an agent's out-ring.

    Args:
        ring: This agent's already-open out-ring, reader end.

    Returns:
        Decoded offer envelopes, oldest first. Malformed slots are logged
        and skipped, never raised — a push channel is a courtesy (see
        `_push_offer`); a bad envelope on it must not crash the poller.

    """
    offers: list[dict[str, Any]] = []
    while True:
        raw = ring.peek(False)
        if raw is None:
            break
        try:
            envelope = json.loads(bytes(raw).decode("utf-8"))
            if envelope.get("v") == _ENVELOPE_VERSION and envelope.get("type") == "channel_offer":
                offers.append(envelope)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("dropping malformed channel offer: %s", exc)
        finally:
            ring.advance()
    return offers


def close_channel(client: StoreClient, channel_id: str) -> bool:
    """Close a channel on demand (not via the sweep). Unlinks its ring files.

    Args:
        client: A redis client connected to this Beacon's store.
        channel_id: The channel to close.

    Returns:
        True if a channel with that id existed and was closed. False if it
        was already gone (idempotent — a caller does not need to check
        existence first).

    """
    key = _channel_key(channel_id)
    data = client.hgetall(key)
    if not data:
        return False

    client.delete(key)
    client.srem(_index_key(data.get("a_id", "")), channel_id)
    client.srem(_index_key(data.get("b_id", "")), channel_id)
    for ring_path in (data.get("a2b_path"), data.get("b2a_path")):
        if ring_path:
            Path(ring_path).unlink(missing_ok=True)
    return True


def list_channels(client: StoreClient, agent_id: str) -> list[Channel]:
    """List every channel agent_id is currently a party to.

    Args:
        client: A redis client connected to this Beacon's store.
        agent_id: The agent whose channels to list.

    Returns:
        Channel records for every id in chan:index:<agent_id> that still
        resolves to a live chan:<id> hash (a sweep may have already closed
        one between the index read and this lookup — such an id is simply
        skipped, not reported as an error).

    """
    channels: list[Channel] = []
    for channel_id in client.smembers(_index_key(agent_id)):
        data = client.hgetall(_channel_key(channel_id))
        if not data:
            continue
        channels.append(
            Channel(
                channel_id=channel_id,
                a_id=data["a_id"],
                b_id=data["b_id"],
                a2b_path=data["a2b_path"],
                b2a_path=data["b2a_path"],
                state=data["state"],
                created_at=data["created_at"],
            )
        )
    return channels
