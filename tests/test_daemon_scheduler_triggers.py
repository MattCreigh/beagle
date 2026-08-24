"""SP-5: tests for daemon/scheduler + daemon/triggers (was zero-coverage).

beagle-spotless-phase2, work package SP-5. The daemon scheduler (blocking
budget / daily cost) and trigger matcher (ChangeSet → workflows) had no direct
tests.
"""

from __future__ import annotations

from beagle.daemon.scheduler import DaemonScheduler
from beagle.daemon.triggers import Trigger, TriggerMatcher
from beagle.daemon.watcher import ChangeSet

# ── DaemonScheduler ────────────────────────────────────────────────────────


def test_scheduler_defaults() -> None:
    """Scheduler defaults match the documented budget/idle values."""
    s = DaemonScheduler()
    assert s.tick_interval == 30
    assert s.blocking_budget == 15
    assert s.idle_threshold == 300
    assert s.max_daily_cost_usd == 5.0


def test_can_run_now_within_budget() -> None:
    """A task within the blocking budget is allowed."""
    s = DaemonScheduler(blocking_budget=15)
    assert s.can_run_now(10.0) is True
    assert s.can_run_now(15.0) is True


def test_can_run_now_over_budget() -> None:
    """A task exceeding the blocking budget is rejected."""
    s = DaemonScheduler(blocking_budget=15)
    assert s.can_run_now(16.0) is False


def test_increment_cost_and_budget() -> None:
    """Daily cost accrues and is_over_budget flips at the cap."""
    s = DaemonScheduler(max_daily_cost_usd=5.0)
    assert s.is_over_budget() is False
    s.increment_cost(3.0)
    assert s.is_over_budget() is False
    s.increment_cost(3.0)
    assert s.is_over_budget() is True
    assert s.daily_cost == 6.0


# ── TriggerMatcher ─────────────────────────────────────────────────────────


def test_trigger_defaults() -> None:
    """Trigger has audit mode, 2.0 budget, 60m cooldown by default."""
    t = Trigger(name="t", file_patterns=["*.py"], workflow="security")
    assert t.mode == "audit"
    assert t.budget == 2.0
    assert t.cooldown_minutes == 60


def test_trigger_matcher_matches_changed_files() -> None:
    """A changed file matching a trigger pattern fires that trigger."""
    matcher = TriggerMatcher([Trigger(name="py", file_patterns=["*.py"], workflow="security")])
    changes = ChangeSet(changed_files=["src/auth.py"], new_files=[])
    matched = matcher.match(changes)
    assert len(matched) == 1
    assert matched[0].name == "py"


def test_trigger_matcher_matches_new_files() -> None:
    """A new file matching a trigger pattern fires that trigger."""
    matcher = TriggerMatcher([Trigger(name="py", file_patterns=["*.py"], workflow="security")])
    changes = ChangeSet(changed_files=[], new_files=["src/new.py"])
    matched = matcher.match(changes)
    assert len(matched) == 1


def test_trigger_matcher_no_match() -> None:
    """No file matching any pattern → no triggers fire."""
    matcher = TriggerMatcher([Trigger(name="py", file_patterns=["*.py"], workflow="security")])
    changes = ChangeSet(changed_files=["README.md"], new_files=[])
    assert matcher.match(changes) == []


def test_trigger_matcher_multiple_matches() -> None:
    """Multiple triggers can match one ChangeSet."""
    matcher = TriggerMatcher(
        [
            Trigger(name="py", file_patterns=["*.py"], workflow="security"),
            Trigger(name="core", file_patterns=["core/*.py"], workflow="planning"),
        ]
    )
    changes = ChangeSet(changed_files=["core/orchestrator.py"], new_files=[])
    matched = matcher.match(changes)
    assert len(matched) == 2


def test_trigger_matcher_defaults_have_triggers() -> None:
    """Default trigger list is non-empty and well-formed."""
    matcher = TriggerMatcher()
    assert matcher.triggers
    assert all(isinstance(t, Trigger) for t in matcher.triggers)
