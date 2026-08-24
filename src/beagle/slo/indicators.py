"""Service Level Indicators (SLIs) for Beagle.

Defines the measurable signals that feed into SLO calculations.
Each SLI corresponds to a specific event stream from the EventBus.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Any


class SLIType(StrEnum):
    """Types of service level indicators."""

    WORKFLOW_SUCCESS_RATE = "workflow_success_rate"
    NODE_LATENCY_P95 = "node_latency_p95"
    E2E_LATENCY_P95 = "e2e_latency_p95"
    BUDGET_ACCURACY = "budget_accuracy"
    MCP_AVAILABILITY = "mcp_availability"
    # Renamed from GROUND_TRUTH_PASS_RATE (SP-11): the member is an SLI name,
    # not a credential. The wire value is unchanged, so stored SLO records and
    # dashboards still resolve.
    GROUND_TRUTH_SUCCESS_RATE = "ground_truth_pass_rate"


@dataclass(frozen=True)
class SLIDefinition:
    """Definition of a Service Level Indicator."""

    name: str
    description: str
    event_types: tuple[str, ...]
    measurement_unit: str
    aggregation_window_days: int = 28
    minimum_events: int = 10

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "event_types": list(self.event_types),
            "measurement_unit": self.measurement_unit,
            "aggregation_window_days": self.aggregation_window_days,
            "minimum_events": self.minimum_events,
        }


# ── Canonical SLI definitions ────────────────────────────────────────────────

SLI_WORKFLOW_SUCCESS = SLIDefinition(
    name=SLIType.WORKFLOW_SUCCESS_RATE,
    description="Percentage of workflows completing without error",
    event_types=("workflow.completed",),
    measurement_unit="percent",
)

SLI_NODE_LATENCY = SLIDefinition(
    name=SLIType.NODE_LATENCY_P95,
    description="95th percentile node execution latency",
    event_types=("node.completed", "node.failed"),
    measurement_unit="seconds",
)

SLI_E2E_LATENCY = SLIDefinition(
    name=SLIType.E2E_LATENCY_P95,
    description="95th percentile end-to-end workflow latency",
    event_types=("workflow.completed",),
    measurement_unit="seconds",
)

SLI_BUDGET_ACCURACY = SLIDefinition(
    name=SLIType.BUDGET_ACCURACY,
    description="Percentage of workflows within 2x of preflight estimate",
    event_types=("workflow.completed",),
    measurement_unit="percent",
)

SLI_MCP_AVAILABILITY = SLIDefinition(
    name=SLIType.MCP_AVAILABILITY,
    description="Percentage of MCP tool calls succeeding",
    event_types=("tool.call", "tool.escalated"),
    measurement_unit="percent",
)

SLI_GROUND_TRUTH = SLIDefinition(
    name=SLIType.GROUND_TRUTH_SUCCESS_RATE,
    description="Percentage of research workflows passing hallucination check",
    event_types=("workflow.completed",),
    measurement_unit="percent",
)

ALL_SLIS: tuple[SLIDefinition, ...] = (
    SLI_WORKFLOW_SUCCESS,
    SLI_NODE_LATENCY,
    SLI_E2E_LATENCY,
    SLI_BUDGET_ACCURACY,
    SLI_MCP_AVAILABILITY,
    SLI_GROUND_TRUTH,
)


def get_sli(sli_type: SLIType | str) -> SLIDefinition | None:
    """Lookup an SLI definition by type."""
    name = sli_type.value if isinstance(sli_type, Enum) else sli_type
    for sli in ALL_SLIS:
        if sli.name == name:
            return sli
    return None


def list_slis() -> list[SLIDefinition]:
    """Return all registered SLI definitions."""
    return list(ALL_SLIS)
