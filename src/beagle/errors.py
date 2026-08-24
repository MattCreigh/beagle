"""Custom exception classes for Goose Agentic Workflow.

Provides descriptive error messages with suggestions for resolution.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SecurityAccessViolation(Exception):
    """Raised when an agent operation violates security policy.

    This exception is designed to be caught by the semantic translation
    layer in subprocess_pool.py so that fallback LLM models receive a
    structured, polite guidance prompt instead of a raw Python traceback.

    Attributes:
        restriction: Human-readable description of what was blocked.
        suggestion: A safe alternative the agent can pursue.
        severity: One of "low", "medium", "high", "critical".

    """

    def __init__(
        self,
        restriction: str,
        suggestion: str = "",
        severity: str = "medium",
    ) -> None:
        self.restriction = restriction
        self.suggestion = suggestion
        self.severity = severity
        msg = f"Security policy violation: {restriction}"
        if suggestion:
            msg += f" — Suggested alternative: {suggestion}"
        super().__init__(msg)


class SandboxViolation(SecurityAccessViolation):
    """Raised when an agent attempts an operation outside the sandbox.

    A specialization of SecurityAccessViolation for sandbox boundary
    breaches (file access, network calls, etc.).
    """

    def __init__(
        self,
        attempted_action: str,
        sandbox_boundary: str = "",
        suggestion: str = "",
    ) -> None:
        self.attempted_action = attempted_action
        self.sandbox_boundary = sandbox_boundary
        super().__init__(
            restriction=f"Sandbox boundary violation: {attempted_action}",
            suggestion=suggestion
            or f"Use an approved read-only alternative to: {attempted_action}",
            severity="high",
        )


class GooseWorkflowError(Exception):
    """Base exception for all workflow errors."""

    def __init__(
        self,
        message: str,
        suggestion: str | None = None,
        docs_link: str | None = None,
        context: dict[str, Any] | None = None,
    ):
        """Initialize workflow error.

        Args:
            message: Error message
            suggestion: Suggested fix
            docs_link: Link to relevant documentation
            context: Additional context data

        """
        super().__init__(message)
        self.message = message
        self.suggestion = suggestion
        self.docs_link = docs_link
        self.context = context or {}

    def __str__(self) -> str:
        parts = [self.message]

        if self.suggestion:
            parts.append(f"\nSuggestion: {self.suggestion}")

        if self.docs_link:
            parts.append(f"\nDocs: {self.docs_link}")

        return "".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "error": self.__class__.__name__,
            "message": self.message,
            "suggestion": self.suggestion,
            "docs_link": self.docs_link,
            "context": self.context,
        }


class RecipeNotFoundError(GooseWorkflowError):
    """Recipe file not found."""

    def __init__(self, recipe_name: str, recipes_dir: Path | None = None):
        """Initialize recipe not found error.

        Args:
            recipe_name: Name of the missing recipe
            recipes_dir: Path to recipes directory

        """
        message = f"Recipe not found: {recipe_name}"

        suggestion = "Check that the recipe exists in the recipes/ directory"
        if recipes_dir:
            suggestion = f"Check that the recipe exists in: {recipes_dir}"

        super().__init__(
            message=message,
            suggestion=suggestion,
            context={"recipe_name": recipe_name},
        )
        self.recipe_name = recipe_name


class WorkflowNotFoundError(GooseWorkflowError):
    """Workflow file not found."""

    def __init__(self, workflow_name: str, metaprompts_dir: Path | None = None):
        """Initialize workflow not found error.

        Args:
            workflow_name: Name of the missing workflow
            metaprompts_dir: Path to metaprompts directory

        """
        message = f"Workflow not found: {workflow_name}"

        suggestion = "Use 'goose-workflow list' to see available workflows"
        if metaprompts_dir:
            suggestion = f"Check workflows in: {metaprompts_dir}"

        super().__init__(
            message=message,
            suggestion=suggestion,
            context={"workflow_name": workflow_name},
        )
        self.workflow_name = workflow_name


class WorkflowValidationError(GooseWorkflowError):
    """Workflow specification is invalid."""

    def __init__(self, workflow_name: str, errors: list[str]):
        """Initialize validation error.

        Args:
            workflow_name: Name of the invalid workflow
            errors: List of validation errors

        """
        message = f"Workflow validation failed: {workflow_name}"
        suggestion = "Fix these issues:\n  - " + "\n  - ".join(errors)

        super().__init__(
            message=message,
            suggestion=suggestion,
            context={"workflow_name": workflow_name, "errors": errors},
        )
        self.workflow_name = workflow_name
        self.errors = errors


class BudgetExceededError(GooseWorkflowError):
    """Budget limit exceeded."""

    def __init__(self, current: float, budget: float, workflow_id: str = ""):
        """Initialize budget exceeded error.

        Args:
            current: Current cost
            budget: Budget limit
            workflow_id: Workflow identifier

        """
        message = f"Budget exceeded: ${current:.4f} / ${budget:.2f}"

        suggestion = (
            "Options:\n"
            "  1. Increase budget with --budget flag\n"
            "  2. Use a cheaper model (e.g., copilot-flash)\n"
            "  3. Use caching to reduce API calls"
        )

        super().__init__(
            message=message,
            suggestion=suggestion,
            context={
                "current_usd": current,
                "budget_usd": budget,
                "workflow_id": workflow_id,
            },
        )
        self.current = current
        self.budget = budget


class EVHValidationError(GooseWorkflowError):
    """EVH validation failed."""

    def __init__(
        self,
        node_name: str,
        validation_result: str,
        attempt: int = 1,
    ):
        """Initialize validation failed error.

        Args:
            node_name: Name of the failed node
            validation_result: Validation result message
            attempt: Attempt number

        """
        message = f"EVH validation failed for {node_name}"

        suggestion = (
            "The security auditor flagged potential issues.\n"
            "Review the node output for:\n"
            "  - Unsafe code patterns\n"
            "  - Potential hallucinations\n"
            "  - Security vulnerabilities"
        )

        super().__init__(
            message=message,
            suggestion=suggestion,
            context={
                "node_name": node_name,
                "validation_result": validation_result[:500],
                "attempt": attempt,
            },
        )
        self.node_name = node_name
        self.validation_result = validation_result


# Alias for backwards compatibility
ValidationFailedError = EVHValidationError


class NodeExecutionError(GooseWorkflowError):
    """Node execution failed."""

    def __init__(
        self,
        node_name: str,
        error: str,
        stderr: str = "",
        attempt: int = 1,
    ):
        """Initialize node execution error.

        Args:
            node_name: Name of the failed node
            error: Error message
            stderr: Standard error output
            attempt: Attempt number

        """
        message = f"Node execution failed: {node_name}"

        suggestion = (
            f"Check the error output:\n{error[:200]}\n\n"
            "Common causes:\n"
            "  - Recipe file issues\n"
            "  - API errors\n"
            "  - Timeout"
        )

        super().__init__(
            message=message,
            suggestion=suggestion,
            context={
                "node_name": node_name,
                "error": error[:500],
                "stderr": stderr[:500],
                "attempt": attempt,
            },
        )
        self.node_name = node_name
        self.stderr = stderr


class QueryRejectedError(GooseWorkflowError):
    """User query rejected by security validation."""

    def __init__(self, reason: str, query: str = ""):
        """Initialize query rejected error.

        Args:
            reason: Rejection reason
            query: The rejected query (truncated)

        """
        message = f"Query rejected: {reason}"

        suggestion = (
            "The query was blocked by security validation.\n"
            "Please modify your query to:\n"
            "  - Remove any prompt injection attempts\n"
            "  - Keep query under length limits\n"
            "  - Avoid dangerous commands"
        )

        super().__init__(
            message=message,
            suggestion=suggestion,
            context={"reason": reason, "query": query[:100]},
        )
        self.reason = reason


class CheckpointError(GooseWorkflowError):
    """Checkpoint operation failed."""

    def __init__(self, operation: str, workflow_id: str, details: str = ""):
        """Initialize checkpoint error.

        Args:
            operation: The failed operation (save/load/delete)
            workflow_id: Workflow identifier
            details: Additional details

        """
        message = f"Checkpoint {operation} failed for {workflow_id}"

        suggestion = "Check checkpoint files in the checkpoints/ directory"
        if details:
            suggestion += f"\nDetails: {details}"

        super().__init__(
            message=message,
            suggestion=suggestion,
            context={
                "operation": operation,
                "workflow_id": workflow_id,
            },
        )


class RateLimitError(GooseWorkflowError):
    """Rate limit exceeded."""

    def __init__(
        self,
        limit_type: str,
        current: float,
        limit: float,
        wait_seconds: float = 0,
    ):
        """Initialize rate limit error.

        Args:
            limit_type: Type of limit (requests/tokens)
            current: Current usage
            limit: The limit
            wait_seconds: Suggested wait time

        """
        message = f"Rate limit exceeded: {limit_type}"

        suggestion = f"Wait {wait_seconds:.1f}s before retrying"
        if wait_seconds > 60:
            suggestion = f"Wait ~{wait_seconds / 60:.1f} minutes before retrying"

        super().__init__(
            message=message,
            suggestion=suggestion,
            context={
                "limit_type": limit_type,
                "current": current,
                "limit": limit,
                "wait_seconds": wait_seconds,
            },
        )
        self.wait_seconds = wait_seconds


class ServerError(GooseWorkflowError):
    """Transient HTTP 500/502/503 server error from an upstream provider.

    These are infrastructure failures (not model failures) and the
    fallback chain should not penalize the circuit breaker for them.
    Typically indicates Ollama Cloud capacity issues, provider outages,
    or load-balancer hiccups.

    Unlike :class:`RateLimitError` (HTTP 429 — caller must slow down),
    ServerError indicates the server is the one having a bad time, so
    callers should retry the request — either on the same model or by
    falling back to an alternative provider — without consuming the
    model's circuit-breaker failure budget.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        provider: str = "",
    ):
        """Initialize transient server error.

        Args:
            message: Human-readable error description.
            status_code: HTTP status code (500, 502, or 503) if known.
            provider: Provider that emitted the error (e.g. "ollama").

        """
        suggestion = (
            "This is a transient upstream server error (HTTP 500/502/503),\n"
            "not a model failure. The fallback chain will try the next\n"
            "model/provider without tripping the circuit breaker.\n"
            "If this persists, check the upstream provider's status page."
        )
        super().__init__(
            message=message,
            suggestion=suggestion,
            context={"status_code": status_code, "provider": provider},
        )
        self.status_code = status_code
        self.provider = provider


