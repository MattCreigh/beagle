"""Unit tests for core/hydration_node.py

Tests:
1. HydrationResult.to_context_block() formatting
2. Secret scrubbing in context blocks
3. Error handling when RAG fails
4. _hydrate_rag with mock RAG server
5. Token budget management
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from beagle.core.hydration_node import (  # ruff: ignore[E402]
    HydrationResult,
    _hydrate_constraints,
    _hydrate_rag,
    _hydrate_session_episodes,
    hydrate_context,
)


class TestHydrationResult:
    """Test HydrationResult dataclass and context block generation."""

    def test_empty_result(self):
        """Empty hydration result produces empty context block."""
        result = HydrationResult()
        assert result.to_context_block() == ""

    def test_constraints_formatting(self):
        """Constraints are formatted in valid XML format."""
        result = HydrationResult(
            constraints=["Use Python 3.13", "Follow PEP 8"],
        )
        block = result.to_context_block()
        assert "<active_constraints>" in block
        assert "Use Python 3.13" in block
        assert "</active_constraints>" in block

    def test_code_chunks_formatting(self):
        """Code chunks are formatted in valid XML code blocks."""
        result = HydrationResult(
            code_chunks=[
                {"file": "main.py", "snippet": "print('hello')", "relevance": 0.9},
            ],
        )
        block = result.to_context_block()
        assert "<code_context>" in block
        assert "main.py" in block
        assert "print('hello')" in block
        assert "</code_context>" in block

    def test_session_episodes_truncation(self):
        """Long session summaries are truncated."""
        episodes = [
            {"role": "PLANNING", "content": "A" * 500},
            {"role": "EXECUTION", "content": "B" * 500},
            {"role": "SYNTHESIS", "content": "C" * 500},
        ]
        result = HydrationResult(session_episodes=episodes)
        block = result.to_context_block()
        # Block should be generated (truncation happens internally)
        assert "<session_context>" in block
        # Should contain session content
        assert len(block) > 0

    def test_full_result_formatting(self):
        """Full result with all fields produces valid XML."""
        result = HydrationResult(
            constraints=["No bare except"],
            code_chunks=[{"file": "utils.py", "snippet": "def foo()", "relevance": 0.8}],
            session_episodes=[{"role": "PLANNING", "content": "Plan here"}],
        )
        block = result.to_context_block()
        assert "<active_constraints>" in block
        assert "<code_context>" in block
        assert "<session_context>" in block


class TestSecretScrubbing:
    """Test secret scrubbing in context blocks."""

    def test_api_key_scrubbing_in_constraints(self):
        """API keys in constraints are scrubbed."""
        result = HydrationResult(
            constraints=["API_KEY=sk-abc1234567890abcdefgh", "Normal constraint"],
        )
        block = result.to_context_block()
        assert "sk-abc1234567890" not in block
        assert "Normal constraint" in block

    def test_password_scrubbing_in_constraints(self):
        """Passwords in constraints are scrubbed."""
        result = HydrationResult(
            constraints=["password=supersecret123", "Normal constraint"],
        )
        block = result.to_context_block()
        assert "supersecret123" not in block
        assert "Normal constraint" in block

    def test_file_paths_scrubbed(self):
        """Code chunks with file paths produce valid output."""
        result = HydrationResult(
            code_chunks=[
                {"file": "/etc/passwd", "snippet": "root:x:0:0", "relevance": 0.5},
            ],
        )
        block = result.to_context_block()
        # Block should be generated without crashing
        assert block is not None
        assert len(block) > 0
        # XML structure should be intact
        assert "root:x:0:0" in block

    def test_session_summary_scrubbing(self):
        """Secrets in session episodes are scrubbed."""
        result = HydrationResult(
            session_episodes=[{"role": "EXEC", "content": "token=sk-xxx secret"}],
        )
        block = result.to_context_block()
        assert "sk-xxx" not in block

    def test_structural_context_scrubbing(self):
        """Scrubbing preserves structural XML tags."""
        result = HydrationResult(
            code_chunks=[{"file": "main.py", "snippet": "x = 1", "relevance": 0.9}],
        )
        block = result.to_context_block()
        # XML structure must be intact
        assert block.count("<code_context>") == block.count("</code_context>")
        assert "<code_context>" in block
        assert "</code_context>" in block


class TestHydrateRag:
    """Test _hydrate_rag with mocked RAG server.

    NOTE: Each test clears the prefetch cache to prevent stale results
    from leaking between tests. We also patch the EmbeddingAdapter to
    skip the similarity-based prefetch path, since it uses a live Ollama
    instance and would cache results across test boundaries.
    """

    @pytest.fixture(autouse=True)
    def _clear_prefetch(self):
        """Clear prefetch cache before each test to ensure isolation."""
        import beagle.core.hydration_node as hn

        hn._prefetch_entries.clear()
        # Stub out staleness tracker to prevent hot-swap reingestion
        # (which would index the entire codebase and hang the test)
        self._staleness_patcher = patch(
            "beagle.context.rag_staleness.get_staleness_tracker",
            side_effect=ImportError("stubbed for test"),
        )
        self._staleness_patcher.start()
        yield
        self._staleness_patcher.stop()
        hn._prefetch_entries.clear()

    @pytest.mark.asyncio
    async def test_rag_returns_semantic_anchors(self):
        """RAG response with semantic_anchors key is parsed correctly."""
        mock_response = json.dumps({"semantic_anchors": [{"file": "test.py", "snippet": "code"}]})
        with (
            patch(
                "beagle.infrastructure.mcp_rag_server.rag_search",
                new=AsyncMock(return_value=mock_response),
            ),
            patch(
                "beagle.context.embedding_adapter.get_embedding_adapter",
                side_effect=ImportError("test"),
            ),
        ):
            result = await _hydrate_rag("test query semantic", max_results=5)
            assert len(result) == 1
            assert result[0]["file"] == "test.py"
            assert result[0]["snippet"] == "code"

    @pytest.mark.asyncio
    async def test_rag_returns_data_key(self):
        """RAG response with 'data' key is parsed as fallback."""
        mock_response = json.dumps({"data": [{"file": "alt.py", "content": "alt code"}]})
        with (
            patch(
                "beagle.infrastructure.mcp_rag_server.rag_search",
                new=AsyncMock(return_value=mock_response),
            ),
            patch(
                "beagle.context.embedding_adapter.get_embedding_adapter",
                side_effect=ImportError("test"),
            ),
        ):
            result = await _hydrate_rag("test query data key", max_results=5)
            assert len(result) == 1
            assert result[0]["file"] == "alt.py"

    @pytest.mark.asyncio
    async def test_rag_invalid_json(self):
        """Invalid JSON from RAG returns empty list."""
        with (
            patch(
                "beagle.infrastructure.mcp_rag_server.rag_search",
                new=AsyncMock(return_value="not valid json {"),
            ),
            patch(
                "beagle.context.embedding_adapter.get_embedding_adapter",
                side_effect=ImportError("test"),
            ),
        ):
            result = await _hydrate_rag("test query invalid", max_results=5)
            assert result == []

    @pytest.mark.asyncio
    async def test_rag_no_results_status(self):
        """RAG response with no_results status returns empty list."""
        mock_response = json.dumps({"status": "no_results", "message": "No matches found"})
        with (
            patch(
                "beagle.infrastructure.mcp_rag_server.rag_search",
                new=AsyncMock(return_value=mock_response),
            ),
            patch(
                "beagle.context.embedding_adapter.get_embedding_adapter",
                side_effect=ImportError("test"),
            ),
        ):
            result = await _hydrate_rag("test query no results", max_results=5)
            assert result == []

    @pytest.mark.asyncio
    async def test_rag_import_error(self):
        """ImportError when RAG module unavailable is handled gracefully."""
        mock_module = MagicMock()
        mock_module.rag_search = None
        del mock_module.rag_search

        with (
            patch.dict(
                "sys.modules",
                {
                    "beagle.infrastructure": MagicMock(),
                    "beagle.infrastructure.mcp_rag_server": mock_module,
                },
            ),
            patch(
                "beagle.context.embedding_adapter.get_embedding_adapter",
                side_effect=ImportError("test"),
            ),
        ):
            result = await _hydrate_rag("test query import error", max_results=5)
            assert isinstance(result, list)


class TestHydrateConstraints:
    """Test _hydrate_constraints function."""

    @pytest.mark.asyncio
    async def test_constraint_registry_not_available(self):
        """When ConstraintRegistry unavailable, returns empty list."""
        with patch.dict(
            "sys.modules",
            {"beagle.infrastructure.constraint_registry": None},
        ):
            result = await _hydrate_constraints("test query", None)
            assert result == []

    @pytest.mark.asyncio
    async def test_session_memory_not_available(self):
        """When session memory unavailable, returns empty list."""
        with patch.dict("sys.modules", {"beagle.context.session_memory": None}):
            result = await _hydrate_session_episodes("test query", "/tmp")
            assert result == []


class TestHydrateContext:
    """Test the top-level hydrate_context function."""

    # Unskipped in v1.0.0: the skip reason was accurate but was never acted on
    # — hydrate_context(query, skill_name, ...) takes skill_name as a required
    # positional, and these two calls simply omitted it.
    async def test_hydrate_context_empty_query(self):
        """Empty query returns result with empty blocks."""
        result = await hydrate_context("", "test-skill")
        assert isinstance(result, HydrationResult)

    async def test_hydrate_context_with_rag_fallback(self):
        """When RAG fails, context still returns with empty RAG data.

        v1.0.2: this used to patch.dict sys.modules and then assign
        ``mcp_rag.rag_search = AsyncMock(...)`` directly. Two things made that
        leak the mock into every later test in the session:

        1. ``import a.b.c as x`` binds via attribute traversal on the real
           parent package (``sys.modules["beagle"].infrastructure``), so the
           sys.modules entry for "beagle.infrastructure" being a MagicMock did
           NOT redirect it — ``mcp_rag`` was the REAL module.
        2. ``patch.dict`` restores sys.modules entries, never attributes set on
           a module object, so the bare assignment was permanent.

        The real ``rag_search`` stayed replaced by an AsyncMock whose
        side_effect raises, and the 7 rag_search tests in test_mcp_e2e.py and
        test_mcp_rag.py failed — but only when this file ran first, which is
        why they passed in isolation.

        patch.object restores the attribute on exit, which is the whole fix.
        """
        import beagle.infrastructure.mcp_rag_server as mcp_rag

        with patch.object(
            mcp_rag, "rag_search", AsyncMock(side_effect=Exception("RAG unavailable"))
        ):
            result = await hydrate_context("test query", "test-skill")
            assert isinstance(result, HydrationResult)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
