"""Unit tests for WatchdogActor (v13.22.0).

Tests the threshold logic in should_fire() and the persistence logic
in record_compaction.  The fold itself (ContextMonitor.fold_and_surrender)
is mocked — testing the real fold would require a working tiktoken /
embedding model and is already covered by trigger.py tests.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add project root to path for imports.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def beagle_dir(tmp_path, monkeypatch):
    """Redirect BEAGLE_DIR to tmp_path so tests don't touch real ~/.beagle."""
    monkeypatch.setattr("beagle.context.watchdog_actor.BEAGLE_DIR", tmp_path)
    monkeypatch.setattr(
        "beagle.context.watchdog_actor.COMPACTION_STATE",
        tmp_path / "compaction_state.json",
    )
    monkeypatch.setattr(
        "beagle.context.watchdog_actor.CONTEXT_REPORT",
        tmp_path / "context_report.json",
    )
    return tmp_path


@pytest.fixture
def mock_config():
    """Mock get_config() with deterministic thresholds."""
    cfg = MagicMock()
    cfg.context_threshold.warning = 0.50
    cfg.context_threshold.pre_compact = 0.58
    cfg.context_threshold.compact = 0.70
    cfg.context_threshold.hard_compact = 0.78
    cfg.context_threshold.critical = 0.85
    return cfg


@pytest.fixture
def actor(monkeypatch, beagle_dir, mock_config):
    """Fresh WatchdogActor with patched get_config."""
    monkeypatch.setattr("beagle.context.watchdog_actor._actor_singleton", None)
    # Patch get_config inside the actor's namespace.
    monkeypatch.setattr(
        "beagle.config.config.get_config",
        lambda: mock_config,
    )
    from beagle.context.watchdog_actor import (
        get_watchdog_actor,
    )

    return get_watchdog_actor()


# ── should_fire tests ───────────────────────────────────────────────────────


class TestShouldFire:
    """Pure-logic tests for the threshold decision."""

    def test_fires_when_timer_elapsed_and_above_warning(self, actor, beagle_dir):
        """2h since last fold + 55% utilization → must fire."""
        state = {
            "last_compaction": time.time() - 7200,
            "last_fold_type": "sovereignty",
            "compaction_count": 1,
        }
        (beagle_dir / "compaction_state.json").write_text(json.dumps(state))
        should, reason = actor.should_fire(current_pct=0.55)
        assert should is True
        assert "timer elapsed" in reason.lower()

    def test_skips_when_recently_compacted(self, actor, beagle_dir):
        """30s since last fold + 60% → must skip (timer not elapsed)."""
        state = {
            "last_compaction": time.time() - 30,
            "last_fold_type": "sovereignty",
            "compaction_count": 5,
        }
        (beagle_dir / "compaction_state.json").write_text(json.dumps(state))
        should, reason = actor.should_fire(current_pct=0.60)
        assert should is False
        assert "recently compacted" in reason.lower()

    def test_skips_when_post_final_answer_fold(self, actor, beagle_dir):
        """30s since post-final-answer fold + 60% → must skip."""
        state = {
            "last_compaction": time.time() - 30,
            "last_fold_type": "post_final_answer",
            "compaction_count": 5,
        }
        (beagle_dir / "compaction_state.json").write_text(json.dumps(state))
        should, reason = actor.should_fire(current_pct=0.60)
        assert should is False
        assert "post_final_answer" in reason.lower() or "beagle fold just ran" in reason.lower()

    def test_fires_when_critical_even_if_recent(self, actor, beagle_dir):
        """30s since last fold + 90% → must fire (critical overrides)."""
        state = {
            "last_compaction": time.time() - 30,
            "last_fold_type": "post_final_answer",
            "compaction_count": 5,
        }
        (beagle_dir / "compaction_state.json").write_text(json.dumps(state))
        should, reason = actor.should_fire(current_pct=0.90)
        assert should is True
        assert "critical" in reason.lower()

    def test_skips_below_warning(self, actor, beagle_dir):
        """No recent fold + 30% → must skip (below warning)."""
        should, reason = actor.should_fire(current_pct=0.30)
        assert should is False
        assert "below warning" in reason.lower()

    def test_handles_missing_state(self, actor, beagle_dir):
        """No compaction_state.json + 55% → fires (no prior fold to honor)."""
        should, reason = actor.should_fire(current_pct=0.55)
        assert should is True
        assert "timer elapsed" in reason.lower()

    def test_handles_corrupt_state(self, actor, beagle_dir):
        """Corrupt JSON + 55% → must not crash, must fire."""
        (beagle_dir / "compaction_state.json").write_text("NOT VALID JSON {{{")
        should, _reason = actor.should_fire(current_pct=0.55)
        assert should is True

    def test_uses_context_report_when_pct_not_overridden(self, actor, beagle_dir):
        """should_fire() with no override reads context_report.json."""
        report = {
            "utilization": 0.55,
            "current_tokens": 70000,
            "max_tokens": 128000,
        }
        (beagle_dir / "context_report.json").write_text(json.dumps(report))
        # Set state to 2h ago so it would fire.
        (beagle_dir / "compaction_state.json").write_text(
            json.dumps({"last_compaction": time.time() - 7200, "last_fold_type": "x"})
        )
        should, _ = actor.should_fire()
        assert should is True


