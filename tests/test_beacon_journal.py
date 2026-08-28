# Copyright (c) 2026 Matt Creigh. Released under the MIT License.
# SPDX-License-Identifier: MIT
"""Tests for beagle.beacon.journal — the write-behind durability layer.

See plans/beagle-beacon-coordination.xml WP-6: decision D-12, invariants
I-6/I-7, hard constraint C-03.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from pathlib import Path

import fakeredis
import pytest
import redis

from beagle.beacon.journal import Journal, is_board_class, replay
from beagle.beacon.keys import resolve_paths
from beagle.beacon.server import BeaconServer

# Canonical [coord] values (schema.py CoordConfig). The Journal requires them
# explicitly since CD-1 of plans/beagle-config-defaults-abstraction.xml —
# durability numbers must not have a second source of truth in code.
J_MAX_BYTES = 1073741824
J_MAX_FILES = 30
_J_FSYNC_S = 2.0


@pytest.fixture
def running_server(tmp_path: Path):
    server = BeaconServer(tmp_path)
    server.start()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    from beagle.beacon.spawn import is_live

    assert is_live(server.paths, connect_timeout_s=2.0)
    yield server
    server.stop()
    thread.join(timeout=5)


class TestIsBoardClass:
    """I-6: the exact prefix classification that decides what survives a crash."""

    @pytest.mark.parametrize(
        "key",
        ["issue:BGL-1", "comment:BGL-1", "transition:BGL-1", "issue:index", "issue:by_state:open"],
    )
    def test_board_keys_are_board_class(self, key: str) -> None:
        assert is_board_class(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            "agent:abc",
            "agent:list",
            "lock:deadbeef",
            "plan:active",
            "beacon:teardown",
            "chan:xyz",
            "chan:index:abc",
            "event",
            "issue:claim:BGL-1",  # starts with issue: but is presence-class
        ],
    )
    def test_presence_keys_are_not_board_class(self, key: str) -> None:
        assert is_board_class(key) is False


class TestSecretRejection:
    """C-03: a key matching the secret pattern must never reach the journal."""

    @pytest.mark.parametrize(
        "key", ["issue:secret_token", "comment:api_key_1", "transition:password_reset"]
    )
    def test_secret_shaped_board_key_is_rejected(self, tmp_path: Path, key: str) -> None:
        journal = Journal(
            resolve_paths(tmp_path),
            max_bytes=J_MAX_BYTES,
            max_files=J_MAX_FILES,
            fsync_interval_s=_J_FSYNC_S,
        )
        try:
            with pytest.raises(ValueError, match="secret"):
                journal.record("hset", key, {"mapping": {"f": "v"}})
        finally:
            journal.stop()


class TestFsyncIsTimerBound:
    """D-12: fsync is timer-driven, never once per mutation."""

    def test_100_records_do_not_cause_100_fsyncs(self, tmp_path: Path) -> None:
        journal = Journal(  # fsync never fires in-test
            resolve_paths(tmp_path),
            max_bytes=J_MAX_BYTES,
            max_files=J_MAX_FILES,
            fsync_interval_s=3600,
        )
        try:
            for i in range(100):
                journal.record("hset", f"issue:BGL-{i}", {"mapping": {"title": f"t{i}"}})
            assert journal.fsync_count == 0, (
                "record() must never fsync directly — only the timer thread "
                "(or an explicit flush()) may"
            )
            journal.flush()
            assert journal.fsync_count == 1, "one explicit flush() must be exactly one fsync"
        finally:
            journal.stop()

    def test_presence_class_records_never_touch_the_file(self, tmp_path: Path) -> None:
        journal = Journal(
            resolve_paths(tmp_path),
            max_bytes=J_MAX_BYTES,
            max_files=J_MAX_FILES,
            fsync_interval_s=_J_FSYNC_S,
        )
        try:
            journal.record("hset", "agent:abc", {"mapping": {"phase": "x"}})
            journal.flush()
            assert journal.fsync_count == 0, "a no-op record() must never dirty the journal"
        finally:
            journal.stop()


class TestRotation:
    def test_rotates_at_max_bytes_and_keeps_max_files(self, tmp_path: Path) -> None:
        journal = Journal(
            resolve_paths(tmp_path),
            max_bytes=200,
            max_files=2,
            fsync_interval_s=_J_FSYNC_S,
        )
        try:
            for i in range(60):
                journal.record("hset", f"issue:BGL-{i}", {"mapping": {"title": "x" * 20}})
            journal_dir = resolve_paths(tmp_path).base_dir / "journal"
            files = sorted(journal_dir.glob("journal-*.jsonl"))
            assert len(files) <= 2, f"expected at most 2 files, found {len(files)}"
            assert len(files) >= 2, "60 records at 200 bytes/rotation must have rotated"
        finally:
            journal.stop()


class TestReplayDurability:
    """D-12's whole point: a crash without clean detach must not lose the board."""

    def test_board_records_survive_a_crash_and_restart(self, running_server: BeaconServer) -> None:
        client = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        journal = Journal(
            running_server.paths,
            max_bytes=J_MAX_BYTES,
            max_files=J_MAX_FILES,
            fsync_interval_s=_J_FSYNC_S,
        )
        journal.record("hset", "issue:BGL-1", {"mapping": {"title": "durable", "state": "open"}})
        journal.record("sadd", "issue:by_state:open", {"values": ["BGL-1"]})
        journal.flush()
        journal.stop()

        # Simulate a crash: no clean shutdown of anything, just a fresh
        # store (a brand-new fakeredis instance has none of this state).
        fresh_client = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        fresh_client.flushall()
        assert fresh_client.hgetall("issue:BGL-1") == {}

        n = replay(fresh_client, running_server.paths)

        assert n == 2
        assert fresh_client.hgetall("issue:BGL-1") == {"title": "durable", "state": "open"}
        assert fresh_client.smembers("issue:by_state:open") == {"BGL-1"}
        client.close()
        fresh_client.close()

    def test_presence_class_keys_never_replay_i6(self, running_server: BeaconServer) -> None:
        """The most dangerous thing to get wrong in this package."""
        client = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        journal = Journal(
            running_server.paths,
            max_bytes=J_MAX_BYTES,
            max_files=J_MAX_FILES,
            fsync_interval_s=_J_FSYNC_S,
        )
        # An attacker (or a bug) trying to smuggle a presence-class write
        # through the journal API directly must simply be ignored — record()
        # itself no-ops for a non-board key.
        journal.record("hset", "agent:phantom", {"mapping": {"phase": "ghost"}})
        journal.record("hset", "lock:deadbeef", {"mapping": {}})
        journal.flush()
        journal.stop()

        assert not client.exists("agent:phantom")
        assert not client.exists("lock:deadbeef")

        n = replay(client, running_server.paths)

        assert n == 0
        assert not client.exists("agent:phantom"), "a phantom agent appeared after replay (I-6)"
        assert not client.exists("lock:deadbeef"), "a stale lock appeared after replay (I-6)"
        client.close()

    def test_a_hand_crafted_presence_key_in_the_journal_file_is_still_filtered(
        self, running_server: BeaconServer
    ) -> None:
        """Defence in depth: replay must not trust the file's own contents."""
        import json

        client = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        journal_dir = running_server.paths.base_dir / "journal"
        journal_dir.mkdir(parents=True, exist_ok=True)
        forged = journal_dir / "journal-0.jsonl"
        forged.write_text(
            json.dumps({"op": "hset", "key": "agent:forged", "args": {"mapping": {"x": "y"}}})
            + "\n",
            encoding="utf-8",
        )

        n = replay(client, running_server.paths)

        assert n == 0
        assert not client.exists("agent:forged")
        client.close()


