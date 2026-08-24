"""Shared field mapping utilities for Beagle v12.3.

Centralizes the mapping between agent output keys, skill aliases, and
LangGraph state fields. Previously duplicated in nodes.py, graph.py,
and autonomous_orchestrator.py.

Generated from: MASTER_IMPROVEMENT_PLAN P2.3
"""

from __future__ import annotations

# ── Output Key → State Field Mapping ───────────────────────────────────────────
# Maps the `output_key` parameter in workflow phases to the corresponding
# BeagleState TypedDict field name.

OUTPUT_FIELD_MAPPING: dict[str, str] = {
    # Planning phase outputs
    "research_plan": "research_plan",
    "planning": "research_plan",
    "plan": "research_plan",
    "audit_plan": "research_plan",
    # Execution phase outputs
    "raw_execution_context": "raw_execution_context",
    "search_results": "raw_execution_context",
    "execution": "raw_execution_context",
    "code_changes": "raw_execution_context",
    # Verification phase outputs
    "verified_facts": "verified_facts",
    "verification": "verified_facts",
    "validation": "verified_facts",
    "ground_truth": "verified_facts",
    # Synthesis phase outputs
    "final_report": "final_report",
    "synthesis": "final_report",
    "report": "final_report",
    "summary": "final_report",
    # Implementation phase outputs
    "implementation": "raw_execution_context",
    "improvements": "raw_execution_context",
    # Security phase outputs
    "security_audit": "verified_facts",
    "vulnerability_report": "verified_facts",
    # Architecture phase outputs
    "architecture_review": "research_plan",
    "design": "research_plan",
    # Test generation outputs
    "test_results": "verified_facts",
    "tests": "verified_facts",
    # Documentation outputs
    "documentation": "final_report",
    "docs": "final_report",
}

# ── Skill Name → Output Key Mapping ────────────────────────────────────────────
# Maps agent recipe/skill names to their canonical output key.
# Used when the output_key isn't explicitly specified in the workflow YAML.

AGENT_ALIAS_MAPPING: dict[str, str] = {
    # Planning agents
    "research-planner": "research_plan",
    "deep-planner": "research_plan",
    "agent-orchestrator": "research_plan",
    # Execution agents
    "search-executor": "raw_execution_context",
    "sota-dev": "raw_execution_context",
    "python-backend": "raw_execution_context",
    "api-designer": "raw_execution_context",
    "react-frontend-dev": "raw_execution_context",
    "rust-cpp-systems": "raw_execution_context",
    "new-ai-dev": "raw_execution_context",
    # Verification agents
    "fact-checker": "verified_facts",
    "ground-truth-validator": "verified_facts",
    "security-auditor": "verified_facts",
    "architecture-auditor": "verified_facts",
    "e2e-tester": "verified_facts",
    # Synthesis agents
    "synthesis-writer": "final_report",
    "documentation-writer": "final_report",
    "consulting-strategist": "final_report",
    # DevOps agents
    "devops-pipeline-architect": "raw_execution_context",
    "infrastructure": "raw_execution_context",
    # Utility agents
    "context-compressor": "research_plan",
    "curator": "research_plan",
    "self-improver": "final_report",
    "self-benchmark": "verified_facts",
    # Specialized agents
    "db-migration-specialist": "raw_execution_context",
    "patent-analyst": "final_report",
    "financial-valuator": "final_report",
    "prompt-engineer": "research_plan",
    "code-profiler": "verified_facts",
    "latency-hunter": "verified_facts",
    "performance-profiler": "verified_facts",
    "memory-forensics": "verified_facts",
    "code-profiler-2": "verified_facts",
    "resource-optimizer": "raw_execution_context",
}


def get_output_field(output_key: str) -> str | None:
    """Map an output_key to its corresponding state field name.

    Args:
        output_key: The output key from a workflow phase or agent

    Returns:
        The BeagleState field name, or None if no mapping exists

    """
    return OUTPUT_FIELD_MAPPING.get(output_key)


def get_agent_output_key(skill_name: str) -> str | None:
    """Map a skill/recipe name to its canonical output key.

    Args:
        skill_name: The agent recipe or skill name

    Returns:
        The output key, or None if no mapping exists

    """
    return AGENT_ALIAS_MAPPING.get(skill_name)


# ── Legacy Field → BeagleState Field Mapping ────────────────────────────────────
# Maps deprecated/legacy YAML field names to the canonical BeagleState fields.
# When hydration_node.py encounters a legacy key in a YAML recipe, it should
# translate it through this mapping before writing to state.

LEGACY_FIELD_MAPPING: dict[str, str] = {
    # Task 4 standardization (v12.3)
    "steps": "step_count",
    "permissions": "permission_level",
    "id": "workflow_id",
    "state": "status",
    "context": "metadata",
    "run_mode": "mode",
    "wf_id": "workflow_id",
    "extra_metadata": "metadata",
    "meta": "metadata",
}


def normalize_state_field(field_name: str) -> str:
    """Translate a legacy YAML field name to its canonical BeagleState field.

    If the field is not a known legacy alias, it is returned unchanged.

    Args:
        field_name: The field name as found in a YAML recipe or agent output.

    Returns:
        The canonical BeagleState field name.

    """
    return LEGACY_FIELD_MAPPING.get(field_name, field_name)


def map_output_to_state(output_key: str, skill_name: str | None = None) -> str | None:
    """Resolve an output_key to a state field, with skill name fallback.

    Tries output_key mapping first, then falls back to skill name alias.

    Args:
        output_key: The output key from a workflow phase
        skill_name: Optional skill name for alias-based resolution

    Returns:
        The BeagleState field name, or None if no mapping exists

    """
    # Try direct output key mapping first
    state_field = OUTPUT_FIELD_MAPPING.get(output_key)
    if state_field:
        return state_field

    # Fall back to skill name alias
    if skill_name:
        alias_output = AGENT_ALIAS_MAPPING.get(skill_name)
        if alias_output:
            return OUTPUT_FIELD_MAPPING.get(alias_output)

    return None


def register_output_mapping(output_key: str, state_field: str) -> None:
    """Register a new output key → state field mapping at runtime.

    For use by plugins or dynamically loaded workflows that define
    custom output keys.

    Args:
        output_key: The output key to register
        state_field: The BeagleState field it maps to

    """
    OUTPUT_FIELD_MAPPING[output_key] = state_field


def register_agent_alias(skill_name: str, output_key: str) -> None:
    """Register a new skill name → output key mapping at runtime.

    Args:
        skill_name: The skill/recipe name to register
        output_key: The output key it maps to

    """
    AGENT_ALIAS_MAPPING[skill_name] = output_key
