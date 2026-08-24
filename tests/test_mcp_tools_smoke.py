"""Smoke test for the code_search MCP tool (WP-1 B1)."""

from __future__ import annotations

import json

import pytest

from beagle import infrastructure


def _project_root() -> infrastructure.tools._impl._PROJECT_ROOT:
    """Return the repository root for the test run."""
    from pathlib import Path

    return Path(__file__).resolve().parent.parent


@pytest.mark.asyncio
async def test_code_search_finds_function_defs(monkeypatch):
    """code_search returns a success payload for a safe regex."""
    from beagle.infrastructure.tools._impl import code_search

    monkeypatch.setattr(
        "beagle.infrastructure.tools._impl._PROJECT_ROOT",
        _project_root(),
    )
    result = await code_search(pattern="def ", path="src/beagle/config")
    data = json.loads(result)
    assert data["status"] == "ok"
    assert len(data["matches"]) > 0


@pytest.mark.asyncio
async def test_code_search_rejects_bad_pattern(monkeypatch):
    """code_search rejects an invalid regex pattern.

    v1.2.0 (RG-5, BGL-005/BGL-006): the pattern is validated by the engine
    that executes it (ripgrep's Rust regex crate) via the subprocess return
    code, not by a Python `re` pre-check. An invalid pattern makes ripgrep
    exit 2; the tool must report status='error' with the ripgrep stderr text
    in the message.
    """
    from beagle.infrastructure.tools._impl import code_search

    result = await code_search(pattern="(", path="src/beagle/config")
    data = json.loads(result)
    assert data["status"] == "error"
    assert "ripgrep exited 2" in data["message"]
