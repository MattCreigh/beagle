"""Parsing blocks for structured data formats."""

from __future__ import annotations

import json
from typing import Any

from .base import python_block


@python_block(name="parse_yaml", description="Parse a YAML string")
def parse_yaml(_ctx: Any, *, text: str) -> Any:
    import yaml

    return yaml.safe_load(text)


@python_block(name="parse_toml", description="Parse a TOML string")
def parse_toml(_ctx: Any, *, text: str) -> Any:
    try:
        import tomllib

        return tomllib.loads(text)
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

        return tomllib.loads(text)


@python_block(name="parse_json", description="Parse a JSON string")
def parse_json(_ctx: Any, *, text: str) -> Any:
    return json.loads(text)


@python_block(name="parse_ast", description="Parse Python source into AST nodes")
def parse_ast(_ctx: Any, *, source: str) -> dict:
    import ast

    try:
        tree = ast.parse(source)
        return {
            "type": "Module",
            "body_count": len(tree.body),
            "functions": [
                node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
            ],
            "classes": [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)],
        }
    except SyntaxError as e:
        return {"type": "error", "message": str(e)}
