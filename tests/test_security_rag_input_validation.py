"""Regression tests for RAG ``_validate_search_input``.

Locks down the v13.5.2 security contract:
- Empty / whitespace-only queries → ValueError
- Queries >50_000 chars → silently truncated (not rejected)
- Cypher injection patterns → ValueError
- max_hops clamped to [1, 3]
- top_k clamped to [1, 100]
- Control characters stripped (except \\t, \\n, \\r)

If these tests are relaxed, the security contract has been undone.
"""

from __future__ import annotations

import pytest

from beagle.infrastructure.mcp_rag_server import _validate_search_input


class TestEmptyAndWhitespace:
    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="empty"):
            _validate_search_input("", 1, 5)

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="empty"):
            _validate_search_input("   \t\n  ", 1, 5)


class TestLengthCap:
    def test_under_limit_preserved(self):
        query = "x" * 1000
        out, _, _ = _validate_search_input(query, 1, 5)
        assert out == query

    def test_over_limit_truncated(self):
        """The current contract truncates at 50_000 chars, not rejects.

        This is a deliberate design choice (graceful degradation) — the
        downstream embedder would OOM otherwise. If you change this to
        raise, update MCP_MAX_QUERY_LENGTH in constants.py first.
        """
        query = "x" * 60_000
        out, _, _ = _validate_search_input(query, 1, 5)
        assert len(out) == 50_000

    def test_exactly_at_limit_preserved(self):
        query = "x" * 50_000
        out, _, _ = _validate_search_input(query, 1, 5)
        assert out == query


class TestCypherInjection:
    """The validator checks for keyword + '(' (no space) per v13.5.2.

    This is intentionally strict to limit false positives: a natural-
    language query containing 'MATCH' or 'CREATE' is not rejected, but
    any query that *looks* like a Cypher clause header is.
    """

    @pytest.mark.parametrize(
        "injection",
        [
            "MATCH(n) RETURN n",
            "CREATE(N {evil: 1})",
            "MERGE(a)-[:EVIL]->(b)",
            "DELETE(n)",
            "SET(n.property = 1)",
            "REMOVE(n.property)",
            "DROP(TABLE ASTNode)",
            "LOAD(FROM 'evil.csv')",
            "COPY(ASTNode FROM 'evil')",
            "DETACH DELETE n",  # DETACH is the only keyword checked bare
        ],
    )
    def test_cypher_keyword_rejected(self, injection):
        with pytest.raises(ValueError, match="unsafe pattern"):
            _validate_search_input(injection, 1, 5)


class TestClamping:
    def test_max_hops_clamped_high(self):
        _, max_hops, _ = _validate_search_input("q", 100, 5)
        assert max_hops == 3

    def test_max_hops_clamped_low(self):
        _, max_hops, _ = _validate_search_input("q", 0, 5)
        assert max_hops == 1

    def test_max_hops_clamped_negative(self):
        _, max_hops, _ = _validate_search_input("q", -10, 5)
        assert max_hops == 1

    def test_top_k_clamped_high(self):
        _, _, top_k = _validate_search_input("q", 1, 1000)
        assert top_k == 100

    def test_top_k_clamped_low(self):
        _, _, top_k = _validate_search_input("q", 1, 0)
        assert top_k == 1


class TestControlCharStrip:
    def test_null_byte_stripped(self):
        out, _, _ = _validate_search_input("hello\x00world", 1, 5)
        assert "\x00" not in out
        assert "helloworld" in out

    def test_newline_preserved(self):
        out, _, _ = _validate_search_input("hello\nworld", 1, 5)
        assert "\n" in out

    def test_tab_preserved(self):
        out, _, _ = _validate_search_input("hello\tworld", 1, 5)
        assert "\t" in out

    def test_bell_stripped(self):
        out, _, _ = _validate_search_input("hello\x07world", 1, 5)
        assert "\x07" not in out
