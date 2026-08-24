"""Beagle Self-Health Monitoring Package.

Provides periodic health checks, event-driven alerts, and trend analysis
for the dark factory operation model.  The factory monitors its own
memory, file descriptors, circuit breakers, rate limiters, cache hit
rates, subprocess pool, and more.
"""

from .collector import HealthSnapshot, calculate_health_score, collect_snapshot
from .monitor import HealthMonitor, get_health_monitor, run_health_check
from .thresholds import HealthThresholds

__all__ = [
    "HealthMonitor",
    "HealthSnapshot",
    "HealthThresholds",
    "calculate_health_score",
    "collect_snapshot",
    "get_health_monitor",
    "run_health_check",
]
