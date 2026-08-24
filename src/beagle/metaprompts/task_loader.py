"""
TOML Task Loader
================
Loads task specifications from TOML files with template inheritance support.

Usage:
    from task_loader import load_task_spec, list_available_tasks

    spec = load_task_spec("tasks/host_optimization.toml")
    logger.info(spec.to_openclaw_spec())
"""

from __future__ import annotations

import logging
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path

from beagle.config._config_path import find_metaprompts_dir
from beagle.metaprompts.task_schema import (
    DEVELOP_TEMPLATE,
    RESEARCH_TEMPLATE,
    SELF_IMPROVEMENT_TEMPLATE,
    AuditConfig,
    BudgetConfig,
    HookSpec,
    ModelConfig,
    OutputConfig,
    TaskPriority,
    TaskSpec,
    TaskType,
    WorkflowConfig,
)

log = logging.getLogger("Beagle.metaprompts.task_loader")

# Directory structure
# v1.1.1 (S5): metaprompts moved to the canonical config root; resolve them
# through find_metaprompts_dir().
# The metaprompts *data* (tasks/, templates/) is detached; only the loader
# code lives in the package. Resolve the data dirs through the canonical
# resolver.
_METAPROMPTS_DIR = find_metaprompts_dir()
TEMPLATES_DIR = _METAPROMPTS_DIR / "templates"
TASKS_DIR = _METAPROMPTS_DIR / "tasks"


# Template registry
TEMPLATE_REGISTRY: dict[str, TaskSpec] = {
    "research": RESEARCH_TEMPLATE,
    "develop": DEVELOP_TEMPLATE,
    "self-improvement": SELF_IMPROVEMENT_TEMPLATE,
}


def load_toml(toml_path: Path) -> dict:
    """Load a TOML file and return as dict."""
    if not toml_path.exists():
        raise FileNotFoundError(f"TOML file not found: {toml_path}")

    with open(toml_path, "rb") as f:
        return tomllib.load(f)


