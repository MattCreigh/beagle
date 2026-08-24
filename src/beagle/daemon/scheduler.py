"""Scheduler for the Beagle daemon.

Manages tick-based scheduling and blocking budgets.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("Beagle.daemon.scheduler")


class DaemonScheduler:
    """Tick-based scheduler with resource constraints."""

    def __init__(
        self,
        tick_interval: int = 30,
        blocking_budget: int = 15,
        idle_threshold: int = 300,
        max_daily_cost_usd: float = 5.0,
    ):
        self.tick_interval = tick_interval
        self.blocking_budget = blocking_budget
        self.idle_threshold = idle_threshold
        self.max_daily_cost_usd = max_daily_cost_usd

        self.daily_cost = 0.0

    def can_run_now(self, estimated_seconds: float) -> bool:
        """Check if a workflow fits in the blocking budget."""
        return estimated_seconds <= self.blocking_budget

    def increment_cost(self, cost: float) -> None:
        """Track daily spend."""
        self.daily_cost += cost

    def is_over_budget(self) -> bool:
        """Check if daily cap is reached."""
        return self.daily_cost >= self.max_daily_cost_usd