# ── compact_now tests ───────────────────────────────────────────────────────


class TestCompactNow:
    """Tests that the fold invocation is wired correctly."""

    def test_compact_now_skipped_when_should_fire_returns_false(self, actor, beagle_dir):
        """If should_fire says no, compact_now returns 'skipped' without calling fold."""
        # Force the should_fire path: recent fold, below critical.
        state = {
            "last_compaction": time.time() - 10,
            "last_fold_type": "post_final_answer",
            "compaction_count": 1,
        }
        (beagle_dir / "compaction_state.json").write_text(json.dumps(state))
        # Also seed context_report.json so should_fire reads current_pct=0.60.
        (beagle_dir / "context_report.json").write_text(
            json.dumps({"utilization": 0.60, "current_tokens": 76800, "max_tokens": 128000})
        )
        # Mock fold_and_surrender so we can assert it was NOT called.
        with patch("beagle.context.trigger.ContextMonitor") as mock_monitor_cls:
            mock_monitor_cls.return_value.fold_and_surrender.return_value = "deadbeef1234"
            result = actor.compact_now(force=False)
        assert result["status"] == "skipped"
        assert (
            "post_final_answer" in result["reason"].lower()
            or "recently compacted" in result["reason"].lower()
        )
        mock_monitor_cls.assert_not_called()

    def test_compact_now_force_bypasses_should_fire(self, actor, beagle_dir, mock_config):
        """force=True bypasses should_fire and calls the fold."""
        with patch("beagle.context.trigger.ContextMonitor") as mock_monitor_cls:
            mock_monitor = MagicMock()
            mock_monitor.fold_and_surrender.return_value = "abc123def456"
            mock_monitor_cls.return_value = mock_monitor
            result = actor.compact_now(force=True)
        assert result["status"] == "fired"
        assert result["fold_id"] == "abc123def456"
        mock_monitor_cls.assert_called_once()
        mock_monitor.fold_and_surrender.assert_called_once()

    def test_compact_now_records_fold_id_in_state(self, actor, beagle_dir, mock_config):
        """After firing, compaction_state.json has the new fold_id and type."""
        with patch("beagle.context.trigger.ContextMonitor") as mock_monitor_cls:
            mock_monitor = MagicMock()
            mock_monitor.fold_and_surrender.return_value = "feedface1234"
            mock_monitor_cls.return_value = mock_monitor
            result = actor.compact_now(force=True)
        assert result["status"] == "fired"
        state_path = beagle_dir / "compaction_state.json"
        assert state_path.is_file()
        state = json.loads(state_path.read_text())
        assert state["last_fold_id"] == "feedface1234"
        assert state["last_fold_type"] == "watchdog"
        assert "feedface1234" in json.dumps(state.get("history", []))

    def test_compact_now_handles_fold_returning_none(self, actor, beagle_dir, mock_config):
        """fold_and_surrender returning None (no accumulated context) is a no-op skip."""
        with patch("beagle.context.trigger.ContextMonitor") as mock_monitor_cls:
            mock_monitor = MagicMock()
            mock_monitor.fold_and_surrender.return_value = None
            mock_monitor_cls.return_value = mock_monitor
            result = actor.compact_now(force=True)
        assert result["status"] == "skipped"
        assert "no accumulated context" in result["reason"].lower()

    def test_compact_now_handles_fold_exception(self, actor, beagle_dir, mock_config):
        """Exception in fold is caught and reported, not raised."""
        with patch("beagle.context.trigger.ContextMonitor") as mock_monitor_cls:
            mock_monitor = MagicMock()
            mock_monitor.fold_and_surrender.side_effect = RuntimeError("boom")
            mock_monitor_cls.return_value = mock_monitor
            result = actor.compact_now(force=True)
        assert result["status"] == "error"
        assert "boom" in result["reason"]

    def test_compact_now_handles_context_trigger_import_error(
        self, actor, beagle_dir, mock_config, monkeypatch
    ):
        """If context.trigger import fails, return error, don't crash."""

        # Patch the lazy import to raise ImportError. The compact_now
        # does `from beagle.context.trigger import ...`
        # so we make the module unimportable.
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "beagle.context.trigger":
                raise ImportError("simulated missing trigger module")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        result = actor.compact_now(force=True)
        assert result["status"] == "error"
        assert "import" in result["reason"].lower()