def merge_dicts(base: dict, override: dict) -> dict:
    """Deep merge two dictionaries. Override takes precedence."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


def resolve_inheritance(toml_data: dict, toml_path: Path) -> dict:
    """
    Resolve template inheritance for a task.

    Templates can be specified in 'extends' field. The loader will:
    1. Load the base template
    2. Merge it with the task-specific TOML
    3. Return the merged configuration
    """
    if "extends" not in toml_data:
        return toml_data

    template_name = toml_data["extends"]

    # Check built-in templates first
    if template_name in TEMPLATE_REGISTRY:
        template_spec = TEMPLATE_REGISTRY[template_name]
        base_dict = template_spec.model_dump()
    else:
        # Try to load from templates directory
        template_path = TEMPLATES_DIR / f"{template_name}.toml"
        if template_path.exists():
            base_dict = load_toml(template_path)
        else:
            log.warning(f"Template '{template_name}' not found, using defaults")
            return toml_data

    # Merge: base template + task-specific overrides
    merged = merge_dicts(base_dict, toml_data)
    # Remove 'extends' from final output
    merged.pop("extends", None)

    return merged


def parse_model_config(data: dict) -> ModelConfig:
    """Parse model configuration from TOML data."""
    model_data = data.get("model", {})
    return ModelConfig(
        provider=model_data.get("provider", "ollama_cloud"),
        model=model_data.get("model", "glm-5:cloud"),
        temperature=model_data.get("temperature", 0.7),
        max_tokens=model_data.get("max_tokens"),
        fallback_models=model_data.get("fallback_models", ["glm-5.1:cloud", "llama3.1:8b"]),
    )


def parse_budget_config(data: dict) -> BudgetConfig:
    """Parse budget configuration from TOML data."""
    budget_data = data.get("budget", {})
    return BudgetConfig(
        max_cost_usd=budget_data.get("max_cost_usd", 5.0),
        max_tokens=budget_data.get("max_tokens"),
        timeout_seconds=budget_data.get("timeout_seconds", 600),
        max_retries=budget_data.get("max_retries", 3),
    )


def parse_workflow_config(data: dict) -> WorkflowConfig:
    """Parse workflow configuration from TOML data."""
    wf_data = data.get("workflow", {})
    return WorkflowConfig(
        name=wf_data.get("name", data.get("name", "unknown")),
        mode=wf_data.get("mode", "develop"),
        loop_count=wf_data.get("loop_count", 1),
        checkpoint_interval=wf_data.get("checkpoint_interval", 10),
        enable_web_search=wf_data.get("enable_web_search", True),
        approve_all=wf_data.get("approve_all", True),
    )


def parse_output_config(data: dict) -> OutputConfig:
    """Parse output configuration from TOML data."""
    out_data = data.get("output", {})
    return OutputConfig(
        output_dir=out_data.get("output_dir", "ai/analysis_reports"),
        file_prefix=out_data.get("file_prefix"),
        format=out_data.get("format", "markdown"),
        include_metadata=out_data.get("include_metadata", True),
        compress_large_outputs=out_data.get("compress_large_outputs", True),
    )


def parse_audit_config(data: dict) -> AuditConfig:
    """Parse audit configuration from TOML data."""
    audit_data = data.get("audit", {})
    return AuditConfig(
        enabled=audit_data.get("enabled", True),
        level=audit_data.get("level", "INFO"),
        log_to_file=audit_data.get("log_to_file", True),
        log_to_db=audit_data.get("log_to_db", True),
        mask_secrets=audit_data.get("mask_secrets", True),
        retain_days=audit_data.get("retain_days", 30),
    )


def parse_hooks(data: dict) -> list[HookSpec]:
    """Parse hook specifications from TOML data."""
    hooks = []
    for hook_data in data.get("hooks", []):
        hooks.append(
            HookSpec(
                event=hook_data["event"],
                command=hook_data["command"],
                timeout=hook_data.get("timeout", 30),
                continue_on_failure=hook_data.get("continue_on_failure", False),
            )
        )
    return hooks


def load_task_spec(toml_path: Path, task_id: str | None = None) -> TaskSpec:
    """
    Load a task specification from a TOML file.

    Args:
        toml_path: Path to the TOML file
        task_id: Optional task ID (auto-generated if None)

    Returns:
        TaskSpec object with all configuration loaded

    Raises:
        FileNotFoundError: If TOML file doesn't exist
        ValueError: If TOML is missing required fields

    """
    # Load raw TOML
    raw_data = load_toml(toml_path)

    # Resolve inheritance
    resolved_data = resolve_inheritance(raw_data, toml_path)

    # Parse configurations
    task_type = TaskType(resolved_data.get("task_type", "workflow"))
    priority = TaskPriority(resolved_data.get("priority", "normal"))

    # Build TaskSpec
    spec = TaskSpec(
        task_id=task_id or str(uuid.uuid4()),
        task_type=task_type,
        name=resolved_data.get("name", toml_path.stem),
        description=resolved_data.get("description", ""),
        priority=priority,
        tags=resolved_data.get("tags", []),
        query=resolved_data.get("query", ""),
        context_files=resolved_data.get("context_files", []),
        model=parse_model_config(resolved_data),
        budget=parse_budget_config(resolved_data),
        workflow=parse_workflow_config(resolved_data),
        output=parse_output_config(resolved_data),
        audit=parse_audit_config(resolved_data),
        hooks=parse_hooks(resolved_data),
        created_at=datetime.now(UTC),
        created_by=resolved_data.get("created_by", "goose"),
        version=resolved_data.get("version", "1.0"),
    )

    log.info(f"Loaded task spec: {spec.task_id} ({spec.name}) from {toml_path}")
    return spec


def load_task_spec_by_name(name: str, task_id: str | None = None) -> TaskSpec:
    """
    Load a task by name from the tasks directory.

    Args:
        name: Task name (without .toml extension)
        task_id: Optional task ID

    Returns:
        TaskSpec object

    """
    # Try tasks directory first
    task_path = TASKS_DIR / f"{name}.toml"
    if task_path.exists():
        return load_task_spec(task_path, task_id)

    # Try templates directory
    template_path = TEMPLATES_DIR / f"{name}.toml"
    if template_path.exists():
        return load_task_spec(template_path, task_id)

    raise FileNotFoundError(f"Task '{name}' not found in tasks or templates directories")


def list_available_tasks() -> list[dict]:
    """
    List all available task specifications.

    Returns:
        List of dicts with task metadata: name, path, description

    """
    tasks = []

    # Scan tasks directory
    if TASKS_DIR.exists():
        for toml_file in TASKS_DIR.glob("*.toml"):
            try:
                data = load_toml(toml_file)
                tasks.append(
                    {
                        "name": toml_file.stem,
                        "path": str(toml_file),
                        "description": data.get("description", ""),
                        "task_type": data.get("task_type", "workflow"),
                        "priority": data.get("priority", "normal"),
                    }
                )
            except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
                log.warning(f"Failed to load task file {toml_file}: {e}")

    # Include built-in templates
    for name, template in TEMPLATE_REGISTRY.items():
        tasks.append(
            {
                "name": name,
                "path": "builtin",
                "description": template.description,
                "task_type": "template",
                "priority": "normal",
            }
        )

    return tasks


def create_task_from_template(
    template_name: str,
    query: str,
    overrides: dict | None = None,
    task_id: str | None = None,
) -> TaskSpec:
    """
    Create a task spec from a template with custom query.

    Args:
        template_name: Name of the template (research, develop, self-improvement)
        query: The task query/instructions
        overrides: Optional dict of configuration overrides
        task_id: Optional task ID

    Returns:
        TaskSpec object ready for submission

    """
    if template_name not in TEMPLATE_REGISTRY:
        raise ValueError(
            f"Unknown template: {template_name}. Available: {list(TEMPLATE_REGISTRY.keys())}"
        )

    template = TEMPLATE_REGISTRY[template_name]
    base_dict = template.model_dump()

    # Apply overrides
    if overrides:
        base_dict = merge_dicts(base_dict, overrides)

    # Set query and task_id
    base_dict["query"] = query
    base_dict["task_id"] = task_id or str(uuid.uuid4())

    # Reconstruct TaskSpec
    return TaskSpec(
        task_id=base_dict["task_id"],
        task_type=TaskType(base_dict.get("task_type", "workflow")),
        name=base_dict["name"],
        description=base_dict.get("description", ""),
        priority=TaskPriority(base_dict.get("priority", "normal")),
        tags=base_dict.get("tags", []),
        query=base_dict["query"],
        context_files=base_dict.get("context_files", []),
        model=parse_model_config(base_dict),
        budget=parse_budget_config(base_dict),
        workflow=parse_workflow_config(base_dict),
        output=parse_output_config(base_dict),
        audit=parse_audit_config(base_dict),
        hooks=parse_hooks(base_dict),
        created_at=datetime.now(UTC),
        created_by=base_dict.get("created_by", "goose"),
        version=base_dict.get("version", "1.0"),
    )


# Convenience function for OpenClaw MCP integration
def prepare_task_for_openclaw(
    toml_path: Path | None = None,
    template_name: str | None = None,
    query: str | None = None,
    overrides: dict | None = None,
) -> dict:
    """
    Prepare a task for submission to OpenClaw MCP server.

    Returns a dict with:
        - spec: OpenClaw spec dict
        - constraints: OpenClaw constraints dict
        - audit_config: OpenClaw audit config dict
        - task_spec: Full TaskSpec object
    """
    if toml_path:
        spec = load_task_spec(toml_path)
    elif template_name and query:
        spec = create_task_from_template(template_name, query, overrides)
    else:
        raise ValueError("Either toml_path or (template_name + query) must be provided")

    return {
        "spec": spec.to_openclaw_spec(),
        "constraints": spec.to_openclaw_constraints(),
        "audit_config": spec.to_openclaw_audit_config(),
        "task_spec": spec,
    }
