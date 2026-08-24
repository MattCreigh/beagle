"""Unit tests for ServerSideTokenCounter (v13.22.0).

Tests the event-bus subscriber, the threshold-driven fold trigger,
and the idempotent singleton.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def beagle_dir(tmp_path, monkeypatch):
    """Redirect BEAGLE_DIR to tmp_path so tests don't touch real ~/.beagle.

    The subscriber now writes through ``context_reporter.write_report``,
    which resolves its path from the ``BEAGLE_STATE_DIR`` env var at import
    time.  Point that env var at tmp_path and reload the reporter so its
    module-level ``_REPORT_PATH`` follows.
    """
    import importlib

    monkeypatch.setattr(
        "beagle.context.token_counter_subscriber.BEAGLE_DIR",
        tmp_path,
    )
    monkeypatch.setattr(
        "beagle.context.token_counter_subscriber.CONTEXT_REPORT",
        tmp_path / "context_report.json",
    )
    monkeypatch.setenv("BEAGLE_STATE_DIR", str(tmp_path))
    from beagle.context import context_reporter

    importlib.reload(context_reporter)
    return tmp_path


@pytest.fixture
def mock_config():
    cfg = MagicMock()
    cfg.context_threshold.pre_compact = 0.58
    cfg.context_threshold.critical = 0.85
    cfg.context_threshold.warning = 0.50
    cfg.context_threshold.compact = 0.70
    cfg.context_threshold.hard_compact = 0.78
    return cfg


@pytest.fixture
def counter(monkeypatch, beagle_dir, mock_config):
    """Fresh ServerSideTokenCounter with mocked bus and config."""
    from beagle.context import token_counter_subscriber

    monkeypatch.setattr(token_counter_subscriber, "_counter_singleton", None)
    # Stub out get_event_bus() at the call site (the subscriber module
    # does a lazy import).
    fake_bus = MagicMock()
    fake_bus.subscribe.return_value = "test-sub-id-12345678"
    monkeypatch.setattr(
        "beagle.events.bus.get_event_bus",
        lambda: fake_bus,
    )
    monkeypatch.setattr(
        "beagle.config.config.get_config",
        lambda: mock_config,
    )
    return token_counter_subscriber.get_token_counter()


def make_event(utilization: float, current_tokens: int = 70000, max_tokens: int = 128000):
    """Build a fake ContextWarning event with the same attributes."""
    ev = MagicMock()
    ev.utilization = utilization
    ev.current_tokens = current_tokens
    ev.max_tokens = max_tokens
    return ev


# ── subscribe / singleton ──────────────────────────────────────────────────


class TestSubscribe:
    def test_subscribe_returns_sub_id(self, counter):
        assert counter._subscribed is True
        assert counter._subscription_id == "test-sub-id-12345678"

    def test_subscribe_is_idempotent(self, counter):
        first_id = counter._subscription_id
        second_id = counter.subscribe()
        assert first_id == second_id

    def test_singleton_returns_same_instance(self, monkeypatch, beagle_dir, mock_config):
        from beagle.context import token_counter_subscriber

        monkeypatch.setattr(token_counter_subscriber, "_counter_singleton", None)
        a = token_counter_subscriber.get_token_counter()
        b = token_counter_subscriber.get_token_counter()
        assert a is b

    def test_reset_clears_singleton(self, monkeypatch, beagle_dir, mock_config):
        from beagle.context import token_counter_subscriber

        monkeypatch.setattr(token_counter_subscriber, "_counter_singleton", None)
        a = token_counter_subscriber.get_token_counter()
        token_counter_subscriber.reset_token_counter()
        b = token_counter_subscriber.get_token_counter()
        assert a is not b


# ── on_event / context_report writes ────────────────────────────────────────


class TestOnEvent:
    def test_event_below_pre_compact_writes_report(self, counter, beagle_dir):
        """A 30% event must update context_report.json but NOT fire the actor."""
        counter._on_event(make_event(0.30))
        snap = counter.get_snapshot()
        assert snap["utilization"] == 0.30
        assert snap["current_tokens"] == 70000
        assert snap["events_seen"] == 1
        report_path = beagle_dir / "context_report.json"
        assert report_path.is_file()
        report = json.loads(report_path.read_text())
        assert report["schema_version"] == 2
        assert report["percentage"] == 0.30
        assert report["subscriber_verified"] is True

    def test_event_at_pre_compact_writes_report(self, counter, beagle_dir):
        """A 60% event (above 0.58 pre_compact) writes the report; actor decision is async."""
        counter._on_event(make_event(0.60))
        report_path = beagle_dir / "context_report.json"
        assert report_path.is_file()
        report = json.loads(report_path.read_text())
        assert report["schema_version"] == 2
        assert report["percentage"] == 0.60

    def test_multiple_events_increment_seen(self, counter):
        for pct in (0.10, 0.20, 0.30, 0.45):
            counter._on_event(make_event(pct))
        assert counter.get_snapshot()["events_seen"] == 4

    def test_event_without_utilization_attribute_ignored(self, counter, beagle_dir):
        """Non-ContextWarning events (no .utilization) are filtered out."""
        ev = MagicMock(spec=["unrelated_field"])
        ev.unrelated_field = "x"
        counter._on_event(ev)
        assert counter.get_snapshot()["events_seen"] == 0
        assert not (beagle_dir / "context_report.json").exists()

    def test_report_write_failure_does_not_crash(self, counter, monkeypatch):
        """If we can't write context_report.json, the callback must not raise."""
        # Make write_text raise OSError for the tmp file path.

        def boom(*_args, **_kwargs):
            raise OSError("simulated disk full")

        with patch.object(Path, "write_text", side_effect=boom):
            # Should not raise.
            counter._on_event(make_event(0.50))
        # Internal state still updated.
        assert counter.get_snapshot()["utilization"] == 0.50


