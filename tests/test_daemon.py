"""Tests for Beagle Daemon (Phase 8.6)."""

from unittest.mock import MagicMock, patch

import pytest

from beagle.daemon.daemon import BeagleDaemon
from beagle.daemon.scheduler import DaemonScheduler
from beagle.daemon.triggers import Trigger, TriggerMatcher
from beagle.daemon.watcher import ChangeSet, Watcher


def test_watcher_no_changes():
    """Test watcher detects no changes when hash matches."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = "hash123"
        mock_run.return_value.returncode = 0

        watcher = Watcher("/tmp")
        # _last_hash is now hash123

        changes = watcher.check()
        assert changes.has_changes is False


def test_trigger_matching():
    """Test trigger matching logic."""
    matcher = TriggerMatcher([Trigger(name="t1", file_patterns=["*.py"], workflow="w1")])

    changes = ChangeSet(changed_files=["main.py"])
    matched = matcher.match(changes)
    assert len(matched) == 1
    assert matched[0].name == "t1"

    changes2 = ChangeSet(changed_files=["README.md"])
    matched2 = matcher.match(changes2)
    assert len(matched2) == 0


def test_scheduler_budget():
    """Test scheduler blocking budget."""
    sched = DaemonScheduler(blocking_budget=10)
    assert sched.can_run_now(5) is True
    assert sched.can_run_now(15) is False


def test_scheduler_cost_tracking():
    """Test scheduler daily cost cap."""
    sched = DaemonScheduler(max_daily_cost_usd=1.0)
    sched.increment_cost(0.5)
    assert sched.is_over_budget() is False
    sched.increment_cost(0.6)
    assert sched.is_over_budget() is True


def test_changeset_affected_modules():
    """Test changeset module extraction."""
    cs = ChangeSet(changed_files=["core/orchestrator.py", "utils/helpers.py", "README.md"])
    modules = cs.affected_modules()
    assert "core" in modules
    assert "utils" in modules
    assert "README.md" in modules  # Top-level files are their own module in this logic


def test_watcher_detects_git_changes():
    """Test watcher detects changes from git diff."""
    with patch("subprocess.run") as mock_run:
        # 1. Constructor calls _get_get_head
        # 2. check() calls _get_git_head
        # 3. check() calls _get_git_diff which calls subprocess.run
        mock_run.side_effect = [
            MagicMock(stdout="oldhash", returncode=0),
            MagicMock(stdout="newhash", returncode=0),
            MagicMock(stdout="M\tmain.py\nA\tnew.py\nD\told.py", returncode=0),
        ]

        watcher = Watcher("/tmp")
        # _last_hash is now oldhash

        changes = watcher.check()
        assert changes.has_changes is True
        assert "main.py" in changes.changed_files
        assert "new.py" in changes.new_files
        assert "old.py" in changes.deleted_files
        assert changes.commit_hash == "newhash"


def test_trigger_wildcard_matching():
    """Test trigger glob matching."""
    matcher = TriggerMatcher(
        [Trigger(name="tests", file_patterns=["tests/test_*.py"], workflow="audit")]
    )

    assert matcher.match(ChangeSet(changed_files=["tests/test_daemon.py"])) != []
    assert matcher.match(ChangeSet(changed_files=["core/nodes.py"])) == []


@pytest.mark.asyncio
async def test_daemon_idle_logic():
    """Test that daemon tracks idle time."""
    with patch("beagle.daemon.watcher.Watcher.check", return_value=ChangeSet()):
        daemon = BeagleDaemon("/tmp")
        daemon.scheduler.tick_interval = 1
        daemon.scheduler.idle_threshold = 2

        # Mock stop event to run only 3 ticks
        stop_mock = MagicMock()
        stop_mock.is_set.side_effect = [False, False, False, True]
        daemon._stop_event = stop_mock

        with patch("beagle.events.bus.EventBus.publish") as mock_pub:
            await daemon.run()
            # Should have published IdleStart
            assert any("daemon.idle_start" in str(args) for args, _ in mock_pub.call_args_list)


@pytest.mark.asyncio
async def test_daemon_trigger_workflow():
    """Test that daemon triggers a workflow on change."""
    changes = ChangeSet(changed_files=["main.py"])
    with (
        patch("beagle.daemon.watcher.Watcher.check", return_value=changes),
        patch(
            "beagle.daemon.daemon.run_workflow",
            return_value={"total_cost": 0.1},
        ) as mock_run,
    ):
        daemon = BeagleDaemon("/tmp")
        # Run 1 tick
        stop_mock = MagicMock()
        stop_mock.is_set.side_effect = [False, True]
        daemon._stop_event = stop_mock

        await daemon.run()
        mock_run.assert_called_once()
        assert daemon.scheduler.daily_cost == 0.1


def test_daemon_stop_lifecycle():
    """Test daemon stop mechanism."""
    daemon = BeagleDaemon("/tmp")
    assert not daemon._stop_event.is_set()
    daemon.stop()
    assert daemon._stop_event.is_set()


def test_scheduler_initialization():
    """Test scheduler default values."""
    sched = DaemonScheduler()
    assert sched.tick_interval == 30
    assert sched.max_daily_cost_usd == 5.0


@pytest.mark.asyncio
async def test_daemon_budget_exhaustion():
    """Test that daemon stops triggering when budget is hit."""
    # Disable signal handlers in tests
    import beagle.core.autonomous_orchestrator as ao_module

    ao_module._signal_handler._test_mode = True

    try:
        changes = ChangeSet(changed_files=["main.py"])
        with (
            patch(
                "beagle.daemon.watcher.Watcher.check",
                return_value=changes,
            ),
            patch("beagle.daemon.daemon.run_workflow") as mock_run,
        ):
            daemon = BeagleDaemon("/tmp")
            daemon.scheduler.max_daily_cost_usd = 0.05
            daemon.scheduler.daily_cost = 0.1

            # Run 1 tick only
            stop_mock = MagicMock()
            stop_mock.is_set.side_effect = [False, True]
            daemon._stop_event = stop_mock

            await daemon.run()
            # Should NOT have triggered workflow
            mock_run.assert_not_called()
    finally:
        ao_module._signal_handler._test_mode = False
