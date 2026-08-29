"""Pytest configuration for Beagle tests.

Ensures proper module discovery and fixtures.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# Add the source tree to sys.path for all tests. The package lives at
# src/beagle, not at the repository root, so tests import the development
# tree here rather than whatever wheel happens to be installed.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers so pytest doesn't emit warnings."""
    config.addinivalue_line("markers", "requires_valid_api_key: test requires a valid live API key")


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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


@pytest.fixture(autouse=True)
def _no_real_rag_reingest() -> Iterator[None]:
    """Refuse automatic reingest against the real, on-disk sidecar file.

    D-04 (release audit 2026-08-29): pointed at the source tree, the suite
    was OOM-killed by the kernel — twice, kernel-confirmed — because some
    test reached the production ``RAGStalenessTracker`` singleton (the real
    on-disk sidecar file, genuinely stale relative to this checkout)
    without isolating its own tracker or mocking ``hotswap_ingest``, and
    that spawned a real, unbounded CAST/Kuzu rebuild of the whole codebase
    on a background thread mid-suite.

    This only gates the DEFAULT tracker (see
    ``rag_staleness._is_default_sidecar``). A test that isolates its own
    tracker with an explicit ``staleness_file`` — the pattern every
    existing reingest test already follows — is unaffected; this closes
    the gap for the tests that do not.

    Function-scoped, not session-scoped, and deliberately so:
    ``tests/test_rag_reingest_guard.py`` calls ``importlib.reload(rs)`` twice
    (to test ``_MIN_REINGEST_INTERVAL``'s env-parsed default), which
    re-executes the module body and silently resets this flag to its
    default — a session-scoped fixture set once at the start would be
    defeated for every test after that reload. Re-asserting before each
    test survives it.
    """
    from beagle.context.rag_staleness import set_default_reingest_disabled

    set_default_reingest_disabled(True)
    yield
    set_default_reingest_disabled(False)


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def mock_ollama() -> MagicMock:
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
def mock_mcp_server() -> MagicMock:
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
def test_config() -> dict[str, Any]:
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
def mock_llm_response() -> Callable[..., dict[str, Any]]:
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
def isolated_db(tmp_path: Path) -> Path:
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
def beagle_event_loop_policy() -> asyncio.DefaultEventLoopPolicy:
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
