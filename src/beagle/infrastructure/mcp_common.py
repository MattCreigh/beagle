"""Shared utilities for Beagle MCP servers.

Consolidates correlation-ID tracking, metrics collection,
and common helpers used across mcp_rag_server, mcp_utility_server,
and mcp_openclaw_server.
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from typing import Any

# ── Correlation ID (async-safe request tracing) ──────────────────────────

_correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)


class CorrelationIdFilter(logging.Filter):
    """Inject correlation_id into log records for async-safe tracing."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = _correlation_id_var.get("")
        return True


def set_correlation_id(correlation_id: str | None = None) -> str:
    """Set or generate a correlation ID for request tracing.

    Args:
        correlation_id: Optional explicit ID. If None, a UUID4 is generated.

    Returns:
        The correlation ID that was set.

    """
    cid = correlation_id or str(uuid.uuid4())
    _correlation_id_var.set(cid)
    return cid


def get_correlation_id() -> str:
    """Return the current correlation ID (empty string if unset)."""
    return _correlation_id_var.get("")


# ── Metrics collection ───────────────────────────────────────────────────

_metrics: dict[str, Any] = {
    "requests": {"total": 0, "success": 0, "error": 0},
    "durations": {},
}


def record_metric(name: str, duration: float, *, success: bool = True) -> None:
    """Record a tool-call metric.

    Args:
        name: Metric name (typically the tool function name).
        duration: Wall-clock duration in seconds.
        success: Whether the invocation succeeded.

    """
    _metrics["requests"]["total"] += 1
    _metrics["requests"]["success" if success else "error"] += 1
    bucket = _metrics["durations"].setdefault(name, [])
    bucket.append(duration)


def get_metrics_summary() -> dict[str, Any]:
    """Return a summary dict of collected metrics."""
    summary: dict[str, Any] = {
        "requests": dict(_metrics["requests"]),
        "durations": {},
    }
    for name, samples in _metrics["durations"].items():
        if not samples:
            continue
        samples.sort()
        summary["durations"][name] = {
            "count": len(samples),
            "min": samples[0],
            "max": samples[-1],
            "median": samples[len(samples) // 2],
            "p95": samples[int(len(samples) * 0.95)],
        }
    return summary


def reset_metrics() -> None:
    """Reset all metric counters (useful for testing)."""
    global _metrics
    _metrics = {
        "requests": {"total": 0, "success": 0, "error": 0},
        "durations": {},
    }


# ── Path safety helper ───────────────────────────────────────────────────


def is_path_within(child: str | object, root: str | object) -> bool:
    """Check that *child* resolves to a path within *root*.

    Uses ``Path.resolve()`` — follows symlinks — and ``relative_to``
    instead of the insecure ``str.startswith`` pattern.

    Args:
        child: Path to check.
        root: Containing directory.

    Returns:
        True if *child* is inside *root*.

    """
    from pathlib import Path

    try:
        Path(str(child)).resolve().relative_to(Path(str(root)).resolve())
        return True
    except ValueError:
        return False


__all__ = [
    "CorrelationIdFilter",
    "get_correlation_id",
    "get_metrics_summary",
    "is_path_within",
    "record_metric",
    "reset_metrics",
    "set_correlation_id",
]
