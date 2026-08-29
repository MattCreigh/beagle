"""``get_style_guide`` / ``list_style_guides`` MCP tools — on-demand expansion.

The per-turn Top-of-Mind carries only the load-bearing tier; these tools are
the documented path to the rest.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from typing import Any

from beagle.infrastructure import mcp_utility_server as u


def _call[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def test_list_style_guides_shape() -> None:
    guides = json.loads(_call(u.list_style_guides()))
    assert isinstance(guides, list) and guides
    for g in guides:
        assert {"name", "stem", "tier", "applies_to", "summary"} <= set(g)
    stems = {g["stem"] for g in guides}
    assert "beagle_core_directives" in stems


def test_get_full_guide_returns_xml() -> None:
    out = _call(u.get_style_guide("beagle_core_directives"))
    assert out.lstrip().startswith("<")
    assert "CRITICAL_ROUTING_PROTOCOL" in out


def test_get_section_only() -> None:
    out = _call(u.get_style_guide("beagle_core_directives", "anti_patterns"))
    assert out.lstrip().startswith("<anti_patterns")
    # the full forbidden list, not just the load-bearing summaries
    assert out.count("<forbidden") > 20


def test_lookup_by_meta_name_also_works() -> None:
    out = _call(u.get_style_guide("Beagle Core Directives"))
    assert "CRITICAL_ROUTING_PROTOCOL" in out


def test_invalid_name_rejected() -> None:
    err = json.loads(_call(u.get_style_guide("../etc/passwd")))
    assert err["code"] == "INVALID_INPUT"
    err2 = json.loads(_call(u.get_style_guide("a/b")))
    assert err2["code"] == "INVALID_INPUT"


def test_missing_guide_lists_available() -> None:
    err = json.loads(_call(u.get_style_guide("no_such_guide")))
    assert err["code"] == "NOT_FOUND"
    assert isinstance(err["available"], list) and err["available"]


def test_missing_section_lists_sections() -> None:
    err = json.loads(_call(u.get_style_guide("beagle_core_directives", "nope")))
    assert err["code"] == "NO_SECTION"
    assert "anti_patterns" in err["sections"]