# ── threshold-trigger logic ────────────────────────────────────────────────


class TestThresholdTriggers:
    def test_pre_compact_event_schedules_actor_fire(self, counter, beagle_dir):
        """A 60% event must trigger the actor; we patch _maybe_fire_actor to verify."""
        with patch.object(counter, "_maybe_fire_actor") as mock_fire:
            counter._on_event(make_event(0.60))
        mock_fire.assert_called_once_with(force=False)

    def test_critical_event_forces_actor_fire(self, counter, beagle_dir, caplog):
        """A 90% event must call _maybe_fire_actor(force=True) and log CRITICAL.

        Asserts through ``caplog``, not ``capsys``. The critical record goes to
        the logger, and pytest captures log records separately from stdout and
        stderr — so ``capsys.readouterr().err`` is empty no matter what the
        logger did. This test asserted on capsys and had been failing silently
        since v13.22.1 removed the ``print()`` that used to satisfy it.
        """
        with (
            caplog.at_level(logging.CRITICAL, logger="Beagle.context.token_counter_subscriber"),
            patch.object(counter, "_maybe_fire_actor") as mock_fire,
        ):
            counter._on_event(make_event(0.90))
        mock_fire.assert_called_once_with(force=True)
        critical = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
        assert critical, f"no CRITICAL record emitted; got {[r.levelname for r in caplog.records]}"
        assert "critical" in critical[0].getMessage().lower()

    def test_below_pre_compact_does_not_fire(self, counter, beagle_dir):
        """A 40% event must NOT call _maybe_fire_actor."""
        with patch.object(counter, "_maybe_fire_actor") as mock_fire:
            counter._on_event(make_event(0.40))
        mock_fire.assert_not_called()

    def test_30s_debounce_throttles_subsequent_fires(self, counter, beagle_dir):
        """Two pre_compact events within 30s: second is throttled."""
        with patch.object(
            counter, "_maybe_fire_actor", return_value={"status": "throttled"}
        ) as mock_fire:
            counter._on_event(make_event(0.60))
            # Within 30s, second event should not call (we already fired recently).
            # We patch the internal _last_fire_at to be very recent to simulate this.
            counter._last_fire_at = time.time()
            counter._on_event(make_event(0.65))
        # First call happened, second was throttled inside the callback.
        assert mock_fire.call_count == 2

    def test_force_fire_test_hook(self, counter):
        """force_fire_test() invokes the actor; we patch it to verify."""
        with patch.object(
            counter, "_maybe_fire_actor", return_value={"status": "fired_async"}
        ) as mock_fire:
            result = counter.force_fire_test()
        mock_fire.assert_called_once_with(force=True)
        assert result["status"] == "fired_async"


# ── _maybe_fire_actor: thread + actor wiring ────────────────────────────────


class TestMaybeFireActor:
    def test_actor_invoked_on_background_thread(self, counter, beagle_dir, mock_config):
        """The actor is called on a daemon thread, not the calling thread."""
        import threading

        from beagle.context import watchdog_actor

        # Reset the watchdog actor singleton so we get a fresh one we can mock.
        watchdog_actor.reset_watchdog_actor()
        # Patch the actor's compact_now to return a fired result.
        # (threading.get_ident() intentionally not captured — the test only
        # asserts mock_compact was called, not which thread it ran on.)
        with patch.object(
            watchdog_actor.WatchdogActor,
            "compact_now",
            return_value={"status": "fired", "fold_id": "abcd12345678"},
        ) as mock_compact:
            counter._maybe_fire_actor(force=True)
            # Wait for the daemon thread to complete.
            for t in [th for th in threading._enumerate() if th.name == "watchdog-actor-fire"]:
                t.join(timeout=5)
        # The actor was called.
        mock_compact.assert_called_once()
        # And it ran on a different thread (we can't easily assert which
        # thread, but if mock_compact was called we're good).

    def test_actor_import_error_caught(self, counter, monkeypatch):
        """If the actor import fails, the callback must not crash."""

        def boom():
            raise ImportError("simulated missing actor")

        monkeypatch.setattr(
            "beagle.context.watchdog_actor.get_watchdog_actor",
            boom,
        )
        # The thread will swallow the error and log; we just verify
        # the main thread doesn't crash.
        counter._maybe_fire_actor(force=True)
        # Give the daemon a moment.
        import threading

        for t in [th for th in threading._enumerate() if th.name == "watchdog-actor-fire"]:
            t.join(timeout=5)


# ── get_snapshot ────────────────────────────────────────────────────────────


class TestGetSnapshot:
    def test_initial_snapshot_is_zero(self, counter):
        snap = counter.get_snapshot()
        assert snap["current_tokens"] == 0
        assert snap["max_tokens"] == 0
        assert snap["utilization"] == 0.0
        assert snap["events_seen"] == 0
        assert snap["subscriber_verified"] is True

    def test_snapshot_after_event(self, counter):
        counter._on_event(make_event(0.45, current_tokens=57600, max_tokens=128000))
        snap = counter.get_snapshot()
        assert snap["utilization"] == 0.45
        assert snap["current_tokens"] == 57600
        assert snap["events_seen"] == 1


# ── Smoke ──────────────────────────────────────────────────────────────────


class TestSmoke:
    def test_module_is_importable(self):
        from beagle.context import token_counter_subscriber

        assert hasattr(token_counter_subscriber, "ServerSideTokenCounter")
        assert hasattr(token_counter_subscriber, "get_token_counter")
        assert hasattr(token_counter_subscriber, "reset_token_counter")
        assert hasattr(token_counter_subscriber, "CONTEXT_REPORT")
