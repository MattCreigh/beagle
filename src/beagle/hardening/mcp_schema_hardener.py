"""Post-registration MCP schema hardener — enforces additionalProperties:false.

v13.12.9: Closes F10.3 — FastMCP's ``@mcp.tool()`` uses Pydantic's
``model_json_schema()`` which does not emit ``additionalProperties: false``.
This module walks all registered tools and adds the guard recursively to
every object-type schema node.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("Beagle.hardening.mcp_schema")


def _is_object_schema(schema: dict[str, Any]) -> bool:
    """Return True if dict represents an object-type JSON schema."""
    t = schema.get("type")
    if t == "object":
        return True
    if isinstance(t, list) and "object" in t:
        return True
    # Schemas that carry object keywords but omit explicit type
    return "properties" in schema or "required" in schema or "patternProperties" in schema


def _harden_schema_node(schema: object, tool_name: str) -> None:
    """Recursively add ``additionalProperties: false`` to object nodes."""
    if not isinstance(schema, dict):
        return
    if _is_object_schema(schema):
        if "additionalProperties" not in schema:
            schema["additionalProperties"] = False
            logger.debug("Hardened object schema for %s", tool_name)
        # Recurse into properties
        for prop_schema in schema.get("properties", {}).values():
            if isinstance(prop_schema, dict):
                _harden_schema_node(prop_schema, tool_name)
        # patternProperties / dependentSchemas / propertyNames may carry schemas
        for key in ("patternProperties", "dependentSchemas", "properties"):
            if key in schema and isinstance(schema[key], dict):
                for sub in schema[key].values():
                    if isinstance(sub, dict):
                        _harden_schema_node(sub, tool_name)
        if "additionalProperties" in schema and isinstance(schema["additionalProperties"], dict):
            _harden_schema_node(schema["additionalProperties"], tool_name)
        if "unevaluatedProperties" in schema and isinstance(schema["unevaluatedProperties"], dict):
            _harden_schema_node(schema["unevaluatedProperties"], tool_name)
    # Recurse into array items / prefixItems
    if "items" in schema and isinstance(schema["items"], dict):
        _harden_schema_node(schema["items"], tool_name)
    if "prefixItems" in schema and isinstance(schema["prefixItems"], list):
        for sub in schema["prefixItems"]:
            if isinstance(sub, dict):
                _harden_schema_node(sub, tool_name)
    # Recurse into composition keywords
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema and isinstance(schema[key], list):
            for sub in schema[key]:
                if isinstance(sub, dict):
                    _harden_schema_node(sub, tool_name)
    # Recurse into definitions / $defs
    for key in ("$defs", "definitions", "defs"):
        if key in schema and isinstance(schema[key], dict):
            for sub in schema[key].values():
                if isinstance(sub, dict):
                    _harden_schema_node(sub, tool_name)


def harden_mcp_tool_schemas(mcp_server: Any) -> int:
    """Add ``additionalProperties: false`` to every registered tool's input schema.

    Must be called after all ``@mcp.tool()`` registrations and before ``mcp.run()``.

    Args:
        mcp_server: A ``FastMCP`` server instance.

    Returns:
        Number of tools hardened.

    """
    try:
        tools = mcp_server._tool_manager._tools
    except AttributeError as exc:
        logger.warning("Cannot access tool manager: %s", exc)
        return 0

    count = 0
    for name, tool in tools.items():
        params = getattr(tool, "parameters", None)
        if isinstance(params, dict):
            _harden_schema_node(params, name)
            count += 1
        else:
            logger.warning("Tool %r has no parameters dict", name)

    logger.info("Hardened %d MCP tool schemas with additionalProperties:false", count)
    return count
