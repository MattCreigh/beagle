"""MCP Exposure — register blocks as MCP tools/resources/workflows.

Beagle v13.8.2:
  - Python blocks → MCP tools (with input_schema from signatures)
  - XML blocks   → MCP resources
  - TOML agents  → MCP workflows
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Any, get_type_hints

from .registry import BlockRegistry

try:
    import tomllib
except ModuleNotFoundError:
    import toml as tomllib  # type: ignore[no-redef]

logger = logging.getLogger("Beagle.blocks.mcp")

# ── Schema generation ────────────────────────────────────────────────

_PYTHON_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _build_input_schema(func: Any) -> dict[str, Any]:
    """Derive a JSON Schema from a Python block's raw function signature.

    Uses ``inspect.signature`` on the underlying ``__raw_func__`` so that
    the wrapper added by ``@python_block`` does not obscure the real
    parameter names and types.
    """
    raw = getattr(func, "__raw_func__", func)
    try:
        sig = inspect.signature(raw)
    except (ValueError, TypeError):
        return {"type": "object", "properties": {}, "required": []}

    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        if name == "_ctx":
            continue

        annotation = param.annotation
        if annotation is inspect.Parameter.empty:
            prop: dict[str, Any] = {"type": "string"}
        elif annotation in _PYTHON_TYPE_MAP:
            prop = {"type": _PYTHON_TYPE_MAP[annotation]}
        else:
            # Handle generic types like list[str], dict[str, Any]
            origin = getattr(annotation, "__origin__", None)
            if origin is list:
                prop = {"type": "array", "items": {"type": "string"}}
            elif origin is dict:
                prop = {"type": "object"}
            else:
                prop = {"type": "string"}

        if param.default is inspect.Parameter.empty:
            required.append(name)
            properties[name] = prop
        else:
            prop["default"] = (
                param.default if not isinstance(param.default, type) else str(param.default)
            )
            properties[name] = prop

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _build_output_schema(func: Any) -> dict[str, Any] | None:
    """Derive a JSON Schema for a Python block's return value.

    Mirror of :func:`_build_input_schema` for the output direction: the
    same argument that justifies validating inputs (a wrong-shaped value
    produces cryptic downstream failures rather than a clean contract
    error) applies to the value a block returns, which is ``ctx.set()``
    and consumed by later blocks.

    Returns ``None`` when no trustworthy contract can be derived —
    unannotated return, ``Any``, or a non-JSON-shaped annotation — in
    which case the output passes through unchecked. Conservative by
    design: never invent a schema that could produce false rejections.
    """
    raw = getattr(func, "__raw_func__", func)
    try:
        sig = inspect.signature(raw)
    except (ValueError, TypeError):
        return None

    annotation = sig.return_annotation
    if isinstance(annotation, str):
        # PEP 563 stringized annotation (`from __future__ import
        # annotations`) — resolve it, or no contract is derivable at all.
        try:
            annotation = get_type_hints(raw).get("return", annotation)
        except (NameError, TypeError, AttributeError):
            return None
    if annotation is inspect.Signature.empty or annotation is Any or isinstance(annotation, str):
        return None
    if annotation is None or annotation is type(None):
        return {"type": "null"}
    if annotation in _PYTHON_TYPE_MAP:
        return {"type": _PYTHON_TYPE_MAP[annotation]}
    origin = getattr(annotation, "__origin__", None)
    if origin is list:
        return {"type": "array"}
    if origin is dict:
        return {"type": "object"}
    return None


class BlockMCPExposure:
    """Adapter that exposes block registry entries to MCP servers."""

    def __init__(self, registry: BlockRegistry | None = None) -> None:
        self.registry = registry or BlockRegistry.instance()

    def list_tools(self) -> list[dict[str, Any]]:
        """Return Python blocks as MCP tool definitions with input schemas."""
        tools: list[dict[str, Any]] = []
        for name in self.registry.list_python():
            block = self.registry.get_python(name)
            doc = getattr(block, "__doc__", None)
            description = (doc.strip() if isinstance(doc, str) and doc.strip() else "") or (
                f"Python block '{name}'"
            )
            schema = _build_input_schema(block)
            tools.append(
                {
                    "name": name,
                    "type": "tool",
                    "description": description,
                    "input_schema": schema,
                }
            )
        return tools

    def list_resources(self) -> list[dict[str, Any]]:
        """Return XML blocks as MCP resource definitions."""
        resources: list[dict[str, Any]] = []
        for name in self.registry.list_xml():
            path = self.registry.get_xml(name)
            resources.append(
                {
                    "name": name,
                    "type": "resource",
                    "uri": f"beagle://xml_blocks/{name}",
                    "mimeType": "application/xml",
                    "description": str(path),
                }
            )
        return resources

    def list_workflows(self, agents_dir: Path | None = None) -> list[dict[str, Any]]:
        """Return TOML agent recipes as MCP workflow definitions."""
        workflows: list[dict[str, Any]] = []
        if agents_dir is None:
            from ..config._config_path import find_blocks_agents_dir

            agents_dir = find_blocks_agents_dir()
        if not agents_dir.exists():
            return workflows
        for toml_file in sorted(agents_dir.glob("*.toml")):
            try:
                raw = toml_file.read_bytes()
                data = tomllib.loads(raw.decode("utf-8"))
                workflows.append(
                    {
                        "name": data.get("name", toml_file.stem),
                        "type": "workflow",
                        "description": data.get("description", ""),
                        "uri": f"beagle://workflows/{toml_file.stem}",
                    }
                )
            except (AttributeError, KeyError, TypeError) as exc:  # catch: NARROWED
                logger.warning(f"Skipping {toml_file}: {exc}")
        return workflows

    def invoke_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Invoke a Python block and return its output as string."""
        import jsonschema

        block = self.registry.get_python(name)
        schema = _build_input_schema(block)
        try:
            jsonschema.validate(instance=arguments, schema=schema)
        except jsonschema.ValidationError as e:
            raise ValueError(f"Invalid arguments for tool '{name}': {e.message}") from e
        result = block(arguments)
        return str(result)

    def read_resource(self, name: str) -> str:
        """Read an XML block as a text resource."""
        path = self.registry.get_xml(name)
        return Path(path).read_text(encoding="utf-8")
