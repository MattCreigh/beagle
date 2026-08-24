"""LangChain Tool Node Adapter for Beagle workflows.

Phase 2 of the LangChain Ecosystem Compatibility Plan.
Executes any LangChain BaseTool as an Beagle workflow node,
returning results in the same state-update format as execute_goose_node().

Enables 200+ LangChain integrations (Slack, SQL, GitHub, etc.)
to be dropped into Beagle workflow YAML phases alongside existing
Goose subprocess nodes.

YAML usage:
  - name: "fetch_context"
    executor: "langchain_tool"
    tool_name: "file_system"
    tool_method: "read_file"
    input_mapping:
      file_path: "{{state.hydration.manifest.source_file}}"
    output_key: "file_content"
    timeout: 15
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from ..events import NodeFailed, get_event_bus
from .config import get_tools_config
from .tool_registry import get_tool_registry

logger = logging.getLogger("Beagle.bridges.tool_node")


def _infer_phase_from_tool(tool_name: str) -> str:
    """Infer workflow phase from a tool name for event metadata."""
    name_lower = tool_name.lower()
    if any(kw in name_lower for kw in ("file", "read", "write", "git", "code")):
        return "execution"
    if any(kw in name_lower for kw in ("search", "web", "request", "fetch")):
        return "execution"
    if any(kw in name_lower for kw in ("sql", "db", "database")):
        return "execution"
    if any(kw in name_lower for kw in ("slack", "email", "notify")):
        return "synthesis"
    return "execution"


def _resolve_input_mapping(
    input_mapping: dict[str, str],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Resolve Jinja-style state references in input mapping.

    Converts {{state.field.subfield}} patterns into actual values
    from the workflow state dict.

    Args:
        input_mapping: Dict of tool_arg_name -> state reference template.
        state: Current Beagle workflow state.

    Returns:
        Dict of resolved tool input arguments.

    """
    resolved: dict[str, Any] = {}

    for arg_name, template in input_mapping.items():
        if not isinstance(template, str):
            resolved[arg_name] = template
            continue

        # Match {{state.field}} or {{state.field.subfield}}
        match = re.match(r"^\{\{state\.([a-zA-Z0-9_.]+)\}\}$", template.strip())
        if match:
            path = match.group(1).split(".")
            value: Any = state
            for key in path:
                if isinstance(value, dict):
                    value = value.get(key, "")
                else:
                    value = ""
                    break
            resolved[arg_name] = value
        elif "{{" in template:
            # Fallback: simple string substitution for all {{state.X}} refs
            result = template
            for m in re.finditer(r"\{\{state\.([a-zA-Z0-9_.]+)\}\}", template):
                path = m.group(1).split(".")
                val: Any = state
                for key in path:
                    if isinstance(val, dict):
                        val = val.get(key, "")
                    else:
                        val = ""
                        break
                result = result.replace(m.group(0), str(val))
            resolved[arg_name] = result
        else:
            resolved[arg_name] = template

    return resolved