# ── get_status tests ────────────────────────────────────────────────────────


class TestGetStatus:
    """Tests for the --status mode payload."""

    def test_get_status_returns_expected_keys(self, actor, beagle_dir):
        status = actor.get_status()
        assert "now" in status
        assert "current_pct" in status
        assert "thresholds" in status
        assert "should_fire" in status
        assert "reason" in status
        assert status["thresholds"]["warning"] == 0.50
        assert status["thresholds"]["critical"] == 0.85

    def test_get_status_handles_missing_state(self, actor, beagle_dir):
        status = actor.get_status()
        assert status["compaction_count"] == 0
        assert status["last_fold_type"] == "unknown"

    def test_get_status_includes_last_outcome(self, actor, beagle_dir, mock_config):
        """After a compact_now, get_status should include the outcome."""
        with patch("beagle.context.trigger.ContextMonitor") as mock_monitor_cls:
            mock_monitor = MagicMock()
            mock_monitor.fold_and_surrender.return_value = "abc123def456"
            mock_monitor_cls.return_value = mock_monitor
            actor.compact_now(force=True)
        status = actor.get_status()
        assert status["last_outcome"]["fold_id"] == "abc123def456"


# ── Singleton tests ─────────────────────────────────────────────────────────


class TestSingleton:
    """Verify the singleton accessor behaves correctly."""

    def test_get_watchdog_actor_returns_same_instance(self, monkeypatch, beagle_dir, mock_config):
        from beagle.context import watchdog_actor

        monkeypatch.setattr(watchdog_actor, "_actor_singleton", None)
        a = watchdog_actor.get_watchdog_actor()
        b = watchdog_actor.get_watchdog_actor()
        assert a is b

    def test_reset_clears_singleton(self, monkeypatch, beagle_dir, mock_config):
        from beagle.context import watchdog_actor

        monkeypatch.setattr(watchdog_actor, "_actor_singleton", None)
        a = watchdog_actor.get_watchdog_actor()
        watchdog_actor.reset_watchdog_actor()
        b = watchdog_actor.get_watchdog_actor()
        assert a is not b


# ── Smoke / import test ─────────────────────────────────────────────────────


class TestSmoke:
    def test_module_is_importable(self):
        """The whole module loads without error."""
        from beagle.context import watchdog_actor

        assert hasattr(watchdog_actor, "WatchdogActor")
        assert hasattr(watchdog_actor, "get_watchdog_actor")
        assert hasattr(watchdog_actor, "reset_watchdog_actor")
        assert hasattr(watchdog_actor, "COMPACTION_STATE")
        assert hasattr(watchdog_actor, "CONTEXT_REPORT")
