"""BEAGLE MIND RELAY P1 — beagle_context_directive MCP tool + ToM resource."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from typing import Any

from beagle.infrastructure import mcp_utility_server as u


def _call[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def _directive(**kw: Any) -> dict[str, Any]:
    return json.loads(_call(u.beagle_context_directive(**kw)))


def test_session_start_shape_and_cap() -> None:
    d = _directive(scope_dir="/home/server/Projects/beagle", phase="session_start")
    assert d["status"] == "ok"
    assert d["domain"] == "python"  # cwd_globs: */beagle
    assert d["cap_ok"] is True
    assert {"global_root", "project_guides"} <= set(d["layers"])
    assert d["directive"].lstrip().startswith("<")  # XML
    assert "beagle_top_of_mind" in d["directive"] or "style_guide" in d["directive"]


def test_compact_slice_small() -> None:
    for ph in ("fold", "compact"):
        d = _directive(scope_dir="/home/server/Projects/beagle", phase=ph)
        assert d["status"] == "ok"
        assert d["bytes"] <= 2048, f"{ph} slice = {d['bytes']} B (COMPACT_MAX_BYTES)"


def test_invalid_phase_fails_closed() -> None:
    d = _directive(phase="bogus")
    assert d["status"] == "error" and d["code"] == "INVALID_PHASE"


def test_bad_scope_fails_closed() -> None:
    d = _directive(scope_dir="/nonexistent_dir_xyz")
    assert d["status"] == "error" and d["code"] == "BAD_SCOPE"
    d2 = _directive(phase="file")
    assert d2["status"] == "error" and d2["code"] == "MISSING_FILE"


def test_file_phase_matches_inject_for_file() -> None:
    from beagle.style_guides.injector import ContextInjector
    from beagle.style_guides.loader import StyleGuideLoader

    p = "/home/server/Projects/routeUpper/src/rup_pkg/cli/main.py"
    d = _directive(phase="file", file_path=p)
    assert d["status"] == "ok"
    expect = ContextInjector(StyleGuideLoader()).inject_for_file(p)
    assert d["directive"] == expect  # T-P1-04 byte-for-byte


def test_universal_scope_domain_none() -> None:
    d = _directive(scope_dir="/home/server/Projects/routeUpper", phase="session_start")
    assert d["status"] == "ok"
    assert d["domain"] is None  # no domain glob matches routeUpper


def test_tom_resource_registered() -> None:
    rm = u.mcp._resource_manager
    uris = sorted(str(r.uri) for r in rm._resources.values())
    assert "beagle://top-of-mind" in uris