class TestNoTTLOnBoardKeys:
    """I-7: no board record ever carries a TTL."""

    def test_replayed_board_keys_have_no_ttl(self, running_server: BeaconServer) -> None:
        client = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        journal = Journal(
            running_server.paths,
            max_bytes=J_MAX_BYTES,
            max_files=J_MAX_FILES,
            fsync_interval_s=_J_FSYNC_S,
        )
        journal.record("hset", "issue:BGL-2", {"mapping": {"title": "x"}})
        journal.record("lpush", "comment:BGL-2", {"values": ["hello"]})
        journal.record("lpush", "transition:BGL-2", {"values": ["open->done"]})
        journal.flush()
        journal.stop()

        replay(client, running_server.paths)

        assert client.ttl("issue:BGL-2") == -1
        assert client.ttl("comment:BGL-2") == -1
        assert client.ttl("transition:BGL-2") == -1
        client.close()

    def test_record_rejects_unreplayable_op(self, tmp_path: Path) -> None:
        journal = Journal(
            resolve_paths(tmp_path),
            max_bytes=J_MAX_BYTES,
            max_files=J_MAX_FILES,
            fsync_interval_s=_J_FSYNC_S,
        )
        try:
            with pytest.raises(ValueError, match="unreplayable"):
                journal.record("expire", "issue:BGL-3", {"seconds": 60})
        finally:
            journal.stop()