async def execute_langchain_tool_node(
    state: dict[str, Any],
    phase_spec: dict[str, Any],
    output_key: str,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Execute a LangChain BaseTool as an Beagle workflow node.

    Called when workflow_loader detects executor="langchain_tool"
    in the phase YAML. Falls through to goose executor for any
    phase that doesn't specify an executor (backward compatible).

    Args:
        state: Current Beagle workflow state dict.
        phase_spec: Phase specification from the YAML workflow.
        output_key: Key to write result into in state.
        timeout: Timeout in seconds (default from config.toml).

    Returns:
        State update dict following Beagle conventions:
        {output_key: str(result), completed_nodes: [tool_name]}

    """
    config = get_tools_config()
    tool_name = phase_spec.get("tool_name", phase_spec.get("name", "unknown"))
    tool_method = phase_spec.get("tool_method")
    input_mapping = phase_spec.get("input_mapping", {})

    if timeout is None:
        timeout = config.default_timeout_seconds

    # Resolve the tool from the registry (lazy import, cached)
    registry = get_tool_registry()
    tool = registry.get_tool(tool_name)

    if tool is None:
        err_msg = f"{tool_name}: Tool not available (not registered, disabled, or import failed)"
        logger.error(err_msg)

        if config.fallback_on_error:
            return {
                "errors": [err_msg],
                "completed_nodes": [f"{tool_name}(tool_unavailable)"],
                "tool_failure_flag": {
                    "tool_name": tool_name,
                    "error": err_msg,
                    "category": "tool_unavailable",
                    "escalate_to_goose": True,
                },
            }
        raise RuntimeError(err_msg)

    # Resolve input arguments from state
    tool_input = _resolve_input_mapping(input_mapping, state)

    logger.info(f"[{tool_name}] Executing LangChain tool with input: {list(tool_input.keys())}")

    try:
        # Execute tool with timeout
        if tool_method and hasattr(tool, tool_method):
            # Call a specific method on the tool
            method = getattr(tool, tool_method)
            if asyncio.iscoroutinefunction(method):
                result = await asyncio.wait_for(method(**tool_input), timeout=timeout)
            else:
                result = method(**tool_input)
        else:
            # Use tool's standard invoke/ainvoke interface
            if hasattr(tool, "ainvoke"):
                result = await asyncio.wait_for(
                    tool.ainvoke(tool_input),
                    timeout=timeout,
                )
            elif hasattr(tool, "invoke"):
                result = tool.invoke(tool_input)
            elif callable(tool):
                if asyncio.iscoroutinefunction(tool):
                    result = await asyncio.wait_for(tool(**tool_input), timeout=timeout)
                else:
                    result = tool(**tool_input)
            else:
                raise RuntimeError(f"Tool '{tool_name}' has no invoke/ainvoke/callable interface")

        # Serialize result to string (Beagle convention: all state values are strings)
        if isinstance(result, str):
            result_str = result
        elif isinstance(result, dict):
            result_str = json.dumps(result, default=str, indent=2)
        elif hasattr(result, "content"):
            # LangChain ToolMessage or similar
            result_str = str(result.content)
        else:
            result_str = str(result)

        # v13.7.0: VIGIL verify-before-commit — validate output
        try:
            from ..security.vigil import validate_tool_output

            is_safe, sanitized = validate_tool_output(tool_name, result_str)
            if not is_safe:
                logger.warning(f"[{tool_name}] VIGIL flagged output — using sanitized version")
                result_str = sanitized
        except ImportError as exc:
            # A security control that cannot load must be loud. Logged at error,
            # not warning: tool output reaches the model WITHOUT VIGIL screening.
            logger.error(
                "[%s] Cannot import VIGIL output validation (%s); this tool's output is "
                "being returned WITHOUT screening.",
                tool_name,
                exc,
            )

        logger.info(f"[{tool_name}] Tool completed: {len(result_str)} chars output")

        # Build state update in Beagle convention
        updates: dict[str, Any] = {
            "completed_nodes": [tool_name],
            "metadata": {**state.get("metadata", {}), output_key: result_str},
        }

        from beagle.utils.field_mapping import map_output_to_state

        target_key = map_output_to_state(output_key, skill_name=tool_name)
        if target_key:
            updates[target_key] = result_str

        return updates

    except TimeoutError:
        err_msg = f"{tool_name}: Tool timed out after {timeout}s"
        logger.error(err_msg)
        get_event_bus().publish(
            NodeFailed(
                workflow_id=state.get("workflow_id", ""),
                node_name=tool_name,
                error=err_msg,
                attempt=1,
                error_category="timeout",
                duration_seconds=timeout,
                node_phase=_infer_phase_from_tool(tool_name),
            )
        )
        if config.fallback_on_error:
            return {
                "errors": [err_msg],
                "completed_nodes": [f"{tool_name}(timeout)"],
                "tool_failure_flag": {
                    "tool_name": tool_name,
                    "error": err_msg,
                    "category": "timeout",
                    "escalate_to_goose": True,
                },
            }
        raise

    except Exception as exc:  # broad catch intentional
        err_msg = f"{tool_name}: {type(exc).__name__} - {exc}"
        logger.error(f"[{tool_name}] Tool execution failed: {exc}", exc_info=True)
        get_event_bus().publish(
            NodeFailed(
                workflow_id=state.get("workflow_id", ""),
                node_name=tool_name,
                error=err_msg,
                attempt=1,
                error_category="tool_error",
                node_phase=_infer_phase_from_tool(tool_name),
            )
        )
        if config.fallback_on_error:
            return {
                "errors": [err_msg],
                "completed_nodes": [f"{tool_name}(failed)"],
                "tool_failure_flag": {
                    "tool_name": tool_name,
                    "error": err_msg,
                    "category": "tool_error",
                    "escalate_to_goose": True,
                },
            }
        raise
