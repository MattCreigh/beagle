"""OTel GenAI semantic convention helpers.

Provides span creation helpers that follow the OpenTelemetry GenAI
semantic conventions (https://opentelemetry.io/docs/specs/semconv/gen-ai/).

These conventions standardize attribute names across GenAI operations:
- ``gen_ai.system`` — the AI system (e.g., "ollama", "openai")
- ``gen_ai.request.model`` — model name
- ``gen_ai.request.max_tokens`` — max token budget
- ``gen_ai.response.model`` — actual model used
- ``gen_ai.usage.input_tokens`` — tokens consumed
- ``gen_ai.usage.output_tokens`` — tokens produced
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from enum import StrEnum
from typing import Any

from beagle.observability.tracing import (
    span,
)

logger = logging.getLogger("Beagle.observability.genai")


class GenAISpanKind(StrEnum):
    """GenAI operation types for span naming."""

    CHAT = "gen_ai.chat"
    COMPLETION = "gen_ai.completion"
    EMBEDDING = "gen_ai.embedding"
    NODE_EXECUTION = "gen_ai.node.execute"
    WORKFLOW_RUN = "gen_ai.workflow.run"
    COMPACTION = "gen_ai.compaction"
    MCP_TOOL = "gen_ai.mcp.tool"
    RAG_SEARCH = "gen_ai.rag.search"
    COST_CHECK = "gen_ai.cost.check"


# ── GenAI span attributes ────────────────────────────────────────────────────

# Standard OTel GenAI semantic convention attribute keys
ATTR_SYSTEM = "gen_ai.system"
ATTR_REQUEST_MODEL = "gen_ai.request.model"
ATTR_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
ATTR_REQUEST_TEMPERATURE = "gen_ai.request.temperature"
ATTR_RESPONSE_MODEL = "gen_ai.response.model"
ATTR_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
ATTR_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
ATTR_USAGE_TOTAL_TOKENS = "gen_ai.usage.total_tokens"

# Beagle-specific extensions
ATTR_NODE_NAME = "beagle.node.name"
ATTR_WORKFLOW_ID = "beagle.workflow.id"
ATTR_COST_USD = "beagle.cost.usd"
ATTR_RECIPE_NAME = "beagle.recipe.name"
ATTR_EVH_PASSED = "beagle.evh.passed"


# ── genai_span context manager ───────────────────────────────────────────────


@contextmanager
def genai_span(
    kind: GenAISpanKind | str,
    model: str = "",
    node_name: str = "",
    workflow_id: str = "",
    system: str = "ollama",
    extra_attributes: dict[str, Any] | None = None,
):
    """Create a GenAI-attributed span following OTel semantic conventions.

    Args:
        kind: The GenAI operation type (e.g., GenAISpanKind.NODE_EXECUTION).
        model: Model name (e.g., "glm-5.1:cloud").
        node_name: Beagle DAG node name.
        workflow_id: Beagle workflow identifier.
        system: GenAI system name (default "ollama").
        extra_attributes: Additional span attributes.

    Yields:
        A ``GenAISpanContext`` with methods to record usage after completion.

    Example::

        with genai_span(GenAISpanKind.NODE_EXECUTION, model="glm-5.1:cloud",
                         node_name="research") as gs:
            result = await execute_node(...)
            gs.set_usage(input_tokens=1500, output_tokens=800)
            gs.set_cost(0.0023)

    """
    attributes = {
        ATTR_SYSTEM: system,
        ATTR_REQUEST_MODEL: model,
        ATTR_NODE_NAME: node_name,
        ATTR_WORKFLOW_ID: workflow_id,
    }
    if extra_attributes:
        attributes.update(extra_attributes)

    start_time = time.monotonic()

    with span(str(kind), attributes) as otel_span:
        ctx = GenAISpanContext(otel_span, start_time)
        try:
            yield ctx
        finally:
            # Record duration as event
            duration = time.monotonic() - start_time
            if otel_span is not None and otel_span.is_recording():
                otel_span.set_attribute("gen_ai.operation.duration_s", duration)

            # Record to metrics collector
            try:
                from beagle.observability.metrics import (
                    get_metrics_collector,
                )

                collector = get_metrics_collector()
                if ctx._input_tokens or ctx._output_tokens:
                    collector.record_token_usage(
                        input_tokens=ctx._input_tokens,
                        output_tokens=ctx._output_tokens,
                        model=model,
                        node_name=node_name,
                    )
                if duration > 0:
                    collector.record_operation_duration(duration, model=model, node_name=node_name)
                if ctx._cost_usd > 0:
                    collector.record_cost(ctx._cost_usd, model=model, node_name=node_name)
            except (ImportError, AttributeError, TypeError, ValueError, OSError) as exc:
                logger.warning(
                    "Cannot record GenAI metrics for model %r (%s); this operation is "
                    "missing from token, duration and cost totals.",
                    model,
                    exc,
                )


class GenAISpanContext:
    """Helper returned by ``genai_span`` for recording usage after execution."""

    def __init__(self, otel_span: Any, start_time: float) -> None:
        self._span = otel_span
        self._start_time = start_time
        self._input_tokens = 0
        self._output_tokens = 0
        self._cost_usd = 0.0

    def set_usage(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        """Record token usage on the span."""
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        total = input_tokens + output_tokens
        if self._span is not None and self._span.is_recording():
            self._span.set_attribute(ATTR_USAGE_INPUT_TOKENS, input_tokens)
            self._span.set_attribute(ATTR_USAGE_OUTPUT_TOKENS, output_tokens)
            self._span.set_attribute(ATTR_USAGE_TOTAL_TOKENS, total)

    def set_cost(self, cost_usd: float) -> None:
        """Record dollar cost on the span."""
        self._cost_usd = cost_usd
        if self._span is not None and self._span.is_recording():
            self._span.set_attribute(ATTR_COST_USD, cost_usd)

    def set_response_model(self, model: str) -> None:
        """Record actual model used (may differ from requested)."""
        if self._span is not None and self._span.is_recording():
            self._span.set_attribute(ATTR_RESPONSE_MODEL, model)

    def set_evh_result(self, passed: bool) -> None:
        """Record EVH validation result."""
        if self._span is not None and self._span.is_recording():
            self._span.set_attribute(ATTR_EVH_PASSED, passed)
