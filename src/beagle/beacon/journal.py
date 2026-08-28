# Copyright (c) 2026 Matt Creigh. Released under the MIT License.
# SPDX-License-Identifier: MIT
"""The write-behind journal: what makes an in-memory board durable.

See plans/beagle-beacon-coordination.xml WP-6, decision D-12, invariants
I-6/I-7, and hard constraint C-03.

D-12 (operator override, 2026-08-21) holds the whole work board in the
Beacon store — no SQLite tier, for performance. That makes this module
load-bearing for the whole system: the store is in memory, so this journal
is the ONLY thing standing between a crash and losing a session's work.

Only BOARD-CLASS keys are journalled and replayed (``issue:*`` excluding
``issue:claim:*``, ``comment:*``, ``transition:*`` — the work-board records
stage 7 writes). PRESENCE-class keys (``agent:*``, ``lock:*``, ``chan:*``,
``issue:claim:*``, ``plan:active``, ``beacon:teardown``) are never
journalled and never replayed (invariant I-6): replaying an ``agent:``
record resurrects an agent that is not running, and replaying a ``lock:``
record hands out a lock held by a dead process. The board is the past made
present again; the roster is the present only.

The append happens inside the Beacon process, AFTER the caller's own
mutation has already returned — never on an agent's critical path. fsync is
timer-driven (``journal_fsync_interval_s``), never per mutation (D-12): a
synchronous fsync on every write would put disk latency back on exactly the
path this whole design exists to keep clear.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import IO, Any

from beagle.beacon.backend import StoreClient
from beagle.beacon.keys import BeaconPaths

logger = logging.getLogger("Beagle.beacon.journal")

_BOARD_PREFIXES = ("issue:", "comment:", "transition:")
_BOARD_EXCLUDE_PREFIXES = ("issue:claim:",)

# C-03: no secret material may ever reach the archive or the journal.
_SECRET_NAME_PATTERN = re.compile(
    r"(secret|token|password|passwd|api[_-]?key|credential)", re.IGNORECASE
)

_JOURNAL_FILE_MODE = 0o600
_REPLAYABLE_OPS = frozenset({"hset", "sadd", "srem", "lpush", "ltrim", "zadd", "delete"})


def is_board_class(key: str) -> bool:
    """Return True if key belongs to the durable work board (I-6).

    ``issue:claim:<id>`` is excluded even though it starts with ``issue:``
    — it is a TTL-bearing presence key (who is actively on this issue right
    now), not a durable record.
    """
    if any(key.startswith(p) for p in _BOARD_EXCLUDE_PREFIXES):
        return False
    return any(key.startswith(p) for p in _BOARD_PREFIXES)


def _reject_secret_keys(key: str) -> None:
    if _SECRET_NAME_PATTERN.search(key):
        msg = f"refusing to journal key {key!r}: matches the secret-name pattern (C-03)"
        raise ValueError(msg)


class Journal:
    """Append-only JSONL writer for board-class mutations, with rotation.

    One line per mutation: ``{"op", "key", "args", "ts"}``. Buffered in the
    OS page cache between fsyncs; a background thread fsyncs on
    ``fsync_interval_s``, never per call (D-12).
    """

    def __init__(
        self,
        paths: BeaconPaths,
        *,
        max_bytes: int,
        max_files: int,
        fsync_interval_s: float,
    ) -> None:
        """Build the journal. Durability values are REQUIRED, never defaulted.

        The canonical homes for these values are the ``[coord]`` config keys
        ``archive_max_bytes``, ``archive_max_files`` and
        ``journal_fsync_interval_s`` — pass them from config at the wiring
        site (plans/beagle-config-defaults-abstraction.xml, CD-1). A default
        here would create a second, invisible source of truth.

        Args:
            paths: This Beacon instance's filesystem paths.
            max_bytes: Rotation threshold in bytes per journal file.
            max_files: Maximum number of rotation files to keep.
            fsync_interval_s: Seconds between background fsync passes.

        """
        self._paths = paths
        self._max_bytes = max_bytes
        self._max_files = max_files
        self._fsync_interval_s = fsync_interval_s
        self._lock = threading.Lock()
        self._fh: IO[str] | None = None
        self._current_path: Path | None = None
        self._dirty = False
        self.fsync_count = 0
        self.fsync_error_count = 0
        self.last_fsync_error_s: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._open_current()

    def _journal_dir(self) -> Path:
        d = self._paths.base_dir / "journal"
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d

    def _existing_numbers(self) -> list[int]:
        numbers = []
        for p in self._journal_dir().glob("journal-*.jsonl"):
            try:
                numbers.append(int(p.stem.removeprefix("journal-")))
            except ValueError:
                logger.warning("skipping malformed journal filename: %s", p.name)
                continue
        return sorted(numbers)

    def _open_current(self) -> None:
        numbers = self._existing_numbers()
        n = numbers[-1] if numbers else 0
        path = self._journal_dir() / f"journal-{n}.jsonl"
        if not path.exists():
            path.touch(mode=_JOURNAL_FILE_MODE)
        path.chmod(_JOURNAL_FILE_MODE)
        self._current_path = path
        self._fh = path.open("a", encoding="utf-8")
        # Rotation must not rely on Path.stat(): with no per-mutation fsync
        # (D-12), writes sit in the Python file object's own buffer and the
        # on-disk size stays stale between flushes. Track bytes written by
        # THIS process in memory instead; seed it from the real size only
        # once, at open, when nothing of ours could still be buffered.
        self._current_size = path.stat().st_size

    def _rotate_if_needed(self) -> None:
        if self._current_size < self._max_bytes or self._fh is None:
            return
        # Open the new file BEFORE closing the old one so no caller ever
        # observes a closed handle between rotations (E-1).
        old_fh = self._fh
        numbers = self._existing_numbers()
        next_n = (numbers[-1] + 1) if numbers else 1
        path = self._journal_dir() / f"journal-{next_n}.jsonl"
        path.touch(mode=_JOURNAL_FILE_MODE)
        new_fh = path.open("a", encoding="utf-8")
        self._fh = new_fh
        self._current_path = path
        self._current_size = 0
        old_fh.close()

        numbers = self._existing_numbers()
        while len(numbers) > self._max_files:
            oldest = numbers.pop(0)
            (self._journal_dir() / f"journal-{oldest}.jsonl").unlink(missing_ok=True)

    def record(self, op: str, key: str, args: dict[str, Any]) -> None:
        """Append one board-class mutation. A no-op for a presence-class key.

        Args:
            op: One of hset, sadd, srem, lpush, ltrim, zadd, delete.
            key: The store key mutated.
            args: The op's arguments (e.g. {"mapping": {...}} for hset).

        Raises:
            ValueError: op is not replayable, or key matches the secret
                pattern (C-03) — the caller must not have reached the store
                with a secret-shaped key at all; this is a defence, not the
                primary check.

        """
        if not is_board_class(key):
            return
        if op not in _REPLAYABLE_OPS:
            msg = f"journal: unreplayable op {op!r} for key {key!r}"
            raise ValueError(msg)
        _reject_secret_keys(key)

        record = {"op": op, "key": key, "args": args, "ts": time.time()}
        line = json.dumps(record, separators=(",", ":"))
        encoded = (line + "\n").encode("utf-8")
        with self._lock:
            if self._fh is None or self._fh.closed:
                msg = "journal: write rejected — the journal is closed"
                raise RuntimeError(msg)
            self._rotate_if_needed()
            self._fh.write(line + "\n")
            self._current_size += len(encoded)
            self._dirty = True

    def flush(self) -> None:
        """Force an fsync now, regardless of the timer.

        Idempotent: a no-op when the journal is clean, closed, or never
        opened. Failure state is recorded on the instance
        (``fsync_error_count`` / ``last_fsync_error_s``) and OSError is
        re-raised for callers that want to react; the fsync timer swallows
        it to stay alive (E-2).

        Raises:
            OSError: the fsync itself failed; durability is NOT confirmed
                for the pending records.

        """
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        """Flush+fsync. Caller MUST hold ``self._lock``."""
        if not self._dirty or self._fh is None or self._fh.closed:
            return
        try:
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except OSError:
            self.fsync_error_count += 1
            self.last_fsync_error_s = time.time()
            logger.exception("journal: fsync failed; durability NOT confirmed")
            self._publish_status("error")
            raise
        self._dirty = False
        self.fsync_count += 1
        self._publish_status("ok")

    def _publish_status(self, state: str) -> None:
        """Atomically publish fsync health for operator surfaces (audit A2).

        The Journal has no in-process owner to poll (WP-B7 backend slot is
        unassigned), so the operator-visible path is a status FILE beside the
        rotations: ``<base>/journal/journal_status.json``. ``beagle coord
        status`` renders it; any watchdog can read it. Written on every
        flush outcome so staleness itself is information.

        Args:
            state: "ok" or "error" — the outcome of this flush cycle.

        """
        try:
            journal_dir = self._paths.base_dir / "journal"
            journal_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            payload = json.dumps(
                {
                    "state": state,
                    "fsync_count": self.fsync_count,
                    "fsync_error_count": self.fsync_error_count,
                    "last_fsync_error_s": self.last_fsync_error_s,
                    "updated_at_s": time.time(),
                },
                indent=2,
            )
            target = journal_dir / "journal_status.json"
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, target)
        except OSError as exc:
            # Status publication must never break the durability path it
            # reports on — but it may not fail silently either.
            logger.warning("journal: failed to publish status file: %s", exc)

    def _fsync_loop(self) -> None:
        while not self._stop.wait(self._fsync_interval_s):
            try:
                self.flush()
            except (OSError, ValueError) as exc:
                # Already counted and logged inside _flush_locked (E-2); this
                # handler logs too so the survive-and-retry decision is
                # visible to the operator, never silent. The timer thread
                # MUST stay alive so durability resumes when the disk
                # recovers — a dead fsync thread is the silent data-loss
                # path this loop exists to prevent.
                logger.warning(
                    "journal: fsync timer cycle failed (%s); will retry on next tick",
                    exc,
                )
                continue

    def start(self) -> None:
        """Start the background fsync-on-timer thread."""
        self._thread = threading.Thread(
            target=self._fsync_loop, name="beacon-journal-fsync", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the fsync thread, flush once more, and close the file.

        Idempotent (E-1): a second call is a no-op. After close, ``flush``
        is a no-op and ``record`` raises RuntimeError — a write after
        teardown must never be silently dropped.
        """
        if self._stop.is_set():
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self._lock:
            self._flush_locked()
            if self._fh is not None:
                self._fh.close()
                self._fh = None
            self._dirty = False


