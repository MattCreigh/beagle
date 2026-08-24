"""SLO (Service Level Objective) package for Beagle.

Provides SLI definitions, SLO targets, error budget policy,
and real-time tracking via EventBus subscription.
"""

from .indicators import SLIDefinition, SLIType, get_sli, list_slis
from .objectives import ALL_SLO_OBJECTIVES, SLOObjective, get_slo, list_slos
from .policy import BudgetState, BudgetStatus, ErrorBudgetPolicy, VelocityAction
from .tracker import ComplianceReport, SLOTracker

__all__ = [
    "ALL_SLO_OBJECTIVES",
    "BudgetState",
    "BudgetStatus",
    "ComplianceReport",
    "ErrorBudgetPolicy",
    "SLIDefinition",
    "SLIType",
    "SLOObjective",
    "SLOTracker",
    "VelocityAction",
    "get_sli",
    "get_slo",
    "list_slis",
    "list_slos",
]
