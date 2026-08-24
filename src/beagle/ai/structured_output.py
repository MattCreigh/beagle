"""Structured Output Module — Instructor-based structured output for Beagle v13.4.

Provides a centralized Instructor client factory and Pydantic models
for structured LLM outputs across the orchestration pipeline.

Instructor wraps OpenAI/Anthropic clients with response_model validation,
ensuring outputs always conform to the expected schema.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

logger = logging.getLogger("Beagle.StructuredOutput")

# ---------------------------------------------------------------------------
# Instructor Client Factory
# ---------------------------------------------------------------------------

_instructor_client = None


def get_instructor_client(provider: str = "openai", model: str | None = None):
    """Get or create a cached Instructor client.

    Falls back gracefully if instructor is not installed.

    Args:
        provider: "openai" or "anthropic"
        model: Optional model override

    Returns:
        Instructor-wrapped client, or None if unavailable

    """
    global _instructor_client

    if _instructor_client is not None:
        return _instructor_client

    try:
        import instructor  # type: ignore[import-untyped]

        if provider == "openai":
            from openai import AsyncOpenAI

            client = instructor.from_openai(AsyncOpenAI())
        elif provider == "anthropic":
            try:
                from anthropic import AsyncAnthropic

                client = instructor.from_anthropic(AsyncAnthropic())  # type: ignore[attr-defined]
            except ImportError:
                logger.warning("Anthropic SDK not installed — falling back to OpenAI")
                from openai import AsyncOpenAI

                client = instructor.from_openai(AsyncOpenAI())
        else:
            logger.warning(f"Unknown provider: {provider}, defaulting to OpenAI")
            from openai import AsyncOpenAI

            client = instructor.from_openai(AsyncOpenAI())

        _instructor_client = client
        return client

    except ImportError:
        logger.info(
            "Instructor not installed — structured outputs unavailable. "
            "Install: pip install instructor"
        )
        return None


# ---------------------------------------------------------------------------
# Pydantic Models for Structured Orchestration Outputs
# ---------------------------------------------------------------------------


class TaskDecomposition(BaseModel):
    """Structured output for DAG orchestrator task decomposition."""

    subtasks: list[str] = Field(description="List of subtask descriptions to execute")
    dependencies: list[list[int]] = Field(
        description="Dependency graph: dependencies[i] = list of indices that task i depends on"
    )
    estimated_complexity: list[str] = Field(
        description="Complexity estimate per subtask: 'trivial', 'simple', 'moderate', 'complex'"
    )
    priority_order: list[int] = Field(
        description="Suggested execution order as list of task indices"
    )


class WorkflowClassification(BaseModel):
    """Structured output for query→workflow classification."""

    workflow: str = Field(description="Selected workflow name")
    confidence: float = Field(ge=0.0, le=1.0, description="Classification confidence")
    reasoning: str = Field(description="Why this workflow was selected")
    estimated_nodes: int = Field(
        ge=1, description="Estimated number of DAG nodes this workflow will produce"
    )
    risk_level: str = Field(description="Risk assessment: 'low', 'medium', 'high'")


class ValidationResult(BaseModel):
    """Structured output for adversarial node validation."""

    is_valid: bool = Field(description="Whether the output passes validation")
    issues: list[str] = Field(default_factory=list, description="List of issues found, if any")
    score: float = Field(ge=0.0, le=1.0, description="Validation quality score")
    suggested_fix: str | None = Field(default=None, description="Suggested fix if invalid")


class SteeringAssessment(BaseModel):
    """Structured output for mid-workflow steering assessment."""

    should_redirect: bool = Field(description="Whether workflow should be redirected")
    new_workflow: str | None = Field(default=None, description="New workflow to switch to")
    priority_guidance: str | None = Field(default=None, description="Priority guidance text")
    skip_nodes: list[str] = Field(default_factory=list, description="Nodes to skip")
    reasoning: str = Field(description="Why this steering decision was made")


# ---------------------------------------------------------------------------
# Convenience Functions
# ---------------------------------------------------------------------------


async def classify_query(
    query: str,
    available_workflows: list[str],
    model: str = "gpt-4o-mini",
) -> WorkflowClassification | None:
    """Classify a query using Instructor structured output.

    Returns None if Instructor is unavailable.
    """
    client = get_instructor_client()
    if client is None:
        return None

    try:
        result = await client.chat.completions.create(
            model=model,
            response_model=WorkflowClassification,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"You are a query classifier. Classify the user query into one of "
                        f"these workflows: {', '.join(available_workflows)}. "
                        f"Assess risk and estimate complexity."
                    ),
                },
                {"role": "user", "content": query},
            ],
            max_retries=2,
        )
        return result  # type: ignore[no-any-return]
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.error(f"Structured classification failed: {e}")
        return None


async def decompose_task(
    task_description: str,
    model: str = "gpt-4o-mini",
) -> TaskDecomposition | None:
    """Decompose a task into subtasks using Instructor structured output.

    Returns None if Instructor is unavailable.
    """
    client = get_instructor_client()
    if client is None:
        return None

    try:
        result = await client.chat.completions.create(
            model=model,
            response_model=TaskDecomposition,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a task decomposition engine. Break the given task into "
                        "independent subtasks with clear dependencies. Estimate complexity "
                        "for each subtask and suggest a priority execution order."
                    ),
                },
                {"role": "user", "content": task_description},
            ],
            max_retries=2,
        )
        return result  # type: ignore[no-any-return]
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.error(f"Structured task decomposition failed: {e}")
        return None


async def validate_output(
    original_query: str,
    output: str,
    model: str = "gpt-4o-mini",
) -> ValidationResult | None:
    """Validate workflow output against the original query using Instructor.

    Returns None if Instructor is unavailable.
    """
    client = get_instructor_client()
    if client is None:
        return None

    try:
        result = await client.chat.completions.create(
            model=model,
            response_model=ValidationResult,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a quality validator. Assess whether the output "
                        "effectively answers the original query. Check for errors, "
                        "omissions, and quality issues."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Original query: {original_query}\n\nOutput to validate:\n{output}",
                },
            ],
            max_retries=2,
        )
        return result  # type: ignore[no-any-return]
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.error(f"Structured validation failed: {e}")
        return None