def replay(client: StoreClient, paths: BeaconPaths) -> int:
    """Rebuild board-class keys from every journal rotation, oldest first.

    Presence-class keys are NEVER replayed (invariant I-6) — this function
    only ever calls a redis command whose key already passed is_board_class.

    Args:
        client: A redis client connected to the (freshly started) store.
        paths: This Beacon instance's filesystem paths.

    Returns:
        The number of records replayed.

    """
    journal_dir = paths.base_dir / "journal"
    if not journal_dir.exists():
        return 0

    numbers = sorted(
        int(p.stem.removeprefix("journal-"))
        for p in journal_dir.glob("journal-*.jsonl")
        if p.stem.removeprefix("journal-").isdigit()
    )

    replayed = 0
    for n in numbers:
        path = journal_dir / f"journal-{n}.jsonl"
        # Stream line-by-line (E-3): a rotation file can be up to
        # [coord].archive_max_bytes; reading it whole is an OOM at startup.
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.warning("skipping malformed journal line in %s: %s", path, exc)
                    continue
                if not isinstance(record, dict) or not _replayable_shape(record):
                    # E-4: a valid-JSON record with drifted shape must skip
                    # with a warning, never KeyError-abort the whole replay.
                    logger.warning(
                        "replay: skipping drifted record in %s: %s", path, str(line)[:80]
                    )
                    continue
                if not is_board_class(record.get("key", "")):
                    # Defence in depth: record() already filters this on write,
                    # but replay must never trust the file's own contents to
                    # decide what is safe to restore (I-6).
                    continue
                _replay_one(client, record)
                replayed += 1
    return replayed


