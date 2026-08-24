"""Beagle Startup Package — pre-flight validation and health checks.

Runs before the orchestrator starts to verify that the environment
is ready for workflow execution.  Each check returns a result dict
with ``status`` (``"ok"`` | ``"warn"`` | ``"fail"``) and an
actionable ``message``.
"""

from .health_check import StartupCheckResult, run_startup_checks

__all__ = ["StartupCheckResult", "run_startup_checks"]
