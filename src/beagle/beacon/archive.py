# Copyright (c) 2026 Matt Creigh. Released under the MIT License.
# SPDX-License-Identifier: MIT
"""Full key-space snapshot on last detach, with a single-flusher election.

See plans/beagle-beacon-coordination.xml WP-6, decision D-07 (the
audit_reader-shaped archive contract), and concept spec section 9: a failed
flush keeps a ``.partial`` archive and logs loudly — it never silently
drops state.

Two agents can observe "I am the last one leaving" at nearly the same
instant. ``elect_flush_owner`` uses ``SET beacon:teardown <id> NX EX 30`` so
exactly one of them proceeds to flush; the loser simply returns.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from beagle.beacon.backend import BackendUnavailableError, StoreClient
from beagle.beacon.journal import _SECRET_NAME_PATTERN
from beagle.beacon.keys import BeaconPaths
from beagle.utils.atomic import atomic_write_text

logger = logging.getLogger("Beagle.beacon.archive")

_ARCHIVE_FILE_MODE = 0o600
_TEARDOWN_KEY = "beacon:teardown"
_TEARDOWN_TTL_S = 30


def elect_flush_owner(client: StoreClient, agent_id: str) -> bool:
    """Decide, atomically, which of possibly-several last agents flushes.

    Args:
        client: A redis client connected to this Beacon's store.
        agent_id: The candidate agent's id.

    Returns:
        True if this agent won the election (SET ... NX succeeded) and must
        proceed to flush. False means another agent already won.

    """
    return bool(client.set(_TEARDOWN_KEY, agent_id, nx=True, ex=_TEARDOWN_TTL_S))


def _next_archive_number(archive_dir: Path) -> int:
    numbers = []
    for p in archive_dir.glob("beacon-*.jsonl"):
        try:
            numbers.append(int(p.stem.removeprefix("beacon-")))
        except ValueError:
            logger.warning("skipping malformed archive filename: %s", p.name)
            continue
    return (max(numbers) + 1) if numbers else 0


def snapshot_keyspace(client: StoreClient) -> dict[str, Any]:
    """Read every key in the store into a JSON-serialisable snapshot.

    Raises:
        ValueError: a key matches the secret-name pattern (C-03) — the
            archive must never contain one, and this is checked at the
            point of snapshot, not left to the caller.

    """
    snapshot: dict[str, Any] = {}
    for key in client.scan_iter():
        if _SECRET_NAME_PATTERN.search(key):
            msg = f"refusing to archive key {key!r}: matches the secret-name pattern (C-03)"
            raise ValueError(msg)
        key_type = client.type(key)
        if key_type == "string":
            snapshot[key] = {"type": "string", "value": client.get(key)}
        elif key_type == "hash":
            snapshot[key] = {"type": "hash", "value": client.hgetall(key)}
        elif key_type == "set":
            snapshot[key] = {"type": "set", "value": sorted(client.smembers(key))}
        elif key_type == "list":
            snapshot[key] = {"type": "list", "value": client.lrange(key, 0, -1)}
        elif key_type == "zset":
            snapshot[key] = {
                "type": "zset",
                "value": client.zrange(key, 0, -1, withscores=True),
            }
        else:
            logger.warning("snapshot: skipping key %r of unhandled type %r", key, key_type)
    return snapshot


def flush_archive(client: StoreClient, paths: BeaconPaths) -> Path:
    """Write the full key-space snapshot to a numbered beacon-<n>.jsonl.

    On failure, a ``.partial`` file is left in place and the failure is
    logged at ERROR — state is never silently dropped.

    Args:
        client: A redis client connected to this Beacon's store.
        paths: This Beacon instance's filesystem paths.

    Returns:
        The path the archive was written to.

    Raises:
        OSError: the write failed. A .partial file is left for inspection.

    """
    archive_dir = paths.archive_dir
    archive_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    n = _next_archive_number(archive_dir)
    dest = archive_dir / f"beacon-{n}.jsonl"
    partial = archive_dir / f"beacon-{n}.jsonl.partial"

    try:
        snapshot = snapshot_keyspace(client)
        payload = json.dumps(snapshot, separators=(",", ":"))
        atomic_write_text(dest, payload, mode=_ARCHIVE_FILE_MODE)
        partial.unlink(missing_ok=True)
    except (OSError, ValueError, BackendUnavailableError) as exc:
        logger.error("archive flush failed for %s: %s", dest, exc)
        try:
            payload = json.dumps({"error": str(exc)}, separators=(",", ":"))
            partial.write_text(payload, encoding="utf-8")
            partial.chmod(_ARCHIVE_FILE_MODE)
        except OSError:
            logger.error("could not even write the .partial marker for %s", dest)
        raise

    return dest
