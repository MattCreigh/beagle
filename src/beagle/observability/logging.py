"""Structured logging with optional structlog integration.

Provides ``get_structured_logger()`` which returns a logger that emits
structured key-value JSON when structlog is available, and falls back to
standard Python logging with trace correlation when it's not.

Trace correlation: if OpenTelemetry is active, every log record includes
``trace_id`` and ``span_id`` fields for cross-referencing with traces.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

_STRUCTLOG_AVAILABLE = False
try:
    import structlog

    _STRUCTLOG_AVAILABLE = True
except ImportError:
    structlog = None  # type: ignore[assignment]


# ── Trace correlation filter ─────────────────────────────────────────────────


# <invariant>
# Nothing in this module may emit a log record on the failure path. Both
# call sites below run *inside* the logging pipeline — a logging.Filter and
# a structlog processor — so logging from them re-enters the pipeline and
# recurses. The failures are counted instead, and
# trace_correlation_failures() exposes the count to health reporting.
# </invariant>
_trace_correlation_failures = 0


def trace_correlation_failures() -> int:
    """Return how many times OTel trace correlation failed.

    A non-zero value means log records are missing trace_id/span_id, so logs
    cannot be joined to traces.

    Returns:
        The process-wide failure count.

    """
    return _trace_correlation_failures


class TraceCorrelationFilter(logging.Filter):
    """Inject OTel trace_id and span_id into log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = ""
        record.span_id = ""
        try:
            from opentelemetry import trace

            span = trace.get_current_span()
            ctx = span.get_span_context()
            if ctx and ctx.trace_id:
                record.trace_id = format(ctx.trace_id, "032x")
                record.span_id = format(ctx.span_id, "016x")
        except (ImportError, AttributeError, TypeError, ValueError):
            global _trace_correlation_failures
            _trace_correlation_failures += 1
        # The record is always allowed through: a missing trace id degrades the
        # record, it does not make the record unloggable.
        return True


# ── JSON formatter (stdlib fallback) ─────────────────────────────────────────


class JSONFormatter(logging.Formatter):
    """Emit structured JSON log lines when structlog is unavailable."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Trace correlation
        if getattr(record, "trace_id", ""):
            log_entry["trace_id"] = getattr(record, "trace_id", "")
        if getattr(record, "span_id", ""):
            log_entry["span_id"] = getattr(record, "span_id", "")
        # Exception info
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = self.formatException(record.exc_info)
        # Extra fields
        for key in ("node_name", "workflow_id", "model", "tokens", "cost"):
            val = getattr(record, key, None)
            if val is not None:
                log_entry[key] = val
        return json.dumps(log_entry, default=str)


# ── Structlog configuration ──────────────────────────────────────────────────


def _configure_structlog() -> None:
    """Configure structlog with OTel trace correlation and JSON rendering."""
    if not _STRUCTLOG_AVAILABLE or structlog is None:
        return

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _add_otel_context,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def _add_otel_context(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> Mapping[str, Any]:
    """Structlog processor: inject OTel trace/span IDs."""
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.trace_id:
            event_dict["trace_id"] = format(ctx.trace_id, "032x")
            event_dict["span_id"] = format(ctx.span_id, "016x")
    except (ImportError, AttributeError, TypeError, ValueError):
        global _trace_correlation_failures
        _trace_correlation_failures += 1
    return event_dict


# ── Public API ───────────────────────────────────────────────────────────────

_configured = False


def get_structured_logger(
    name: str,
    use_json: bool = True,
) -> Any:
    """Get a structured logger.

    If structlog is available, returns a structlog BoundLogger with
    OTel trace correlation. Otherwise, returns a standard Python logger
    with JSON formatting and trace correlation filter.

    Args:
        name: Logger name (e.g. "Beagle.orchestrator").
        use_json: If True and structlog unavailable, use JSONFormatter.

    Returns:
        A structlog BoundLogger or stdlib Logger.

    """
    global _configured

    if _STRUCTLOG_AVAILABLE and structlog is not None:
        if not _configured:
            _configure_structlog()
            _configured = True
        return structlog.get_logger(name)

    # Fallback: stdlib with JSON formatter + trace correlation
    std_logger = logging.getLogger(name)
    if not std_logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        if use_json:
            handler.setFormatter(JSONFormatter())
        else:
            handler.setFormatter(
                logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
            )
        handler.addFilter(TraceCorrelationFilter())
        std_logger.addHandler(handler)
        std_logger.setLevel(logging.INFO)
    return std_logger
