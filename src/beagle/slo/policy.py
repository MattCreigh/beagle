"""Error budget policy for Beagle SLOs.

Implements Google SRE error budget policy pattern:
- Track error budget consumption over rolling 28-day window
- Alert when budget is exhausted
- Reduce velocity when budget is depleted
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .objectives import ALL_SLO_OBJECTIVES

logger = logging.getLogger("Beagle.slo.policy")


class VelocityAction(StrEnum):
    """Actions to take based on error budget state."""

    NORMAL = "normal"
    CAUTION = "caution"
    REDUCE = "reduce"
    HALT = "halt"


class BudgetStatus(StrEnum):
    """Status of an error budget."""

    HEALTHY = "healthy"  # >50% remaining
    CAUTION = "caution"  # 10-50% remaining
    EXHAUSTED = "exhausted"  # <10% remaining or over budget
    BURNING = "burning"  # Burn rate >2x expected


@dataclass(frozen=True)
class BudgetState:
    """Current state of an error budget for an SLO."""

    sli: str
    budget_percent: float
    error_rate: float
    window_days: int
    total_events: int
    error_events: int
    burn_rate: float
    status: BudgetStatus
    velocity_action: VelocityAction
    days_until_burn: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sli": self.sli,
            "budget_percent": round(self.budget_percent, 2),
            "error_rate": round(self.error_rate, 4),
            "window_days": self.window_days,
            "total_events": self.total_events,
            "error_events": self.error_events,
            "burn_rate": round(self.burn_rate, 2),
            "status": self.status.value,
            "velocity_action": self.velocity_action.value,
            "days_until_burn": (
                round(self.days_until_burn, 1) if self.days_until_burn is not None else None
            ),
        }


class ErrorBudgetPolicy:
    """Calculates error budget state and recommends velocity actions.

    Error budget is the inverse of the SLO target. For a 99% target,
    the error budget is 1% — 1 out of 100 requests may fail.

    Budget is tracked over a rolling window and consumed by actual errors.
    """

    def __init__(self) -> None:
        self._objectives = {obj.sli: obj for obj in ALL_SLO_OBJECTIVES}

    def calculate(
        self,
        sli: str,
        total_events: int,
        bad_events: int,
        window_days: int = 28,
    ) -> BudgetState:
        """Calculate the current budget state for an SLI.

        Args:
            sli: The SLI type identifier.
            total_events: Total number of events in the measurement window.
            bad_events: Number of events that violated the SLO.
            window_days: Size of the rolling window in days.

        Returns:
            BudgetState with error budget and recommended action.

        """
        obj = self._objectives.get(sli)
        if obj is None:
            raise ValueError(f"Unknown SLI: {sli}")

        if total_events == 0:
            return BudgetState(
                sli=sli,
                budget_percent=100.0,
                error_rate=0.0,
                window_days=window_days,
                total_events=0,
                error_events=0,
                burn_rate=0.0,
                status=BudgetStatus.HEALTHY,
                velocity_action=VelocityAction.NORMAL,
                days_until_burn=None,
            )

        # For latency SLOs: bad_events = events exceeding target
        error_rate = bad_events / total_events
        budget_percent = max(0.0, obj.error_budget_percent - (error_rate * 100))
        budget_fraction = budget_percent / max(obj.error_budget_percent, 1e-6)

        # Determine status from budget remaining
        if budget_fraction <= 0.0:
            status = BudgetStatus.EXHAUSTED
        elif budget_fraction < 0.2:
            status = BudgetStatus.CAUTION
        else:
            status = BudgetStatus.HEALTHY

        # Calculate burn rate (errors per day vs budget per day)
        expected_budget_per_day = max(obj.error_budget_percent, 1e-6) / window_days
        actual_error_per_day = (error_rate * 100) / max(window_days, 1)
        burn_rate = (
            actual_error_per_day / expected_budget_per_day if expected_budget_per_day else 0.0
        )

        if burn_rate > 2.0 and status != BudgetStatus.EXHAUSTED:
            status = BudgetStatus.BURNING

        # Determine velocity action
        velocity_action = self._velocity_action(status, budget_fraction, obj.critical)

        # Days until burn: if burn rate > 0, estimate days until 0% left
        days_until_burn = None
        if burn_rate > 1e-6 and budget_fraction > 0:
            days_remaining = (window_days * budget_fraction) / burn_rate
            days_until_burn = max(0.0, days_remaining)

        return BudgetState(
            sli=sli,
            budget_percent=budget_percent,
            error_rate=error_rate,
            window_days=window_days,
            total_events=total_events,
            error_events=bad_events,
            burn_rate=burn_rate,
            status=status,
            velocity_action=velocity_action,
            days_until_burn=days_until_burn,
        )

    def _velocity_action(
        self,
        status: BudgetStatus,
        budget_fraction: float,
        critical: bool,
    ) -> VelocityAction:
        """Recommend a velocity action based on budget health."""
        if status == BudgetStatus.EXHAUSTED:
            return VelocityAction.HALT if critical else VelocityAction.REDUCE
        if status == BudgetStatus.BURNING:
            return VelocityAction.REDUCE
        if status == BudgetStatus.CAUTION:
            return VelocityAction.CAUTION
        return VelocityAction.NORMAL

    def check_all(
        self,
        measurements: dict[str, tuple[int, int]],  # sli -> (total, bad)
        window_days: int = 28,
    ) -> dict[str, BudgetState]:
        """Calculate budget state for all SLIs with measurements."""
        results: dict[str, BudgetState] = {}
        for sli, (total, bad) in measurements.items():
            try:
                results[sli] = self.calculate(sli, total, bad, window_days)
            except ValueError:
                logger.warning(f"Skipping unknown SLI during policy check: {sli}")
        return results

    def get_worst(self, states: dict[str, BudgetState]) -> BudgetState | None:
        """Get the most critical budget state."""
        if not states:
            return None
        priority = {
            BudgetStatus.EXHAUSTED: 0,
            BudgetStatus.BURNING: 1,
            BudgetStatus.CAUTION: 2,
            BudgetStatus.HEALTHY: 3,
        }
        return min(states.values(), key=lambda s: priority.get(s.status, 99))
