"""Tests for the query_fold MCP tool.

Validates input validation, sanitisation, fold resolution,
error handling, rate limiting, and JSON serialisation.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from beagle.infrastructure.tools._impl import query_fold
from beagle.utils.mcp_rate_limit import RateLimiter, RateLimitExceeded

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_limiter():
    """Reset the rate limiter between tests so one test's 120 calls
    don't starve the next."""
    import beagle.infrastructure.tools._impl as impl

    impl._limiter = RateLimiter(max_calls=120, window_seconds=60.0)
    yield
    impl._limiter = RateLimiter(max_calls=120, window_seconds=60.0)


@pytest.fixture()
def mock_store():
    """Patch get_compressed_store at its source to return a MagicMock.

    Because query_fold lazy-imports get_compressed_store inside
    the function body, we must patch at the source module.
    """
    with patch("beagle.context.compressed_store.get_compressed_store") as mock_get:
        store = MagicMock()
        mock_get.return_value = store
        yield store


async def _call(query: str, fold_id: str = "", top_k: int = 3) -> dict:
    """Helper to call query_fold and parse the JSON result."""
    raw = await query_fold(query=query, fold_id=fold_id, top_k=top_k)
    return json.loads(raw)


# ── Test: query returns top-K results ─────────────────────────────────────────


class TestQueryReturnsTopK:
    async def test_returns_top_k(self, mock_store):
        """Query a fold and get top-K results with correct keys."""
        mock_store.list_folds.return_value = ["abc123456789"]
        mock_store.query_fold.return_value = [
            {"index": 0, "similarity": 0.92, "text_preview": "rate limiter..."},
            {"index": 3, "similarity": 0.85, "text_preview": "sliding window..."},
            {"index": 7, "similarity": 0.71, "text_preview": "asyncio lock..."},
        ]
        result = await _call("rate limiter implementation", top_k=3)
        assert result["status"] == "ok"
        assert len(result["results"]) == 3
        assert result["results"][0]["similarity"] == 0.92
        mock_store.query_fold.assert_called_once_with(
            "rate limiter implementation", "abc123456789", 3
        )


# ── Test: query specific fold_id ──────────────────────────────────────────────


class TestQuerySpecificFoldId:
    async def test_uses_specified_fold(self, mock_store):
        """When fold_id is given, query that specific fold."""
        mock_store.query_fold.return_value = [
            {"index": 0, "similarity": 0.80, "text_preview": "test data"},
        ]
        result = await _call("test query", fold_id="deadbeef0123", top_k=1)
        assert result["status"] == "ok"
        mock_store.query_fold.assert_called_once_with("test query", "deadbeef0123", 1)
        # list_folds should NOT be called when fold_id is explicit
        mock_store.list_folds.assert_not_called()


# ── Test: empty fold_id uses most recent ───────────────────────────────────────


class TestQueryEmptyFoldIdUsesMostRecent:
    async def test_uses_most_recent(self, mock_store):
        """Empty fold_id should resolve to the most recent fold."""
        mock_store.list_folds.return_value = ["fold000000003", "fold000000002", "fold000000001"]
        mock_store.query_fold.return_value = [
            {"index": 1, "similarity": 0.75, "text_preview": "recent data"},
        ]
        result = await _call("recent query", fold_id="", top_k=1)
        assert result["status"] == "ok"
        # Should have queried the first (most recent) fold
        mock_store.query_fold.assert_called_once_with("recent query", "fold000000003", 1)


# ── Test: invalid fold_id rejected ────────────────────────────────────────────


class TestInvalidFoldIdRejected:
    async def test_path_traversal(self, mock_store):
        """fold_id with path traversal should be rejected."""
        result = await _call("test", fold_id="../etc/passwd", top_k=3)
        assert result["status"] == "error"
        assert "fold_id" in result["message"]

    async def test_invalid_hex_chars(self, mock_store):
        """fold_id with non-hex chars should be rejected."""
        result = await _call("test", fold_id="ghijklmnopqr", top_k=3)
        assert result["status"] == "error"
        assert "fold_id" in result["message"]

    async def test_wrong_length_short(self, mock_store):
        """fold_id that's too short should be rejected."""
        result = await _call("test", fold_id="abc123", top_k=3)
        assert result["status"] == "error"

    async def test_wrong_length_long(self, mock_store):
        """fold_id that's too long should be rejected."""
        result = await _call("test", fold_id="abc123456789def456", top_k=3)
        assert result["status"] == "error"

    async def test_uppercase_hex(self, mock_store):
        """fold_id with uppercase hex should be rejected."""
        result = await _call("test", fold_id="ABC123456789", top_k=3)
        assert result["status"] == "error"


# ── Test: top_k out of bounds ─────────────────────────────────────────────────


