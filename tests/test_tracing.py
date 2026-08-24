"""Tests for Tracing Module.

Comprehensive tests for:
- Trace context management
- Span creation and management
- Trace ID generation
- OpenTelemetry integration
"""

from __future__ import annotations

# Add project root to path
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TestTracingImports:
    """Test that tracing components can be imported."""

    def test_import_tracing_module(self):
        """Tracing module can be imported."""
        from beagle.utils import tracing

        assert tracing is not None

    def test_import_tracing_context(self):
        """TracingContext is the real grouping primitive."""
        from beagle.utils.tracing import TracingContext

        assert TracingContext is not None

    def test_import_span(self):
        """span() is the real span context manager."""
        from beagle.utils.tracing import span

        assert callable(span)


class TestTracingContextAPI:
    """TracingContext groups child spans under a parent.

    v1.0.0: this file previously tested a speculative API that was never
    built — TraceContext(trace_id=..., span_id=...), TraceContext.current(),
    and a Span class with .end()/.duration/.attributes/.events. Every one of
    those tests sat permanently skipped behind `except ImportError`, so the
    tracing module that *is* implemented had no coverage at all. These
    exercise the real surface: TracingContext, its child_span(), and span().
    """

    def test_context_enter_exit(self):
        """TracingContext works as a context manager and records its name."""
        from beagle.utils.tracing import TracingContext

        with TracingContext("workflow.run") as ctx:
            assert ctx.name == "workflow.run"
        # Exiting must not raise whether or not an OTel tracer is configured.

    def test_context_accepts_attributes(self):
        """Attributes passed to TracingContext are retained."""
        from beagle.utils.tracing import TracingContext

        attrs = {"workflow_id": "wf-1", "nodes": 3}
        with TracingContext("workflow.run", attributes=attrs) as ctx:
            assert ctx.attributes == attrs

    def test_child_span_is_a_context_manager(self):
        """child_span() yields a context manager usable inside the parent."""
        from beagle.utils.tracing import TracingContext

        with TracingContext("workflow.run") as ctx, ctx.child_span("node.execute"):
            pass

    def test_exception_propagates_through_context(self):
        """An exception inside the context is recorded and re-raised."""
        from beagle.utils.tracing import TracingContext

        with pytest.raises(ValueError, match="boom"), TracingContext("workflow.run"):
            raise ValueError("boom")


class TestSpanAPI:
    """span() is a context manager yielding the span, or None without OTel."""

    def test_span_yields_without_error(self):
        """span() is usable regardless of whether OTel is configured."""
        from beagle.utils.tracing import span

        with span("test_operation") as s:
            # Yields the OTel span, or None when tracing is unavailable.
            assert s is None or hasattr(s, "set_attribute")

    def test_span_accepts_attributes(self):
        """span() accepts an attributes mapping without raising."""
        from beagle.utils.tracing import span

        with span("test_operation", attributes={"key": "value", "count": 42}) as s:
            assert s is None or hasattr(s, "set_attribute")

    def test_span_reraises_exceptions(self):
        """span() records the exception and re-raises it."""
        from beagle.utils.tracing import span

        with pytest.raises(RuntimeError, match="failed"), span("test_operation"):
            raise RuntimeError("failed")

    def test_span_helpers_are_callable(self):
        """The module-level span helpers exist and are callable."""
        from beagle.utils.tracing import add_event, record_exception, set_attribute

        for fn in (add_event, record_exception, set_attribute):
            assert callable(fn)


class TestTraceId:
    """Test Trace ID generation."""

    def test_tracer_get_tracer(self):
        """get_tracer returns a tracer if OpenTelemetry available."""
        try:
            from opentelemetry import trace as otel_trace

            # OpenTelemetry is available
            tracer = otel_trace.get_tracer("test")
            assert tracer is not None
        except ImportError:
            # OpenTelemetry not installed - this is OK
            pytest.skip("OpenTelemetry not installed")

    def test_init_tracing_function(self):
        """init_tracing function works."""
        from beagle.utils.tracing import init_tracing, shutdown_tracing

        # Should return bool indicating success
        try:
            result = init_tracing(service_name="test_service", export_to_console=True)
            assert isinstance(result, bool)
        finally:
            # export_to_console installs a global BatchSpanProcessor writing to
            # stdout. Without this shutdown it keeps flushing after pytest has
            # closed the captured stream, raising
            # "ValueError: I/O operation on closed file" during teardown.
            shutdown_tracing()


class TestTracingIntegration:
    """Integration tests for tracing."""

    def test_get_tracer(self):
        """get_tracer returns tracer instance or None if unavailable."""
        from beagle.utils.tracing import get_tracer

        tracer = get_tracer()

        # May be None if OpenTelemetry not installed
        # This is valid behavior
        if tracer is not None:
            # If available, should be a tracer
            assert hasattr(tracer, "start_span") or hasattr(tracer, "start_as_current_span")

    def test_start_span_if_available(self):
        """start_span works if OpenTelemetry available."""
        try:
            from opentelemetry import trace as otel_trace

            tracer = otel_trace.get_tracer("test")
            span = tracer.start_span("test_span")
            span.end()

            assert span is not None
        except ImportError:
            pytest.skip("OpenTelemetry not installed")

    def test_trace_context_manager(self):
        """Tracing can be used as context manager if available."""
        try:
            from opentelemetry import trace as otel_trace

            tracer = otel_trace.get_tracer("test")

            with tracer.start_as_current_span("operation"):
                # Span is active in context
                pass

            # Context exits cleanly
        except ImportError:
            pytest.skip("OpenTelemetry not installed")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
