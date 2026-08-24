"""Pydantic schemas for structured LLM output in Beagle workflows.

Provides reusable BaseModel subclasses that can be referenced by name
in workflow YAML via `output_schema: SchemaName`.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

logger = logging.getLogger("Beagle.bridges.schemas")


class FinalAnswer(BaseModel):
    """Generic final answer with confidence."""

    answer: str = Field(description="The final textual answer")
    confidence: float = Field(ge=0.0, le=1.0, default=0.8)


class CodeReview(BaseModel):
    """Structured code review output."""

    findings: list[str] = Field(description="List of specific findings")
    severity: str = Field(description="Overall severity: low | medium | high | critical")


class ResearchSummary(BaseModel):
    """Structured research synthesis."""

    sources: list[str] = Field(description="Source URLs or citations")
    summary: str = Field(description="Concise synthesis of findings")


class TaskPlan(BaseModel):
    """Structured task decomposition plan."""

    steps: list[str] = Field(description="Ordered list of steps")
    estimated_complexity: str = Field(description="low | medium | high")


_SCHEMA_REGISTRY: dict[str, type[BaseModel]] = {
    "FinalAnswer": FinalAnswer,
    "CodeReview": CodeReview,
    "ResearchSummary": ResearchSummary,
    "TaskPlan": TaskPlan,
}


def get_schema_by_name(name: str) -> type[BaseModel] | None:
    """Resolve a schema class from its registered name.

    Args:
        name: Schema name as used in workflow YAML (e.g. "FinalAnswer").

    Returns:
        The Pydantic model class, or None if not found.

    """
    return _SCHEMA_REGISTRY.get(name)


def list_schema_names() -> list[str]:
    """Return all registered schema names."""
    return list(_SCHEMA_REGISTRY.keys())
