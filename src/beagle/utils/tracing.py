"""OpenTelemetry tracing for Beagle — re-export shim.

Canonical implementation moved to ``beagle.observability.tracing``.
This module re-exports all public symbols for backward compatibility.
"""

from beagle.observability.tracing import (
    OTEL_AVAILABLE,
    TracingContext,
    add_event,
    get_tracer,
    init_tracing,
    record_exception,
    set_attribute,
    shutdown_tracing,
    span,
    trace_async,
)

__all__ = [
    "OTEL_AVAILABLE",
    "TracingContext",
    "add_event",
    "get_tracer",
    "init_tracing",
    "record_exception",
    "set_attribute",
    "shutdown_tracing",
    "span",
    "trace_async",
]