class ConfigurationError(GooseWorkflowError):
    """Configuration is invalid or missing."""

    def __init__(self, config_key: str, details: str = ""):
        """Initialize configuration error.

        Args:
            config_key: The problematic config key
            details: Additional details

        """
        message = f"Configuration error: {config_key}"

        suggestion = (
            "Check config.toml and environment variables.\n"
            "Run 'goose-workflow config init' to create default config."
        )
        if details:
            suggestion = details + "\n\n" + suggestion

        super().__init__(
            message=message,
            suggestion=suggestion,
            context={"config_key": config_key},
        )


def format_error_rich(error: GooseWorkflowError) -> str:
    """Format error for rich console output.

    Args:
        error: The error to format

    Returns:
        Rich-formatted error string

    """
    lines = [
        f"[bold red]Error: {error.__class__.__name__}[/bold red]",
        "",
        error.message,
    ]

    if error.suggestion:
        lines.extend(
            [
                "",
                "[yellow]Suggestion:[/yellow]",
                error.suggestion,
            ]
        )

    if error.docs_link:
        lines.extend(
            [
                "",
                f"[dim]Docs: {error.docs_link}[/dim]",
            ]
        )

    return "\n".join(lines)


if __name__ == "__main__":
    # Demo errors
    errors = [
        RecipeNotFoundError("custom-analyzer"),
        BudgetExceededError(12.34, 10.0),
        ValidationFailedError("ExecutionPhase", "FAIL: Unsafe code pattern detected"),
        QueryRejectedError("Prompt injection detected"),
    ]

    for e in errors:
        logger.info(f"\n{'=' * 60}")
        logger.info(e)
        logger.info(f"{'=' * 60}")