class TestJournalDurabilityHardening:
    """E-1..E-4 regression gates from docs/audits/release_readiness_code_audit_2026-08-22.md."""

    @staticmethod
    def _make(paths, **kw):
        opts: dict = {
            "max_bytes": J_MAX_BYTES,
            "max_files": J_MAX_FILES,
            "fsync_interval_s": _J_FSYNC_S,
        }
        opts.update(kw)
        return Journal(paths, **opts)

    def test_flush_after_stop_is_a_noop(self, tmp_path: Path) -> None:
        """E-1: post-close flush neither raises nor resurrects state."""
        j = self._make(resolve_paths(tmp_path))
        j.record("hset", "issue:E1", {"mapping": {"a": "b"}})
        j.stop()
        j.flush()  # must not raise ValueError/AttributeError
        assert j.fsync_count >= 1

    def test_stop_is_idempotent_and_record_rejects_after_close(self, tmp_path: Path) -> None:
        """E-1: stop() twice is fine; writes after teardown fail LOUDLY."""
        j = self._make(resolve_paths(tmp_path))
        j.record("hset", "issue:E1b", {"mapping": {"a": "b"}})
        j.stop()
        j.stop()  # second call is a no-op
        with pytest.raises(RuntimeError, match="closed"):
            j.record("hset", "issue:E1b", {"mapping": {"a": "c"}})

    def test_concurrent_flush_and_stop_race(self, tmp_path: Path) -> None:
        """E-1: flush racing stop across 100 cycles never observes a bad handle."""
        for _ in range(100):
            j = self._make(resolve_paths(tmp_path))
            j.record("hset", "issue:race", {"mapping": {"n": "1"}})
            errors: list[BaseException] = []

            def flusher(j=j, errors=errors) -> None:
                try:
                    while not j._stop.is_set():
                        j.flush()
                except BaseException as exc:  # noqa: BLE001 - captured deliberately
                    errors.append(exc)

            t = threading.Thread(target=flusher, daemon=True)
            t.start()
            j.stop()
            t.join(timeout=5)
            assert not errors

    def test_fsync_failure_is_counted_raised_then_recoverable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """E-2: a failed fsync is surfaced, counted, and retryable."""
        import beagle.beacon.journal as journal_module

        j = self._make(resolve_paths(tmp_path))
        j.record("hset", "issue:E2", {"mapping": {"a": "b"}})

        def boom(*a, **kw):
            raise OSError("disk gone")

        monkeypatch.setattr(journal_module.os, "fsync", boom)
        with pytest.raises(OSError, match="disk gone"):
            j.flush()
        assert j.fsync_error_count == 1
        assert j.last_fsync_error_s is not None
        assert j._dirty  # durability NOT confirmed for the pending record

        monkeypatch.undo()
        j.flush()
        assert j.fsync_count == 1
        assert j.fsync_error_count == 1  # failure history is kept
        assert not j._dirty
        j.stop()

    def test_fsync_timer_survives_transient_failures(self, tmp_path: Path) -> None:
        """E-2: the timer thread stays alive through failures and recovers."""
        import beagle.beacon.journal as journal_module

        j = self._make(resolve_paths(tmp_path), fsync_interval_s=0.05)
        j.record("hset", "issue:E2b", {"mapping": {"a": "b"}})

        real_fsync = journal_module.os.fsync
        state = {"failures": 3}

        def flaky(fd):
            if state["failures"] > 0:
                state["failures"] -= 1
                raise OSError("transient")
            return real_fsync(fd)

        monkeypatch_jmodule = journal_module.os
        saved = monkeypatch_jmodule.fsync
        monkeypatch_jmodule.fsync = flaky
        try:
            j.start()
            for _ in range(200):
                if j.fsync_count >= 1:
                    break
                threading.Event().wait(0.05)
            assert j.fsync_count >= 1, "timer never recovered after transient failures"
            assert j.fsync_error_count >= 3
            assert j._thread is not None and j._thread.is_alive()
        finally:
            monkeypatch_jmodule.fsync = saved
            j.stop()

    def test_replay_skips_drifted_records_without_aborting(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """E-4: schema-drifted but valid JSON skips with a warning."""
        import json as _json

        paths = resolve_paths(tmp_path)
        jdir = paths.base_dir / "journal"
        jdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lines = [
            _json.dumps({"op": "hset", "key": "issue:ok", "args": {"mapping": {"t": "v"}}}),
            _json.dumps({"op": "hset", "key": "issue:x", "args": {}}),  # missing mapping
            _json.dumps({"op": "bogus", "key": "issue:y", "args": {}}),  # unknown op
            "{truncated",
        ]
        (jdir / "journal-0.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        client = fakeredis.FakeRedis(decode_responses=True)
        with caplog.at_level("WARNING"):
            n = replay(client, paths)
        assert n == 1
        assert client.hgetall("issue:ok") == {"t": "v"}
        assert "skipping drifted record" in caplog.text or "malformed" in caplog.text

    def test_replay_streams_instead_of_read_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """E-3: replay must stream line-by-line, never load a rotation whole."""
        import json as _json
        from pathlib import Path as _Path

        paths = resolve_paths(tmp_path)
        jdir = paths.base_dir / "journal"
        jdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (jdir / "journal-0.jsonl").write_text(
            _json.dumps({"op": "sadd", "key": "issue:by_state:open", "args": {"values": ["Z"]}})
            + "\n",
            encoding="utf-8",
        )

        original_read_text = _Path.read_text

        def forbidden(self: _Path, *a, **kw):
            # Tripwire scoped to journal rotation files only: Path.read_text is
            # process-global, and unrelated library internals (e.g. redis-py's
            # importlib.metadata METADATA probe during lazy connection init)
            # legitimately use it.
            if self.suffix == ".jsonl":
                raise AssertionError("replay regressed to whole-file read_text (E-3)")
            return original_read_text(self, *a, **kw)

        monkeypatch.setattr(_Path, "read_text", forbidden)
        try:
            client = fakeredis.FakeRedis(decode_responses=True)
            n = replay(client, paths)
            assert n == 1
            assert client.smembers("issue:by_state:open") == {"Z"}
        finally:
            monkeypatch.setattr(_Path, "read_text", original_read_text)


class TestJournalStatusSurfacing:
    """Audit A2: fsync health must be operator-visible without an in-process owner."""

    def test_fsync_failure_publishes_error_status_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed flush cycle lands state=error with counters in the status file."""
        paths = resolve_paths(tmp_path)
        j = Journal(paths, max_bytes=10_000, max_files=3, fsync_interval_s=600.0)
        j.start()
        try:
            j.record("sadd", "issue:by_state:open", {"values": ["A"]})
            j.flush()  # ok cycle
            assert (paths.base_dir / "journal" / "journal_status.json").exists()
            j.stop()

            # Inject a persistent failure and flush again.
            j2 = Journal(paths, max_bytes=10_000, max_files=3, fsync_interval_s=600.0)
            j2.start()
            j2.record("sadd", "issue:by_state:open", {"values": ["B"]})

            # Inject at the fsync syscall so the REAL _flush_locked error
            # path (counters + status publish + re-raise) executes.
            def boom(fd: int) -> None:
                raise OSError("injected EIO")

            monkeypatch.setattr(os, "fsync", boom)
            with pytest.raises(OSError):
                j2.flush()
            payload = json.loads((paths.base_dir / "journal" / "journal_status.json").read_text())
            assert payload["state"] == "error"
            assert payload["fsync_error_count"] >= 1
            assert payload["last_fsync_error_s"] > 0
        finally:
            with contextlib.suppress(Exception):
                j2.stop()

    def test_status_file_staleness_is_visible_not_absent(self, tmp_path: Path) -> None:
        """Every flush outcome republishes the file; updated_at tracks freshness."""
        paths = resolve_paths(tmp_path)
        j = Journal(paths, max_bytes=10_000, max_files=3, fsync_interval_s=600.0)
        j.start()
        try:
            j.record("sadd", "issue:by_state:open", {"values": ["C"]})
            j.flush()
            first = json.loads((paths.base_dir / "journal" / "journal_status.json").read_text())
            j.record("sadd", "issue:by_state:open", {"values": ["D"]})
            j.flush()
            second = json.loads((paths.base_dir / "journal" / "journal_status.json").read_text())
            assert second["updated_at_s"] >= first["updated_at_s"]
            assert second["state"] == "ok"
            assert second["fsync_count"] == first["fsync_count"] + 1
        finally:
            j.stop()
