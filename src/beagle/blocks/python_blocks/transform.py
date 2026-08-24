"""Transform blocks for data manipulation."""

from __future__ import annotations

from typing import Any

from .base import python_block


@python_block(name="extract_sections", description="Extract named sections from markdown")
def extract_sections(_ctx: Any, *, text: str, headers: list[str]) -> dict[str, str]:
    import re

    result: dict[str, str] = {}
    for header in headers:
        pattern = re.compile(rf"(?m)^#+\s*{re.escape(header)}\s*\n(.*?)(?=\n#+\s|\Z)", re.S)
        match = pattern.search(text)
        result[header] = match.group(1).strip() if match else ""
    return result


@python_block(name="merge_dicts", description="Deep-merge two dicts")
def merge_dicts(_ctx: Any, *, base: dict, overlay: dict) -> dict:
    import copy

    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_dicts(_ctx, base=result[key], overlay=value)["output"]
        else:
            result[key] = value
    return result


@python_block(name="format_markdown", description="Format content as markdown")
def format_markdown(_ctx: Any, *, title: str, sections: dict[str, str]) -> str:
    lines = [f"# {title}\n"]
    for header, content in sections.items():
        lines.append(f"\n## {header}\n")
        lines.append(content)
    return "\n".join(lines)
