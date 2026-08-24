"""Observability stack — consolidated tracing, metrics, and structured logging.

Replaces the fragmented ``utils/tracing.py`` and ``enterprise/telemetry.py``
with a unified package that follows OpenTelemetry GenAI semantic conventions.

Quick start::

    from beagle.observability import (
        configure_observability,
        get_tracer,
        span,
        trace_async,
        record_genai_metric,
        get_structured_logger,
    )

    # Initialize once at startup
    configure_observability()

    # Use throughout
    with span("my.operation", {"key": "value"}):
        ...

    @trace_async("my.async_op")
    async def my_function():
        ...

SP-12: the re-exported names are listed explicitly and repeated in ``__all__``
so the public surface is the code rather than a set of ``unused-import``
suppression comments.
"""

from beagle.observability.config import (
    ObservabilityConfig,
    configure_observability,
    get_observability_config,
)
from beagle.observability.genai import (
    GenAISpanKind,
    genai_span,
)
from beagle.observability.logging import (
    get_structured_logger,
)
from beagle.observability.metrics import (
    MetricsCollector,
    get_metrics_collector,
    record_genai_metric,
)
from beagle.observability.tracing import (
    add_event,
    get_tracer,
    init_tracing,
    record_exception,
    set_attribute,
    span,
    trace_async,
)

__all__ = [
    "GenAISpanKind",
    "MetricsCollector",
    "ObservabilityConfig",
    "add_event",
    "configure_observability",
    "genai_span",
    "get_metrics_collector",
    "get_observability_config",
    "get_structured_logger",
    "get_tracer",
    "init_tracing",
    "record_exception",
    "record_genai_metric",
    "set_attribute",
    "span",
    "trace_async",
]
