"""Tests for beagle.daemon modules — scheduler, triggers, watcher, daemon creation."""

from __future__ import annotations

import tempfile
from pathlib import Path

from beagle.daemon.scheduler import DaemonScheduler
from beagle.daemon.triggers import Trigger, TriggerMatcher
from beagle.daemon.watcher import ChangeSet, Watcher

# ── DaemonScheduler ────────────────────────────────────────────────────────


class TestDaemonScheduler:
    """DaemonScheduler creation and budget logic."""

    def test_default_creation(self):
        scheduler = DaemonScheduler()
        assert scheduler.tick_interval == 30
        assert scheduler.blocking_budget == 15
        assert scheduler.idle_threshold == 300
        assert scheduler.max_daily_cost_usd == 5.0

    def test_custom_creation(self):
        scheduler = DaemonScheduler(
            tick_interval=60,
            blocking_budget=30,
            idle_threshold=600,
            max_daily_cost_usd=10.0,
        )
        assert scheduler.tick_interval == 60
        assert scheduler.blocking_budget == 30
        assert scheduler.idle_threshold == 600
        assert scheduler.max_daily_cost_usd == 10.0

    def test_can_run_now_within_budget(self):
        scheduler = DaemonScheduler(blocking_budget=15)
        assert scheduler.can_run_now(10) is True

    def test_can_run_now_over_budget(self):
        scheduler = DaemonScheduler(blocking_budget=15)
        assert scheduler.can_run_now(20) is False

    def test_can_run_now_exact_budget(self):
        scheduler = DaemonScheduler(blocking_budget=15)
        assert scheduler.can_run_now(15) is True

    def test_increment_cost(self):
        scheduler = DaemonScheduler()
        scheduler.increment_cost(1.5)
        assert scheduler.daily_cost == 1.5

    def test_increment_cost_accumulates(self):
        scheduler = DaemonScheduler()
        scheduler.increment_cost(2.0)
        scheduler.increment_cost(3.0)
        assert scheduler.daily_cost == 5.0

    def test_is_over_budget_false_initially(self):
        scheduler = DaemonScheduler(max_daily_cost_usd=5.0)
        assert scheduler.is_over_budget() is False

    def test_is_over_budget_true_after_cost(self):
        scheduler = DaemonScheduler(max_daily_cost_usd=5.0)
        scheduler.increment_cost(5.0)
        assert scheduler.is_over_budget() is True

    def test_is_over_budget_boundary(self):
        scheduler = DaemonScheduler(max_daily_cost_usd=5.0)
        scheduler.increment_cost(4.99)
        assert scheduler.is_over_budget() is False
        scheduler.increment_cost(0.01)
        assert scheduler.is_over_budget() is True


# ── Trigger and TriggerMatcher ─────────────────────────────────────────────


class TestTrigger:
    """Trigger dataclass creation."""

    def test_trigger_creation_defaults(self):
        trigger = Trigger(name="test_trigger", file_patterns=["*.py"], workflow="audit")
        assert trigger.name == "test_trigger"
        assert trigger.file_patterns == ["*.py"]
        assert trigger.workflow == "audit"
        assert trigger.mode == "audit"
        assert trigger.budget == 2.0
        assert trigger.cooldown_minutes == 60

    def test_trigger_custom_params(self):
        trigger = Trigger(
            name="custom",
            file_patterns=["*.js"],
            workflow="security",
            mode="auto",
            budget=5.0,
            cooldown_minutes=30,
        )
        assert trigger.mode == "auto"
        assert trigger.budget == 5.0
        assert trigger.cooldown_minutes == 30


class TestTriggerMatcher:
    """TriggerMatcher pattern matching logic."""

    def test_default_triggers_exist(self):
        matcher = TriggerMatcher()
        assert len(matcher.triggers) >= 2

    def test_match_python_files(self):
        matcher = TriggerMatcher()
        changes = ChangeSet(changed_files=["src/main.py"])
        matched = matcher.match(changes)
        # Default triggers include security_check for *.py
        assert len(matched) >= 1
        assert any(t.name == "security_check" for t in matched)

    def test_match_no_changes(self):
        matcher = TriggerMatcher()
        changes = ChangeSet()
        matched = matcher.match(changes)
        assert matched == []

    def test_match_custom_trigger(self):
        custom = Trigger(
            name="react_check",
            file_patterns=["*.jsx", "*.tsx"],
            workflow="audit",
        )
        matcher = TriggerMatcher(triggers=[custom])
        changes = ChangeSet(changed_files=["src/App.tsx"])
        matched = matcher.match(changes)
        assert len(matched) == 1
        assert matched[0].name == "react_check"

    def test_no_match_for_unrelated_files(self):
        custom = Trigger(
            name="rust_check",
            file_patterns=["*.rs"],
            workflow="security",
        )
        matcher = TriggerMatcher(triggers=[custom])
        changes = ChangeSet(changed_files=["src/main.py"])
        matched = matcher.match(changes)
        assert matched == []


# ── ChangeSet ──────────────────────────────────────────────────────────────


class TestChangeSet:
    """ChangeSet dataclass and utility methods."""

    def test_empty_changeset_no_changes(self):
        cs = ChangeSet()
        assert cs.has_changes is False

    def test_changeset_with_changed_files(self):
        cs = ChangeSet(changed_files=["a.py", "b.py"])
        assert cs.has_changes is True

    def test_changeset_with_new_files(self):
        cs = ChangeSet(new_files=["c.py"])
        assert cs.has_changes is True

    def test_changeset_with_deleted_files(self):
        cs = ChangeSet(deleted_files=["d.py"])
        assert cs.has_changes is True

    def test_affected_modules(self):
        cs = ChangeSet(changed_files=["module_a/file.py"], new_files=["module_b/new.py"])
        modules = cs.affected_modules()
        assert "module_a" in modules
        assert "module_b" in modules

    def test_changeset_with_commit_info(self):
        cs = ChangeSet(commit_hash="abc123", commit_message="fix bug")
        assert cs.commit_hash == "abc123"
        assert cs.commit_message == "fix bug"


# ── Watcher ────────────────────────────────────────────────────────────────


class TestWatcher:
    """Watcher creation (git operations are tested via integration)."""

    def test_watcher_creation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            watcher = Watcher(tmpdir)
            assert watcher.workspace_root == Path(tmpdir)

    def test_watcher_check_returns_changeset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            watcher = Watcher(tmpdir)
            result = watcher.check()
            assert isinstance(result, ChangeSet)


# ── BeagleDaemon import ──────────────────────────────────────────────────────


class TestDaemonImport:
    """Verify BeagleDaemon can be imported (no git repo needed for import)."""

    def test_import_beagle_daemon(self):
        from beagle.daemon.daemon import BeagleDaemon

        assert BeagleDaemon is not None

    def test_daemon_creation(self):
        from beagle.daemon.daemon import BeagleDaemon

        with tempfile.TemporaryDirectory() as tmpdir:
            daemon = BeagleDaemon(tmpdir)
            assert daemon.workspace_root == Path(tmpdir)
            assert daemon.daily_cost == 0.0
            assert daemon._idle_counter == 0

    def test_daemon_stop_sets_event(self):
        from beagle.daemon.daemon import BeagleDaemon

        with tempfile.TemporaryDirectory() as tmpdir:
            daemon = BeagleDaemon(tmpdir)
            assert not daemon._stop_event.is_set()
            daemon.stop()
            assert daemon._stop_event.is_set()
