"""Consolidated tracing — canonical implementation.

Merges functionality from ``utils/tracing.py`` (span helpers) and
``enterprise/telemetry.py`` (TelemetryManager tracing) into a single
module.  Both original modules become re-export shims.

Gracefully degrades to no-ops when OpenTelemetry SDK is unavailable.
"""

from __future__ import annotations

import functools
import logging
import os
from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from types import TracebackType
from typing import Any, ParamSpec, Protocol, Self

logger = logging.getLogger("Beagle.observability.tracing")

#: A span attribute value. OpenTelemetry accepts only these primitives, so this
#: alias is the real contract — using ``Any`` here would hide a caller passing a
#: dict or an arbitrary object, which OTel silently drops.
SpanAttribute = str | bool | int | float


class SpanLike(Protocol):
    """The slice of the OpenTelemetry span API this module uses.

    Declared rather than typing the provider as ``Any``: the module only ever
    calls these four methods, and stating them is what lets the tracer be typed
    without an ``Any`` at the boundary.
    """

    def set_attribute(self, key: str, value: SpanAttribute) -> None: ...
    def set_status(self, _status: object) -> None: ...
    def record_exception(self, exception: BaseException) -> None: ...
    def is_recording(self) -> bool: ...
    def end(self) -> None: ...
    def add_event(self, name: str, attributes: dict[str, SpanAttribute] | None = ...) -> None: ...


class TracerLike(Protocol):
    """The slice of the OpenTelemetry tracer API this module uses."""

    def start_span(self, name: str) -> SpanLike: ...
    @contextmanager
    def start_as_current_span(self, name: str) -> Iterator[SpanLike]: ...

_P = ParamSpec("_P")

# ── OpenTelemetry imports (graceful fallback) ────────────────────────────────

try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
    )
    from opentelemetry.semconv.resource import ResourceAttributes
    from opentelemetry.trace import Status, StatusCode

    SERVICE_NAME = ResourceAttributes.SERVICE_NAME
    SERVICE_VERSION = ResourceAttributes.SERVICE_VERSION
    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False
    trace = None

# ── Global state ─────────────────────────────────────────────────────────────

_tracer: Any = None
_provider: Any = None


# ── Init / Teardown ─────────────────────────────────────────────────────────


def init_tracing(
    service_name: str = "beagle",
    service_version: str = "13.8.1",
    export_to_console: bool = True,
    otlp_endpoint: str | None = None,
) -> bool:
    """Initialize OpenTelemetry tracing.

    Returns True if tracing initialized successfully, False otherwise.
    """
    global _tracer, _provider

    if not OTEL_AVAILABLE:
        logger.debug("OpenTelemetry not installed — tracing disabled")
        return False

    try:
        resource = Resource.create(
            {
                SERVICE_NAME: service_name,
                SERVICE_VERSION: service_version,
                "deployment.environment": os.getenv("BEAGLE_ENV", "development"),
            }
        )

        _provider = TracerProvider(resource=resource)

        if export_to_console or not otlp_endpoint:
            _provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

        if otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )

                _provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
                )
                logger.info(f"Tracing OTLP export enabled → {otlp_endpoint}")
            except ImportError:
                logger.warning(
                    "OTLP exporter not installed — pip install opentelemetry-exporter-otlp"
                )

        trace.set_tracer_provider(_provider)
        _tracer = trace.get_tracer(service_name, service_version)
        logger.info(f"Tracing initialized: service={service_name} v{service_version}")
        return True

    except Exception as e:  # broad catch intentional
        logger.exception(f"Failed to initialize tracing: {e}")
        return False


def shutdown_tracing() -> None:
    """Flush and shut down the tracer provider."""
    global _provider
    if _provider is not None and hasattr(_provider, "shutdown"):
        _provider.shutdown()
        _provider = None


def get_tracer() -> TracerLike | None:
    """Get the global tracer instance (no-op tracer if OTel unavailable)."""
    global _tracer
    if _tracer is None and OTEL_AVAILABLE and trace is not None:
        _tracer = trace.get_tracer("beagle")
    return _tracer


# ── Span helpers ─────────────────────────────────────────────────────────────


@contextmanager
def span(
    name: str,
    attributes: dict[str, SpanAttribute] | None = None,
    record_exception: bool = True,
) -> Iterator[SpanLike | None]:
    """Context manager for creating a traced span.

    Yields the span object, or ``None`` if tracing is not available.
    """
    tracer = get_tracer()
    if tracer is None:
        yield None
        return

    with tracer.start_as_current_span(name) as current_span:
        if attributes:
            for key, value in attributes.items():
                current_span.set_attribute(key, str(value) if value is not None else "")
        try:
            yield current_span
        except Exception as e:  # broad catch intentional
            if record_exception:
                current_span.set_status(Status(StatusCode.ERROR, str(e)))
                current_span.record_exception(e)
            raise


def trace_async(
    name: str, attributes: dict[str, Any] | None = None
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator for tracing async functions."""

    def decorator[**P, R](func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with span(name, attributes) as s:
                try:
                    result = await func(*args, **kwargs)
                    return result
                except Exception as e:  # broad catch intentional
                    if s is not None:
                        s.set_status(Status(StatusCode.ERROR, str(e)))
                        s.record_exception(e)
                    raise

        return wrapper

    return decorator


def add_event(name: str, attributes: dict | None = None) -> None:
    """Add an event to the current span."""
    if not OTEL_AVAILABLE or trace is None:
        return
    current = trace.get_current_span()
    if current and current.is_recording():
        current.add_event(name, attributes=attributes)


def set_attribute(key: str, value: SpanAttribute | None) -> None:
    """Set an attribute on the current span."""
    if not OTEL_AVAILABLE or trace is None:
        return
    current = trace.get_current_span()
    if current and current.is_recording():
        current.set_attribute(key, str(value) if value is not None else "")


def record_exception(exc: Exception) -> None:
    """Record an exception on the current span."""
    if not OTEL_AVAILABLE or trace is None:
        return
    current = trace.get_current_span()
    if current and current.is_recording():
        current.record_exception(exc)
        current.set_status(Status(StatusCode.ERROR, str(exc)))


class TracingContext:
    """Context manager for grouping related spans under a parent.

    Usage::

        with TracingContext("workflow.run") as ctx:
            with ctx.child_span("node.execute"):
                ...
    """

    def __init__(self, name: str, attributes: dict | None = None):
        self.name = name
        self.attributes = attributes
        self._span: SpanLike | None = None
        self._token: object | None = None

    def __enter__(self) -> Self:
        tracer = get_tracer()
        if tracer is not None:
            self._span = tracer.start_span(self.name)
            if self.attributes:
                for k, v in self.attributes.items():
                    self._span.set_attribute(k, str(v) if v is not None else "")
            self._token = trace.context_api.attach(trace.set_span_in_context(self._span))
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        _exc_tb: TracebackType | None,
    ) -> None:
        if self._span is not None:
            if exc_val is not None:
                self._span.set_status(Status(StatusCode.ERROR, str(exc_val)))
                self._span.record_exception(exc_val)
            self._span.end()
        if self._token is not None:
            trace.context_api.detach(self._token)

    def child_span(
        self, name: str, attributes: dict[str, SpanAttribute] | None = None
    ) -> AbstractContextManager[SpanLike | None]:
        """Create a child span within this context."""
        return span(name, attributes)
