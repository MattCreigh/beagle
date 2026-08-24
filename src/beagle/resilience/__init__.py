"""Resilience package for Beagle.

Provides degradation management and fallback chains for graceful
degradation under failure conditions.
"""

from .degradation import DegradationLevel, DegradationManager, DegradationState, TriggerType
from .fallback import FallbackChain, FallbackLevel, FallbackResult

__all__ = [
    "DegradationLevel",
    "DegradationManager",
    "DegradationState",
    "FallbackChain",
    "FallbackLevel",
    "FallbackResult",
    "TriggerType",
]
