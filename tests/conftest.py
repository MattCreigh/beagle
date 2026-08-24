"""Pytest configuration for Beagle tests.

Ensures proper module discovery and fixtures.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# Add project root to sys.path for all tests
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Ensure source takes precedence over installed package
# This allows tests to import from the development tree


def pytest_configure(config):
    """Register custom markers so pytest doesn't emit warnings."""
    config.addinivalue_line("markers", "requires_valid_api_key: test requires a valid live API key")


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """v1.0.9 (audit M5): make the suite hermetic against ambient colour env.

    The 2026-08-15 audit found the ambient ``FORCE_COLOR`` variable produced
    both a phantom test failure (test_preflight_log_output) and a phantom
    mypy-gate pass (C2). Pin colour-related env vars to a deterministic
    colour-free state for every test so no test result depends on the
    developer's terminal environment.
    """
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("NO_COLOR", "1")
    yield


@pytest.fixture
def tmp_workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def mock_ollama():
    mock = MagicMock()
    mock.chat.return_value = {
        "message": {"content": "mocked response"},
        "eval_count": 100,
        "prompt_eval_count": 50,
    }
    return mock


# ──────────────────────────────────────────────────────────────────────────────
# Shared fixtures added per the v13.21.12 test infrastructure audit (P2 #11).
# All are opt-in (not autouse) so existing tests are unaffected.
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_mcp_server():
    """Mock the mcp.server.fastmcp.FastMCP interface.

    Returns a MagicMock that mimics the FastMCP class used in
    infrastructure/mcp_utility_server.py and infrastructure/mcp_rag_server.py.
    Use this when a test instantiates an MCP server but does not need the
    real stdio transport.
    """
    mock = MagicMock(name="mock_mcp_server")
    mock.tool.return_value = lambda func: func  # @mcp.tool() decorator = identity
    mock.resource.return_value = lambda func: func  # @mcp.resource() = identity
    mock.prompt.return_value = lambda func: func  # @mcp.prompt() = identity
    mock.run = MagicMock()
    mock.run.return_value = None
    return mock


@pytest.fixture
def test_config():
    """A default Beagle config dict suitable for `get_config()` patching.

    Returns a fresh dict per-test so mutation does not leak. Tests that
    call `beagle.config.config.get_config()` should patch
    that function to return this fixture's value.
    """
    return {
        "model": {
            "primary": "minimax-m3:cloud",
            "fallback_chain": [
                "minimax-m3:cloud",
                "glm-5.1:cloud",
                "deepseek-v4-pro:cloud",
            ],
            "timeout_seconds": 30,
        },
        "workspace_root": "/tmp/beagle_test_workspace",
        "feature_flags": {
            "turboquant": True,
            "rag_sync": False,
            "mcp_openclaw": False,
        },
        "security": {
            "scrub_secrets_in_logs": True,
            "ast_validator": True,
        },
        "tracking": {
            "db_path": ":memory:",
        },
    }


@pytest.fixture
def mock_llm_response():
    """Factory for canned LLM response dicts.

    Returns a function that takes (content="...", eval_count=100, prompt_eval_count=50)
    and returns a dict shaped like the Ollama chat() response. Use this when
    a test needs a specific LLM response shape but is not exercising the
    ollama bridge itself.
    """

    def factory(
        content: str = "mocked LLM response",
        eval_count: int = 100,
        prompt_eval_count: int = 50,
    ) -> dict[str, Any]:
        return {
            "message": {"content": content},
            "eval_count": eval_count,
            "prompt_eval_count": prompt_eval_count,
        }

    return factory


@pytest.fixture
def isolated_db(tmp_path):
    """A fresh SQLite database file in a tmp directory, with cleanup.

    Returns the path to the database file. The directory is automatically
    removed by pytest's tmp_path teardown. Use this for tests that touch
    tracking/database.py or any other sqlite-touching module.
    """
    db_path = tmp_path / "test_tracking.db"
    # Touch the file so SQLite can open it (some code paths require the
    # file to exist; others create it on first connect).
    db_path.touch()
    return db_path


@pytest.fixture
def beagle_event_loop_policy():
    """Provide a default asyncio event-loop policy for tests.

    Use this when a test creates its own asyncio.run / loop and needs
    consistent behaviour with pytest-asyncio's auto mode. The default
    policy is the platform default; tests may override per-call.

    Note: name is prefixed `beagle_` to avoid clashing with pytest-asyncio's
    reserved `event_loop_policy` fixture (which would emit a deprecation
    warning if we override it).
    """
    return asyncio.DefaultEventLoopPolicy()


__all__ = ["PROJECT_ROOT", "test_config"]
