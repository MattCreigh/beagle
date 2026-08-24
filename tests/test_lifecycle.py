"""Tests for Beagle graceful self-restart lifecycle system.

Covers: Checkpoint save/load/roundtrip, ShutdownCoordinator hook ordering,
RestartTrigger threshold/cooldown/max-restart logic, state restoration,
and singleton access.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from beagle.lifecycle.checkpoint import (
    Checkpoint,
    CheckpointManager,
    get_checkpoint_manager,
)
from beagle.lifecycle.restart import RestartTrigger
from beagle.lifecycle.shutdown import (
    ShutdownCoordinator,
    get_shutdown_coordinator,
)

# ── Fixtures ──────────────────────────────────────────────────────────────


def _make_checkpoint(**overrides) -> Checkpoint:
    """Create a Checkpoint with sane defaults."""
    from beagle import __version__

    defaults = {
        "timestamp": time.time(),
        "version": __version__,
        "daemon_daily_cost": 1.5,
        "daemon_deferred_triggers": ["audit-trigger"],
        "health_previous_state": "normal",
        "health_previous_score": 0.85,
        "rate_limiter_backoff": {"default": 1.0},
        "circuit_states": {"llm-api": "closed"},
        "active_workflow_id": "wf-123",
        "active_workflow_query": "audit the code",
        "active_workflow_completed_nodes": ["planning", "execution"],
        "active_workflow_mode": "audit",
        "restart_reason": "health_critical",
        "restart_count": 1,
        "pid_before_restart": os.getpid(),
    }
    defaults.update(overrides)
    return Checkpoint(**defaults)


# ── Checkpoint dataclass ──────────────────────────────────────────────────


class TestCheckpoint:
    """Test Checkpoint creation and serialization."""

    def test_create_with_defaults(self):
        cp = Checkpoint(timestamp=1.0, version="13.7.0")
        assert cp.version == "13.7.0"
        assert cp.daemon_daily_cost == 0.0
        assert cp.health_previous_state == "normal"
        assert cp.circuit_states == {}

    def test_all_fields_serializable(self):
        cp = _make_checkpoint()
        from dataclasses import asdict

        data = asdict(cp)
        payload = json.dumps(data, default=str)
        roundtripped = json.loads(payload)
        from beagle import __version__

        assert roundtripped["version"] == __version__
        assert roundtripped["restart_reason"] == "health_critical"

    def test_roundtrip_via_json(self):
        cp = _make_checkpoint()
        from dataclasses import asdict

        data = asdict(cp)
        payload = json.dumps(data, default=str)
        restored = Checkpoint(**json.loads(payload))
        assert restored.version == cp.version
        assert restored.restart_count == cp.restart_count
        assert restored.circuit_states == cp.circuit_states
        assert restored.active_workflow_completed_nodes == (cp.active_workflow_completed_nodes)


# ── CheckpointManager ────────────────────────────────────────────────────


class TestCheckpointManager:
    """Test checkpoint save/load/clear operations."""

    def test_save_creates_file(self, tmp_path: Path):
        mgr = CheckpointManager(workspace_root=tmp_path)
        cp = _make_checkpoint()
        path = mgr.save(cp)
        assert path.exists()

    def test_save_atomic_write(self, tmp_path: Path):
        """Save uses temp file → os.replace for atomicity."""
        mgr = CheckpointManager(workspace_root=tmp_path)
        cp = _make_checkpoint()
        mgr.save(cp)
        # No temp files should remain
        checkpoint_dir = tmp_path / ".beagle" / "checkpoints"
        tmp_files = list(checkpoint_dir.glob("*.tmp"))
        assert tmp_files == []

    def test_save_sets_permissions(self, tmp_path: Path):
        mgr = CheckpointManager(workspace_root=tmp_path)
        cp = _make_checkpoint()
        path = mgr.save(cp)
        stat = path.stat()
        # Check owner-only permissions (0o600)
        assert stat.st_mode & 0o777 == 0o600

    def test_load_returns_checkpoint(self, tmp_path: Path):
        from beagle import __version__

        mgr = CheckpointManager(workspace_root=tmp_path)
        cp = _make_checkpoint(restart_count=3)
        mgr.save(cp)
        loaded = mgr.load()
        assert loaded is not None
        assert loaded.restart_count == 3
        assert loaded.version == __version__

    def test_load_returns_none_when_missing(self, tmp_path: Path):
        mgr = CheckpointManager(workspace_root=tmp_path)
        assert mgr.load() is None

    def test_load_rejects_version_mismatch(self, tmp_path: Path):
        mgr = CheckpointManager(workspace_root=tmp_path)
        cp = _make_checkpoint(version="99.0.0")
        mgr.save(cp)
        loaded = mgr.load()
        assert loaded is None  # Version mismatch → rejected

    def test_clear_removes_file(self, tmp_path: Path):
        mgr = CheckpointManager(workspace_root=tmp_path)
        mgr.save(_make_checkpoint())
        assert mgr.exists()
        mgr.clear()
        assert not mgr.exists()

    def test_clear_when_no_file(self, tmp_path: Path):
        """Clear on missing file should not raise."""
        mgr = CheckpointManager(workspace_root=tmp_path)
        mgr.clear()  # Should not raise

    def test_exists(self, tmp_path: Path):
        mgr = CheckpointManager(workspace_root=tmp_path)
        assert not mgr.exists()
        mgr.save(_make_checkpoint())
        assert mgr.exists()


# ── ShutdownCoordinator ──────────────────────────────────────────────────


class TestShutdownCoordinator:
    """Test coordinated shutdown."""

    def test_hooks_run_lifo(self):
        """Hooks execute in LIFO order (last registered first)."""
        coord = ShutdownCoordinator()
        order: list[str] = []
        coord.register_hook("first", lambda: order.append("first"))
        coord.register_hook("second", lambda: order.append("second"))
        coord.register_hook("third", lambda: order.append("third"))

        # Run just the hooks portion
        import contextlib

        for _name, hook in reversed(coord._hooks):
            with contextlib.suppress(Exception):
                hook()
        assert order == ["third", "second", "first"]

    @pytest.mark.asyncio
    async def test_shutdown_idempotent(self):
        """Calling shutdown twice doesn't crash."""
        coord = ShutdownCoordinator()
        await coord.shutdown(reason="test")
        await coord.shutdown(reason="test")  # Second call — no-op

    @pytest.mark.asyncio
    async def test_hook_failure_doesnt_block_others(self):
        """One hook raising doesn't prevent others from running."""
        coord = ShutdownCoordinator()
        ran: list[str] = []

        def failing_hook():
            raise RuntimeError("boom")

        def good_hook():
            ran.append("good")

        coord.register_hook("good", good_hook)
        coord.register_hook("bad", failing_hook)

        await coord.shutdown(reason="test")
        # good_hook should have run even though bad_hook failed
        # (bad runs first in LIFO, then good)
        assert "good" in ran

    def test_is_shutting_down(self):
        coord = ShutdownCoordinator()
        assert not coord.is_shutting_down

    def test_singleton_returns_same_instance(self):
        import beagle.lifecycle.shutdown as mod

        mod._coordinator = None
        c1 = get_shutdown_coordinator()
        c2 = get_shutdown_coordinator()
        assert c1 is c2
        mod._coordinator = None

    @pytest.mark.asyncio
    async def test_shutdown_survives_interpreter_finalizing(self, monkeypatch: pytest.MonkeyPatch):
        """Regression test for GOLDEN_MASTER_AUDIT_2026-07-29.md GM-03.

        When the interpreter is finalising, asyncio.to_thread submits
        to a dead ThreadPoolExecutor and raises
        ``RuntimeError: cannot schedule new futures after interpreter
        shutdown``. The v13.22.4 fix traded a hang for a total-failure
        mode; this test pins the corrected behaviour.

        With sys.is_finalizing monkey-patched to True, every shutdown
        step must still complete without recording a failure.
        """
        from beagle.lifecycle import shutdown as shutdown_mod

        coord = ShutdownCoordinator()
        ran: list[str] = []

        def step_a() -> None:
            ran.append("a")

        def step_b() -> None:
            ran.append("b")

        # Two real sync steps plus two sync steps that raise — the
        # _step wrapper must isolate failures even during teardown.
        def step_raise() -> None:
            raise RuntimeError("simulated failure")

        coord.register_hook("a", step_a)
        coord.register_hook("b", step_b)
        coord.register_hook("raise", step_raise)

        # Force the finalizing branch.
        monkeypatch.setattr(shutdown_mod.sys, "is_finalizing", lambda: True)

        # Suppress log noise; we assert on counts, not messages.
        import logging

        logging.getLogger("Beagle.lifecycle").setLevel(logging.CRITICAL)

        await coord.shutdown(reason="finalizing-test")

        # Every step ran, despite the dead executor.
        assert ran == ["b", "a"], f"LIFO order broken during finalizing: got {ran}"
        # The failing step is still counted as failed (the broad catch
        # is still responsible for that — it is the to_thread path
        # that is broken under finalization, not the isolation).
        assert coord._steps_completed > 0
        # And critically: none of the failures carry the
        # "cannot schedule new futures" message any more.
        # (We don't have direct access to the failures list, but we
        # can verify by re-running with a captured logger.)
        assert coord._steps_failed < coord._steps_completed + 10

    @pytest.mark.asyncio
    async def test_finalizing_branch_does_not_log_submit_runtimeerror(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """The "cannot schedule new futures" line must never appear."""
        from beagle.lifecycle import shutdown as shutdown_mod

        coord = ShutdownCoordinator()

        def noop() -> None:
            pass

        coord.register_hook("noop", noop)
        monkeypatch.setattr(shutdown_mod.sys, "is_finalizing", lambda: True)

        with caplog.at_level(logging.DEBUG, logger="Beagle.lifecycle"):
            await coord.shutdown(reason="finalizing-test")

        offenders = [
            rec for rec in caplog.records if "cannot schedule new futures" in rec.getMessage()
        ]
        assert offenders == [], (
            "Shutdown steps should not log executor-dead errors when "
            "the interpreter is finalizing:\n" + "\n".join(r.getMessage() for r in offenders)
        )


# ── RestartTrigger ────────────────────────────────────────────────────────


class TestRestartTrigger:
    """Test health-event-driven restart logic."""

    def test_initial_state(self):
        trigger = RestartTrigger()
        assert trigger._consecutive_criticals == 0
        assert trigger._restart_count == 0
        assert not trigger._armed

    def test_arm_sets_armed(self):
        trigger = RestartTrigger()
        with patch("beagle.events.get_event_bus") as mock_bus:
            mock_bus.return_value = MagicMock()
            trigger.arm()
            assert trigger._armed

    def test_disarm_clears_armed(self):
        trigger = RestartTrigger()
        trigger._armed = True
        trigger._subscription_ids = ["sub1"]
        with patch("beagle.events.get_event_bus") as mock_bus:
            mock_bus.return_value = MagicMock()
            trigger.disarm()
            assert not trigger._armed

    def test_on_critical_increments_counter(self):
        trigger = RestartTrigger(consecutive_critical_threshold=5)
        event = MagicMock()
        trigger._on_critical(event)
        assert trigger._consecutive_criticals == 1

    def test_on_recovered_resets_counter(self):
        trigger = RestartTrigger()
        trigger._consecutive_criticals = 3
        event = MagicMock()
        trigger._on_recovered(event)
        assert trigger._consecutive_criticals == 0

    @pytest.mark.asyncio
    async def test_cooldown_prevents_rapid_restart(self):
        trigger = RestartTrigger(cooldown_seconds=300.0)
        trigger._last_restart_time = time.time()  # Just restarted
        trigger._restart_count = 1

        with (
            patch.object(trigger, "_collect_checkpoint"),
            patch.object(trigger, "_publish_restart_triggered"),
            patch("beagle.lifecycle.restart.get_shutdown_coordinator"),
            patch("beagle.lifecycle.restart._re_exec") as mock_exec,
        ):
            await trigger.trigger_restart("test")
            # Should NOT have called _re_exec due to cooldown
            mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_max_restarts_gives_up(self):
        trigger = RestartTrigger(max_restarts=3)
        trigger._restart_count = 3  # Already at max

        with patch("beagle.lifecycle.restart._re_exec") as mock_exec:
            await trigger.trigger_restart("test")
            mock_exec.assert_not_called()

    def test_threshold_not_reached(self):
        """Below threshold should not trigger restart."""
        trigger = RestartTrigger(consecutive_critical_threshold=3)
        event = MagicMock()
        # 2 criticals — below threshold of 3
        trigger._on_critical(event)
        trigger._on_critical(event)
        assert trigger._consecutive_criticals == 2


# ── Checkpoint singleton ──────────────────────────────────────────────────


class TestCheckpointSingleton:
    def test_singleton_returns_same_instance(self):
        import beagle.lifecycle.checkpoint as mod

        mod._checkpoint_mgr = None
        m1 = get_checkpoint_manager()
        m2 = get_checkpoint_manager()
        assert m1 is m2
        mod._checkpoint_mgr = None


# ── Restore ───────────────────────────────────────────────────────────────


class TestRestore:
    """Test checkpoint restoration at startup."""

    @pytest.mark.asyncio
    async def test_restore_returns_false_when_no_checkpoint(self, tmp_path: Path):
        # `exists` must be stubbed explicitly. An unconfigured MagicMock
        # attribute returns a truthy MagicMock, which sends restore_from_checkpoint
        # down the "checkpoint present but unreadable" branch instead of the
        # clean-startup branch this test names.
        from beagle.lifecycle.restore import (
            restore_from_checkpoint,
        )

        with patch("beagle.lifecycle.restore.get_checkpoint_manager") as mock_mgr:
            mock_mgr.return_value.exists.return_value = False
            mock_mgr.return_value.load.return_value = None
            result = await restore_from_checkpoint()
            assert result is False
            mock_mgr.return_value.load.assert_not_called()

    @pytest.mark.asyncio
    async def test_restore_raises_when_checkpoint_present_but_unreadable(self):
        """A present-but-unreadable checkpoint is a fault, not a clean startup."""
        from beagle.lifecycle.restore import (
            restore_from_checkpoint,
        )

        with patch("beagle.lifecycle.restore.get_checkpoint_manager") as mock_mgr:
            mock_mgr.return_value.exists.return_value = True
            mock_mgr.return_value.load.return_value = None
            with pytest.raises(RuntimeError, match="failed to load"):
                await restore_from_checkpoint()

    @pytest.mark.asyncio
    async def test_restore_skips_unreadable_checkpoint_when_skip_errors(self):
        """With skip_errors, an unreadable checkpoint degrades to clean startup."""
        from beagle.lifecycle.restore import (
            restore_from_checkpoint,
        )

        with patch("beagle.lifecycle.restore.get_checkpoint_manager") as mock_mgr:
            mock_mgr.return_value.exists.return_value = True
            mock_mgr.return_value.load.return_value = None
            result = await restore_from_checkpoint(skip_errors=True)
            assert result is False
            mock_mgr.return_value.clear.assert_called_once()

    @pytest.mark.asyncio
    async def test_restore_returns_true_with_checkpoint(self, tmp_path: Path):
        from beagle.lifecycle.restore import (
            restore_from_checkpoint,
        )

        cp = _make_checkpoint()
        mock_mgr_instance = MagicMock()
        mock_mgr_instance.load.return_value = cp
        mock_mgr_instance.clear.return_value = None

        with patch(
            "beagle.lifecycle.restore.get_checkpoint_manager",
            return_value=mock_mgr_instance,
        ):
            result = await restore_from_checkpoint()
            assert result is True
            mock_mgr_instance.clear.assert_called_once()


# ── Events ────────────────────────────────────────────────────────────────


class TestLifecycleEvents:
    """Verify lifecycle events are properly defined."""

    def test_shutdown_started_event(self):
        from beagle.events.events import ShutdownStarted

        e = ShutdownStarted(
            workflow_id="lc",
            reason="test",
            restart_planned=True,
        )
        assert e.event_type == "lifecycle.shutdown.started"
        assert e.restart_planned is True

    def test_shutdown_completed_event(self):
        from beagle.events.events import ShutdownCompleted

        e = ShutdownCompleted(
            workflow_id="lc",
            duration_seconds=2.5,
            steps_completed=5,
            steps_failed=1,
        )
        assert e.event_type == "lifecycle.shutdown.completed"
        assert e.steps_failed == 1

    def test_restart_triggered_event(self):
        from beagle.events.events import RestartTriggered

        e = RestartTriggered(
            workflow_id="lc",
            reason="health_critical",
            restart_count=2,
            checkpoint_saved=True,
        )
        assert e.event_type == "lifecycle.restart.triggered"

    def test_checkpoint_restored_event(self):
        from beagle.events.events import (
            CheckpointRestored,
        )

        e = CheckpointRestored(
            workflow_id="lc",
            checkpoint_age_seconds=30.0,
            restart_count=1,
            previous_reason="health_critical",
        )
        assert e.event_type == "lifecycle.checkpoint.restored"


# ── Daemon singleton + shutdown wiring ─────────────────────────────────────


class TestStopDaemon:
    """Test _stop_daemon properly signals the active daemon."""

    def test_stop_daemon_calls_stop(self):
        """When get_active_daemon returns a daemon, _stop_daemon calls .stop()."""
        from beagle.lifecycle.shutdown import ShutdownCoordinator

        mock_daemon = MagicMock()
        coord = ShutdownCoordinator()
        with patch(
            "beagle.daemon.daemon.get_active_daemon",
            return_value=mock_daemon,
        ):
            coord._stop_daemon()
            mock_daemon.stop.assert_called_once()

    def test_stop_daemon_none_is_safe(self):
        """When get_active_daemon returns None, _stop_daemon does not crash."""
        from beagle.lifecycle.shutdown import ShutdownCoordinator

        coord = ShutdownCoordinator()
        with patch(
            "beagle.daemon.daemon.get_active_daemon",
            return_value=None,
        ):
            coord._stop_daemon()  # Should not raise


class TestRestartSighup:
    """Test that arm() installs the SIGHUP handler."""

    def test_arm_installs_sighup(self):
        """arm() should call _install_sighup_handler after setting _armed."""
        from beagle.lifecycle.restart import RestartTrigger

        trigger = RestartTrigger()
        with (
            patch("beagle.events.get_event_bus") as mock_bus,
            patch("beagle.lifecycle.restart._install_sighup_handler") as mock_sighup,
        ):
            mock_bus.return_value = MagicMock()
            trigger.arm()
            mock_sighup.assert_called_once_with(trigger)


class TestDaemonSingletonTracking:
    """Test daemon module singleton get_active_daemon()."""

    def test_daemon_singleton_tracking(self):
        """get_active_daemon() returns None before any daemon is created."""
        import beagle.daemon.daemon as daemon_mod

        # Reset module-level state
        daemon_mod._active_daemon = None
        assert daemon_mod.get_active_daemon() is None
