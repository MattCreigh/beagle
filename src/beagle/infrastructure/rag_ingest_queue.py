"""Redis-backed single-flight queue for git-diff RAG ingests.

<invariant>
Concurrent ingest triggers must NEVER race into two simultaneous corpus
swaps. All triggers land on a Redis dirty-flag; exactly ONE worker holds
the lock; every drain recomputes its work unit from the LAST SUCCESSFUL
SNAPSHOT to current HEAD — so N triggers during a run collapse into ONE
follow-up delta, never N competing swaps.
</invariant>

Semantics (contract):
    enqueue()  -> INCR dirty; if the worker lock is free it is claimed and a
                  drain starts. The trigger "sets to 0" here: accepting a job
                  consumes the pending count.
    drain()    -> loop: base = snapshot SHA; head = HEAD;
                  paths = changed(base..head) + working-tree changes;
                  if none: advance snapshot, clear dirty, stop.
                  else: run backend(paths); on success snapshot=head,
                  clear dirty; if triggers arrived DURING the run, loop again —
                  the next pass diffs from the NEW snapshot (coalesced delta),
                  not from whenever the burst started.

Keys (prefix-configurable):
    {p}dirty      int    pending triggers not yet consumed by a drain
    {p}snapshot   str    git SHA of the last successfully ingested state
    {p}lock       str    single-flight claim (SET NX PX, token-released)
    {p}last_error str    last backend failure (diagnostics)

<config-change>
    <file>src/beagle/infrastructure/rag_ingest_queue.py</file>
    <change>new module — no existing behaviour altered; opt-in via CLI/main()</change>
    <reason>B-queue: repeated concurrent reingest churn (603 observed in one
    day) raced staging swaps and left torn LanceDB fragments behind.</reason>
</config-change>

<verification-checklist>
    1. python3 -m pytest tests/test_rag_ingest_queue.py -q        # all green
    2. Concurrent enqueue() xN with slow backend -> exactly one drain pass per
       settle, final snapshot == final HEAD, dirty == 0.
</verification-checklist>
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis import Redis

logger = logging.getLogger("Beagle.infrastructure.rag_ingest_queue")

DEFAULT_PREFIX = "beagle:rag:ingest:"
LOCK_TTL_SECONDS = 3600

#: Resolved absolute git binary (S607: never a bare partial path).
_GIT_BIN = shutil.which("git")
if not _GIT_BIN:
    raise RuntimeError("git executable not found on PATH — rag ingest queue requires git")


# ── Store protocol ──────────────────────────────────────────────────────────


@dataclass
class QueueStore:
    """Minimal KV interface the queue needs; wraps a redis.Redis client."""

    client: Redis
    prefix: str = DEFAULT_PREFIX

    def _k(self, name: str) -> str:
        return f"{self.prefix}{name}"

    def incr(self, key: str) -> int:
        return int(self.client.incr(self._k(key)))

    def get(self, key: str) -> str | None:
        value = self.client.get(self._k(key))
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else value

    def set(self, key: str, value: str) -> None:
        self.client.set(self._k(key), value)

    def delete(self, key: str) -> None:
        self.client.delete(self._k(key))

    def acquire_lock(self, ttl_seconds: int = LOCK_TTL_SECONDS) -> str | None:
        """Claim the single-flight lock; returns the token, or None if held."""
        token = uuid.uuid4().hex
        claimed = self.client.set(self._k("lock"), token, nx=True, ex=ttl_seconds)
        return token if claimed else None

    def release_lock(self, token: str) -> None:
        """Release the lock only if we still own it (GET-compare-DEL)."""
        key = self._k("lock")
        if self.get("lock") == token:
            self.client.delete(key)


# ── Git helpers ─────────────────────────────────────────────────────────────


def _git(repo: str, *args: str) -> str:
    # Re-narrow locally: mypy treats module-level globals as mutable, so the
    # import-time None-check on _GIT_BIN does not carry into function bodies.
    git_bin = _GIT_BIN
    if not git_bin:
        raise RuntimeError("git executable unavailable")
    out = subprocess.run(
        [git_bin, "-C", repo, *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if out.returncode != 0:
        msg = f"git {' '.join(args)} failed: {out.stderr.strip()}"
        raise RuntimeError(msg)
    # subprocess.run(text=True) guarantees stdout is str.
    return out.stdout.strip()


def head_sha(repo: str) -> str:
    """Return the repository's current HEAD commit SHA."""
    return _git(repo, "rev-parse", "HEAD")


