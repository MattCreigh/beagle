"""Service Level Objectives (SLOs) for Beagle.

Defines target thresholds and error budgets for each SLI.
Follows Google SRE error budget policy patterns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .indicators import SLIType


@dataclass(frozen=True)
class SLOObjective:
    """A Service Level Objective with target and error budget."""

    sli: str
    target: float
    target_unit: str
    window_days: int
    error_budget_percent: float
    description: str
    critical: bool = False

    @property
    def target_display(self) -> str:
        """Human-readable target string."""
        if self.target_unit == "percent":
            return f"{self.target:.1f}%"
        if self.target_unit == "seconds":
            return f"{self.target:.0f}s"
        return f"{self.target} {self.target_unit}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "sli": self.sli,
            "target": self.target,
            "target_unit": self.target_unit,
            "window_days": self.window_days,
            "error_budget_percent": self.error_budget_percent,
            "description": self.description,
            "critical": self.critical,
            "target_display": self.target_display,
        }


# ── Canonical SLO definitions ─────────────────────────────────────────────────

SLO_WORKFLOW_SUCCESS = SLOObjective(
    sli=SLIType.WORKFLOW_SUCCESS_RATE,
    target=99.0,
    target_unit="percent",
    window_days=28,
    error_budget_percent=1.0,
    description="99.0% of workflows complete without error over 28 days",
    critical=True,
)

SLO_NODE_LATENCY = SLOObjective(
    sli=SLIType.NODE_LATENCY_P95,
    target=120.0,
    target_unit="seconds",
    window_days=28,
    error_budget_percent=5.0,
    description="95th percentile node latency stays under 120 seconds",
    critical=True,
)

SLO_E2E_LATENCY = SLOObjective(
    sli=SLIType.E2E_LATENCY_P95,
    target=600.0,
    target_unit="seconds",
    window_days=28,
    error_budget_percent=5.0,
    description="95th percentile workflow latency stays under 600 seconds",
    critical=False,
)

SLO_BUDGET_ACCURACY = SLOObjective(
    sli=SLIType.BUDGET_ACCURACY,
    target=90.0,
    target_unit="percent",
    window_days=28,
    error_budget_percent=10.0,
    description="90% of workflows complete within 2x of preflight estimate",
    critical=False,
)

SLO_MCP_AVAILABILITY = SLOObjective(
    sli=SLIType.MCP_AVAILABILITY,
    target=99.5,
    target_unit="percent",
    window_days=28,
    error_budget_percent=0.5,
    description="99.5% of MCP tool calls succeed",
    critical=True,
)

SLO_GROUND_TRUTH = SLOObjective(
    sli=SLIType.GROUND_TRUTH_SUCCESS_RATE,
    target=95.0,
    target_unit="percent",
    window_days=28,
    error_budget_percent=5.0,
    description="95% of research workflows pass ground-truth validation",
    critical=False,
)

ALL_SLO_OBJECTIVES: tuple[SLOObjective, ...] = (
    SLO_WORKFLOW_SUCCESS,
    SLO_NODE_LATENCY,
    SLO_E2E_LATENCY,
    SLO_BUDGET_ACCURACY,
    SLO_MCP_AVAILABILITY,
    SLO_GROUND_TRUTH,
)


def get_slo(sli_type: str) -> SLOObjective | None:
    """Lookup an SLO objective by SLI type."""
    for slo in ALL_SLO_OBJECTIVES:
        if slo.sli == sli_type:
            return slo
    return None


def list_slos() -> list[SLOObjective]:
    """Return all registered SLO objectives."""
    return list(ALL_SLO_OBJECTIVES)
