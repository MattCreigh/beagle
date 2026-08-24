"""State manipulation blocks for the execution context."""

from __future__ import annotations

from typing import Any

from .base import python_block


@python_block(name="set_field", description="Set a field in the context outputs")
def set_field(ctx: Any, *, key: str, value: Any) -> Any:
    ctx.set(key, value)
    return value


@python_block(name="append_list", description="Append a value to a list in context")
def append_list(ctx: Any, *, key: str, value: Any) -> list[Any]:
    existing = ctx.get(key, [])
    if not isinstance(existing, list):
        existing = []
    existing.append(value)
    ctx.set(key, existing)
    return existing


@python_block(name="merge_metadata", description="Deep-merge metadata into context")
def merge_metadata(ctx: Any, *, metadata: dict[str, Any]) -> dict[str, Any]:
    existing: dict[str, Any] = ctx.metadata
    for k, v in metadata.items():
        if k in existing and isinstance(existing[k], dict) and isinstance(v, dict):
            existing[k].update(v)
        else:
            existing[k] = v
    return existing