def delta_paths(repo: str, base_sha: str | None, head_sha_value: str) -> list[str]:
    """Return repo-relative file paths changed between base and head, plus WT.

    Includes committed diffs (``base..head``) when a snapshot exists and all
    working-tree modifications/untracked files regardless — a snapshot only
    covers COMMITTED history, so uncommitted state must always be re-examined.

    Args:
        repo: Path to the git repository.
        base_sha: Snapshot SHA, or None for a first (full) ingest.
        head_sha_value: Current HEAD SHA.

    Returns:
        Sorted list of existing repo-relative file paths needing ingestion.
    """
    changed: set[str] = set()
    if base_sha is None:
        # No snapshot yet: the work unit is the ENTIRE tracked tree (plus any
        # working-tree additions below). A committed-clean repo would otherwise
        # look like "nothing to do" on the very first ingest.
        changed.update(line for line in _git(repo, "ls-files").splitlines() if line)
    else:
        out = _git(repo, "diff", "--name-only", f"{base_sha}..{head_sha_value}")
        changed.update(line for line in out.splitlines() if line)
    out = _git(repo, "status", "--porcelain")
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        if path.endswith("/"):
            continue
        changed.add(path)
    from pathlib import Path as _P

    root = _P(repo)
    return sorted(p for p in changed if (root / p).is_file())


# ── Queue operations ────────────────────────────────────────────────────────

Backend = Callable[[str, list[str]], dict]


def enqueue(
    store: QueueStore,
    repo: str,
    backend: Backend,
    autostart: bool = True,
) -> dict[str, object]:
    """Register an ingest trigger and start a drain if no worker holds the lock.

    Args:
        store: Queue KV store (Redis-backed or test double).
        repo: Repository path to ingest.
        backend: Callable(repo, paths) performing the actual ingestion.
        autostart: When True and the lock was free, run the drain in a daemon
            thread immediately; otherwise the caller owns calling drain().

    Returns:
        {"status": "accepted"|"queued", "dirty": <int>, "drained_inline": bool}
    """
    dirty = store.incr("dirty")
    token = store.acquire_lock()
    if token is None:
        logger.info("[RagIngestQueue] worker active — trigger queued (dirty=%d)", dirty)
        return {"status": "queued", "dirty": dirty, "drained_inline": False}
    store.delete("dirty")  # accepted: the drain now OWNS everything pending
    if autostart:
        thread = threading.Thread(
            target=_drain_locked,
            args=(store, repo, backend, token),
            name="beagle-rag-ingest-drain",
            daemon=True,
        )
        thread.start()
        return {"status": "accepted", "dirty": 0, "drained_inline": False}
    summary = drain(store, repo, backend, token=token)
    return {
        "status": "accepted",
        "dirty": 0,
        "drained_inline": True,
        "drain_status": summary.get("status"),
        "passes": summary.get("passes", 0),
    }


def drain(
    store: QueueStore,
    repo: str,
    backend: Backend,
    token: str,
    max_passes: int = 8,
) -> dict[str, object]:
    """Run settle-loop drains while the lock is held. See module docstring."""
    summary: dict[str, object] = {"passes": 0}
    passes = 0
    try:
        for _ in range(max_passes):
            pass_head = head_sha(repo)
            base = store.get("snapshot")
            paths = delta_paths(repo, base, pass_head)
            if not paths:
                # Nothing between snapshot and HEAD and a clean worktree:
                # safe to fully settle.
                store.set("snapshot", pass_head)
                store.delete("dirty")
                summary["status"] = "clean"
                return summary
            try:
                outcome = backend(repo, paths)
                summary["last_backend"] = outcome
            except (
                RuntimeError,
                ValueError,
                OSError,
                subprocess.SubprocessError,
            ) as err:
                # Doctrine floor (BLE001): no blind `except Exception`. Anything
                # outside this tuple propagates — the finally-block releases the
                # lock, so a loud failure can never wedge the queue. The error
                # is surfaced via the last_error key for operator pickup.
                logger.warning("[RagIngestQueue] backend failed: %s", err)
                store.set("last_error", str(err)[:500])
                store.incr("dirty")  # keep the pending signal visible
                summary["status"] = "error"
                return summary
            # The snapshot may ONLY advance to the head this pass actually
            # ingested. Commits landing during the backend call are newer
            # than `pass_head`; advancing past them would mark un-ingested
            # work as done (lost update).
            store.set("snapshot", pass_head)
            store.delete("dirty")
            passes += 1
            summary["passes"] = passes
            if head_sha(repo) == pass_head and not store.get("dirty"):
                summary["status"] = "ok"
                return summary
            logger.info("[RagIngestQueue] new commits/triggers during pass — coalescing next delta")
        summary["status"] = "deferred"
        return summary
    finally:
        store.release_lock(token)


def _drain_locked(store: QueueStore, repo: str, backend: Backend, token: str) -> None:
    try:
        drain(store, repo, backend, token=token)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as err:
        logger.exception("[RagIngestQueue] drain crashed: %s", err)
        store.set("last_error", str(err)[:500])


@dataclass
class DrainObservation:
    """Test/diagnostic view of what a drain consumed."""

    passes: list[list[str]] = field(default_factory=list)


def make_default_backend() -> Backend:
    """Backend wired to the deployed hotswap pipeline (full-corpus swap).

    Delta narrowing lands when the writer grows a per-path upsert API; until
    then bursts are COALESCED (one swap per settle) which already removes the
    clobbering this queue exists to prevent.
    """

    from beagle.infrastructure.hotswap_ingest import hotswap_ingest

    def _backend(repo: str, paths: list[str]) -> dict:
        del paths  # full swap; see docstring
        return dict(hotswap_ingest(target_directory=repo) or {})

    return _backend
