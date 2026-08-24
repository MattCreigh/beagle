"""
TOML Task Specification Schema
==============================
Pydantic models for validating task specifications loaded from TOML files.

This module provides structured task configuration that integrates with
the OpenClaw task queue and Beagle workflow system.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class TaskType(StrEnum):
    """Valid task types for OpenClaw."""

    WORKFLOW = "workflow"
    SKILL = "skill"
    DELEGATE = "delegate"


class TaskPriority(StrEnum):
    """Task priority levels."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class ModelConfig(BaseModel):
    """Model configuration for task execution."""

    provider: str = Field(default="ollama_cloud", description="LLM provider")
    model: str = Field(default="glm-5:cloud", description="Model identifier")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, description="Max output tokens")
    fallback_models: list[str] = Field(default_factory=lambda: ["glm-5.1:cloud", "llama3.1:8b"])


class BudgetConfig(BaseModel):
    """Budget and resource constraints."""

    max_cost_usd: float = Field(default=5.0, ge=0.0, description="Maximum cost in USD")
    max_tokens: int | None = Field(default=None, description="Token budget")
    timeout_seconds: int = Field(default=600, ge=60, description="Execution timeout")
    max_retries: int = Field(default=3, ge=0, description="Retry attempts on failure")


class WorkflowConfig(BaseModel):
    """Workflow-specific configuration."""

    name: str = Field(description="Workflow name (e.g., 'research', 'develop')")
    mode: str = Field(default="develop", description="Workflow mode: develop, research, audit")
    loop_count: int = Field(default=1, ge=1, description="Number of execution loops")
    checkpoint_interval: int = Field(default=10, description="Checkpoint save interval")
    enable_web_search: bool = Field(default=True, description="Allow web search in execution")
    approve_all: bool = Field(default=True, description="Auto-approve all actions")


class OutputConfig(BaseModel):
    """Output and artifact configuration."""

    output_dir: str = Field(default="ai/analysis_reports", description="Output directory")
    file_prefix: str | None = Field(default=None, description="Output file prefix")
    format: str = Field(default="markdown", description="Output format: markdown, json, yaml")
    include_metadata: bool = Field(default=True, description="Include execution metadata")
    compress_large_outputs: bool = Field(default=True, description="Compress outputs >100KB")


class AuditConfig(BaseModel):
    """Audit and logging configuration."""

    enabled: bool = Field(default=True, description="Enable audit logging")
    level: str = Field(default="INFO", description="Log level: DEBUG, INFO, WARNING, ERROR")
    log_to_file: bool = Field(default=True, description="Log to file")
    log_to_db: bool = Field(default=True, description="Log to SQLite database")
    mask_secrets: bool = Field(default=True, description="Mask API keys and secrets")
    retain_days: int = Field(default=30, description="Log retention period")


class HookSpec(BaseModel):
    """Hook specification for task lifecycle events."""

    event: str = Field(description="Event name: pre_start, post_complete, on_error, on_retry")
    command: str = Field(description="Shell command to execute")
    timeout: int = Field(default=30, description="Hook timeout in seconds")
    continue_on_failure: bool = Field(default=False, description="Continue task if hook fails")


class TaskSpec(BaseModel):
    """
    Complete task specification loaded from TOML.

    This is the root model that combines all configuration sections.
    """

    # Task identification
    task_id: str | None = Field(default=None, description="Unique task ID (auto-generated if None)")
    task_type: TaskType = Field(default=TaskType.WORKFLOW, description="Task type")
    name: str = Field(description="Human-readable task name")
    description: str = Field(default="", description="Task description")
    priority: TaskPriority = Field(default=TaskPriority.NORMAL)
    tags: list[str] = Field(default_factory=list, description="Task tags for categorization")

    # Inheritance - allows tasks to inherit from template TOML files
    extends: str | None = Field(default=None, description="Template TOML to inherit from")

    # Core query/instructions
    query: str = Field(description="The main task query or instruction")
    context_files: list[str] = Field(
        default_factory=list, description="Files to include as context"
    )

    # Configuration sections
    model: ModelConfig = Field(default_factory=ModelConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    workflow: WorkflowConfig
    output: OutputConfig = Field(default_factory=OutputConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)

    # Hooks
    hooks: list[HookSpec] = Field(default_factory=list, description="Lifecycle hooks")

    # Metadata
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_by: str = Field(default="goose")
    version: str = Field(default="1.0")

    @field_validator("workflow", mode="before")
    @classmethod
    def set_default_workflow(cls, v, info):
        """Set default workflow name from task name if not provided."""
        if isinstance(v, dict) and "name" not in v and "name" in info.data:
            v["name"] = info.data["name"]
        return v

    def to_openclaw_spec(self) -> dict[str, Any]:
        """Convert to OpenClaw MCP server spec format."""
        return {
            "workflow": self.workflow.name,
            "query": self.query,
            "model": self.model.model,
            "mode": self.workflow.mode,
        }

    def to_openclaw_constraints(self) -> dict[str, Any]:
        """Convert to OpenClaw constraints format."""
        return {
            "budget_usd": self.budget.max_cost_usd,
            "timeout_seconds": self.budget.timeout_seconds,
            "max_retries": self.budget.max_retries,
        }

    def to_openclaw_audit_config(self) -> dict[str, Any]:
        """Convert to audit config for OpenClaw."""
        return {
            "enabled": self.audit.enabled,
            "log_level": self.audit.level,
            "log_to_file": self.audit.log_to_file,
            "log_to_db": self.audit.log_to_db,
            "mask_secrets": self.audit.mask_secrets,
        }


# Template defaults for common workflows
RESEARCH_TEMPLATE = TaskSpec(
    name="research",
    description="Research workflow template",
    query="",
    workflow=WorkflowConfig(
        name="research",
        mode="research",
        loop_count=1,
        enable_web_search=True,
        approve_all=True,
    ),
    model=ModelConfig(model="glm-5:cloud"),
    budget=BudgetConfig(max_cost_usd=1.0, timeout_seconds=600),
    output=OutputConfig(file_prefix="research_", format="markdown"),
)

DEVELOP_TEMPLATE = TaskSpec(
    name="develop",
    description="Development workflow template",
    query="",
    workflow=WorkflowConfig(
        name="develop",
        mode="develop",
        loop_count=1,
        enable_web_search=False,
        approve_all=True,
    ),
    model=ModelConfig(model="glm-5:cloud"),
    budget=BudgetConfig(max_cost_usd=3.0, timeout_seconds=900),
    output=OutputConfig(file_prefix="develop_", format="markdown"),
)

SELF_IMPROVEMENT_TEMPLATE = TaskSpec(
    name="self-improvement",
    description="Self-improvement workflow template",
    query="",
    workflow=WorkflowConfig(
        name="self-improvement",
        mode="develop",
        loop_count=1,
        enable_web_search=True,
        approve_all=True,
    ),
    model=ModelConfig(model="glm-5:cloud"),
    budget=BudgetConfig(max_cost_usd=5.0, timeout_seconds=1200),
    output=OutputConfig(file_prefix="improve_", format="markdown"),
)
