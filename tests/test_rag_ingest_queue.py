"""Unit tests for the Redis-backed git-diff RAG ingest queue.

Uses fakeredis (already a project dependency for Beacon) so no redis-server
binary is required; the QueueStore adapter is exercised against the same
command surface a live redis.Redis exposes.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import cast

import fakeredis
import pytest

from beagle.infrastructure.rag_ingest_queue import (
    QueueStore,
    delta_paths,
    drain,
    enqueue,
    head_sha,
)


@pytest.fixture
def store() -> QueueStore:
    return QueueStore(client=fakeredis.FakeStrictRedis())


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal git repository with one committed file."""
    r = tmp_path / "repo"
    r.mkdir()

    def g(*args: str) -> None:
        subprocess.run(["git", "-C", str(r), *args], check=True, capture_output=True)

    subprocess.run(["git", "init", "-q", str(r)], check=True, capture_output=True)
    (r / "a.py").write_text("x = 1\n")
    g("add", ".")
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return r


def _commit(repo: Path, name: str, content: str) -> None:
    (repo / name).write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            f"add {name}",
        ],
        check=True,
        capture_output=True,
    )


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, repo: str, paths: list[str]) -> dict:
        self.calls.append(list(paths))
        return {"ingested": len(paths)}


# ── delta computation ───────────────────────────────────────────────────────


def test_delta_first_run_covers_working_tree(repo: Path) -> None:
    (repo / "b.py").write_text("y = 2\n")  # untracked
    paths = delta_paths(str(repo), None, head_sha(str(repo)))
    assert paths == ["a.py", "b.py"]


def test_delta_narrows_to_snapshot_diff(repo: Path) -> None:
    base = head_sha(str(repo))
    _commit(repo, "b.py", "y = 2\n")
    (repo / "c.py").write_text("z = 3\n")  # uncommitted
    paths = delta_paths(str(repo), base, head_sha(str(repo)))
    assert paths == ["b.py", "c.py"]


def test_delta_excludes_deleted_paths(repo: Path) -> None:
    base = head_sha(str(repo))
    (repo / "a.py").unlink()
    paths = delta_paths(str(repo), base, head_sha(str(repo)))
    assert "a.py" not in paths


# ── enqueue / drain lifecycle ───────────────────────────────────────────────


def test_enqueue_accepts_and_drains_inline(store: QueueStore, repo: Path) -> None:
    backend = RecordingBackend()
    result = enqueue(store, str(repo), backend, autostart=False)
    assert result["status"] == "accepted"
    assert store.get("snapshot") == head_sha(str(repo))
    assert store.get("dirty") is None
    assert store.get("lock") is None
    assert len(backend.calls) == 1


def test_second_enqueue_after_snapshot_is_empty_delta(
    store: QueueStore,
    repo: Path,
) -> None:
    backend = RecordingBackend()
    enqueue(store, str(repo), backend, autostart=False)
    # No new work: the follow-up drain must be a clean no-op pass.
    token = store.acquire_lock()
    assert token is not None
    summary = drain(store, str(repo), backend, token=token)
    assert summary["status"] == "clean"
    assert backend.calls == [backend.calls[0]]


def test_burst_coalesces_into_one_follow_up_pass(store: QueueStore, repo: Path) -> None:
    backend = RecordingBackend()
    enqueue(store, str(repo), backend, autostart=False)

    # Two commits land AFTER the snapshot, plus two fresh triggers arrive.
    _commit(repo, "b.py", "y = 2\n")
    store.incr("dirty")
    store.incr("dirty")

    token = store.acquire_lock()
    assert token is not None
    summary = drain(store, str(repo), backend, token=token)

    assert summary["status"] == "ok"
    assert summary["passes"] == 1  # coalesced, not per-trigger
    assert backend.calls[-1] == ["b.py"]  # delta vs NEW snapshot
    assert store.get("snapshot") == head_sha(str(repo))
    assert store.get("dirty") is None
    assert store.get("lock") is None


def test_trigger_during_backend_run_reloops(store: QueueStore, repo: Path) -> None:
    state: dict = {"phase": 0}

    class MidRunTriggerBackend(RecordingBackend):
        def __call__(self, repo_arg: str, paths: list[str]) -> dict:
            out = super().__call__(repo_arg, paths)
            if state["phase"] == 0:
                state["phase"] = 1
                _commit(Path(repo_arg), "late.py", "late = True\n")
                store.incr("dirty")  # trigger arrives mid-run
            return out

    backend = MidRunTriggerBackend()
    token = store.acquire_lock()
    assert token is not None
    summary = drain(store, str(repo), backend, token=token)

    assert summary["status"] == "ok"
    assert summary["passes"] == 2  # initial + coalesced pass
    assert backend.calls[-1] == ["late.py"]
    assert store.get("lock") is None


def test_busy_worker_turns_triggers_into_queued(store: QueueStore, repo: Path) -> None:
    token = store.acquire_lock()
    assert token is not None
    result = enqueue(store, str(repo), RecordingBackend(), autostart=False)
    assert result["status"] == "queued"
    assert int(cast("int", result["dirty"])) >= 1


def test_backend_failure_records_error_keeps_dirty_releases_lock(
    store: QueueStore,
    repo: Path,
) -> None:
    def failing_backend(repo_arg: str, paths: list[str]) -> dict:
        raise RuntimeError("boom")

    result = enqueue(store, str(repo), failing_backend, autostart=False)
    assert result["drained_inline"] is True
    assert (store.get("last_error") or "").startswith("boom")
    assert int(store.get("dirty") or 0) >= 1  # pending signal preserved
    assert store.get("lock") is None  # lock NOT leaked


# ── lock hygiene ────────────────────────────────────────────────────────────


def test_release_with_foreign_token_is_noop(store: QueueStore) -> None:
    owner = store.acquire_lock()
    assert owner is not None
    store.release_lock("not-the-owner")
    assert store.get("lock") == owner
    store.release_lock(owner)
    assert store.get("lock") is None


def test_drain_never_leaks_lock_when_git_fails(store: QueueStore, tmp_path: Path) -> None:
    empty = tmp_path / "not_a_repo"
    empty.mkdir()
    token = store.acquire_lock()
    assert token is not None
    with pytest.raises(Exception):  # noqa: B017 — any failure mode
        drain(store, str(empty), RecordingBackend(), token=token)
    assert store.get("lock") is None


def test_autostart_thread_drains_asynchronously(store: QueueStore, repo: Path) -> None:
    backend = RecordingBackend()
    result = enqueue(store, str(repo), backend, autostart=True)
    assert result["status"] == "accepted"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and store.get("snapshot") is None:
        time.sleep(0.05)
    assert store.get("snapshot") == head_sha(str(repo))