_REPLAY_REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "hset": ("mapping",),
    "sadd": ("values",),
    "srem": ("values",),
    "lpush": ("values",),
    "ltrim": ("start", "end"),
    "zadd": ("mapping",),
    "delete": (),
}


def _replayable_shape(record: dict[str, Any]) -> bool:
    """Return True when a journalled record carries every field its op needs.

    Args:
        record: A decoded JSON line from a journal rotation file.

    Returns:
        True when ``op`` is a replayable op, ``key`` is a str, ``args`` is a
        dict, and every argument the op replays is present.

    """
    op = record.get("op")
    if not isinstance(op, str) or op not in _REPLAY_REQUIRED_ARGS:
        return False
    if not isinstance(record.get("key"), str) or not isinstance(record.get("args"), dict):
        return False
    args: dict[str, Any] = record["args"]
    return all(name in args for name in _REPLAY_REQUIRED_ARGS[op])


def _replay_one(client: StoreClient, record: dict[str, Any]) -> None:
    op = record["op"]
    key = record["key"]
    args = record["args"]
    if op == "hset":
        client.hset(key, mapping=args["mapping"])
    elif op == "sadd":
        client.sadd(key, *args["values"])
    elif op == "srem":
        client.srem(key, *args["values"])
    elif op == "lpush":
        client.lpush(key, *args["values"])
    elif op == "ltrim":
        client.ltrim(key, args["start"], args["end"])
    elif op == "zadd":
        client.zadd(key, args["mapping"])
    elif op == "delete":
        client.delete(key)
    else:
        logger.warning("replay: skipping unknown op %r for key %r", op, key)