class TestTopKOutOfBounds:
    async def test_zero(self, mock_store):
        result = await _call("test", top_k=0)
        assert result["status"] == "error"
        assert "top_k" in result["message"]

    async def test_eleven(self, mock_store):
        result = await _call("test", top_k=11)
        assert result["status"] == "error"

    async def test_negative(self, mock_store):
        result = await _call("test", top_k=-1)
        assert result["status"] == "error"


# ── Test: control chars stripped from query ────────────────────────────────────


class TestControlCharsStripped:
    async def test_null_byte_stripped(self, mock_store):
        """Null bytes should be stripped from query."""
        mock_store.list_folds.return_value = ["abc123456789"]
        mock_store.query_fold.return_value = [
            {"index": 0, "similarity": 0.5, "text_preview": "result"},
        ]
        await _call("hello\x00world", fold_id="", top_k=1)
        # The query sent to store should have the null byte stripped
        call_args = mock_store.query_fold.call_args[0]
        assert "\x00" not in call_args[0]
        assert "hello" in call_args[0]
        assert "world" in call_args[0]

    async def test_escape_stripped(self, mock_store):
        """ESC (0x1b) should be stripped from query."""
        mock_store.list_folds.return_value = ["abc123456789"]
        mock_store.query_fold.return_value = []
        await _call("test\x1bquery", fold_id="", top_k=1)
        call_args = mock_store.query_fold.call_args[0]
        assert "\x1b" not in call_args[0]

    async def test_newline_preserved(self, mock_store):
        """Newlines should be preserved in queries."""
        mock_store.list_folds.return_value = ["abc123456789"]
        mock_store.query_fold.return_value = []
        await _call("line1\nline2", fold_id="", top_k=1)
        call_args = mock_store.query_fold.call_args[0]
        assert "\n" in call_args[0]


# ── Test: rate limit enforced ─────────────────────────────────────────────────


class TestRateLimitEnforced:
    async def test_rate_limit_on_121st_call(self, mock_store):
        """121st rapid call should be rate-limited."""
        mock_store.list_folds.return_value = ["abc123456789"]
        mock_store.query_fold.return_value = []

        # Make 120 successful calls
        for _ in range(120):
            await query_fold(query="test q", fold_id="abc123456789", top_k=1)

        # 121st should raise
        with pytest.raises(RateLimitExceeded):
            await query_fold(query="test q", fold_id="abc123456789", top_k=1)


# ── Test: no folds exist ──────────────────────────────────────────────────────


class TestNoFoldsExist:
    async def test_empty_store(self, mock_store):
        """When no folds exist, return no_folds_available status."""
        mock_store.list_folds.return_value = []
        result = await _call("anything", fold_id="", top_k=3)
        assert result["status"] == "no_folds_available"
        assert "No compressed folds" in result["message"]


# ── Test: "no folds" distinguishable from "no matches" ────────────────────────


class TestNoFoldsVsNoMatches:
    async def test_different_statuses(self, mock_store):
        """no_folds_available and no_matches must return different status values."""
        # No folds exist
        mock_store.list_folds.return_value = []
        result_no_folds = await _call("anything", fold_id="", top_k=3)

        # Fold exists but no matches
        mock_store.list_folds.return_value = ["abc123456789"]
        mock_store.query_fold.return_value = []
        result_no_matches = await _call("anything", fold_id="", top_k=3)

        assert result_no_folds["status"] != result_no_matches["status"]
        assert result_no_folds["status"] == "no_folds_available"
        assert result_no_matches["status"] == "no_matches"


# ── Test: fold not found lists available ───────────────────────────────────────


class TestFoldNotFoundListsAvailable:
    async def test_missing_fold_returns_available(self, mock_store):
        """Querying a non-existent fold_id should list available folds."""
        from beagle.context.compressed_store import FoldNotFoundError

        mock_store.query_fold.side_effect = FoldNotFoundError("fold not found")
        mock_store.list_folds.return_value = ["aaa111111111", "bbb222222222", "ccc333333333"]
        result = await _call("test", fold_id="abc123456789", top_k=3)
        assert result["status"] == "fold_not_found"
        assert result["requested_fold_id"] == "abc123456789"
        assert "available_fold_ids" in result
        assert len(result["available_fold_ids"]) == 3


# ── Test: numpy values serialise ──────────────────────────────────────────────


class TestNumpySerialisation:
    async def test_numpy_floats_serialise(self, mock_store):
        """numpy float types in results must serialise to JSON cleanly."""
        import numpy as np

        mock_store.query_fold.return_value = [
            {
                "index": 0,
                "similarity": np.float32(0.92),
                "text_preview": "test data",
            },
        ]
        # If json.loads succeeds, serialisation worked
        result = await _call("test", fold_id="abc123456789", top_k=1)
        assert result["status"] == "ok"
        assert isinstance(result["results"][0]["similarity"], float)
