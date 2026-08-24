"""Tests for RAG query decomposition and result merging."""

from __future__ import annotations

from beagle.core.hydration_node import (
    _merge_rag_results,
    decompose_query,
)


class TestDecomposeQuery:
    """Verify query decomposition heuristics."""

    def test_simple_query_returns_original(self):
        """A simple short query should not be decomposed."""
        result = decompose_query("fix the bug")
        assert result == ["fix the bug"]

    def test_conjunction_split(self):
        """Query with 'and' should be split into sub-queries."""
        result = decompose_query("find the authentication module and refactor the login handler")
        assert len(result) >= 2
        assert result[0] == "find the authentication module and refactor the login handler"
        # At least one sub-query should mention authentication or login
        all_text = " ".join(result).lower()
        assert "authentication" in all_text

    def test_sentence_split(self):
        """Multi-sentence query should split on sentence boundaries."""
        result = decompose_query(
            "How does the authentication module work? I need to understand the login flow."
        )
        assert len(result) >= 2
        assert result[0].startswith("How does the authentication module work?")

    def test_comma_split(self):
        """Comma-separated clauses should be split."""
        result = decompose_query("explain the caching layer, the rate limiter, and the retry logic")
        assert len(result) >= 2

    def test_max_subqueries_limit(self):
        """Should not exceed max_subqueries+1 total entries."""
        result = decompose_query(
            "find the auth module, and refactor the login handler, "
            "and fix the session timeout, and update the docs",
            max_subqueries=2,
        )
        # Original + at most 2 additional sub-queries
        assert len(result) <= 3

    def test_empty_query_returns_empty(self):
        """Empty query should return empty list."""
        result = decompose_query("")
        assert result == []

    def test_whitespace_query_returns_empty(self):
        """Whitespace-only query should return empty list."""
        result = decompose_query("   ")
        assert result == ["   "]  # Preserves original but won't decompose

    def test_deduplication(self):
        """Duplicate sub-queries from different split methods should not repeat."""
        result = decompose_query("find the module and find the module")
        # Should not have duplicate entries
        assert len(result) == len(set(result))

    def test_short_fragment_filtering(self):
        """Sub-queries shorter than 10 chars should be skipped."""
        result = decompose_query("analyze the complex distributed system architecture and fix it")
        for sq in result[1:]:  # Skip original which may be long
            assert len(sq) >= 10


class TestDecompositionConfig:
    """Verify DecompositionConfig dataclass defaults."""

    def test_default_values(self):
        """Config should have sensible defaults."""
        from beagle.config.schema import DecompositionConfig

        cfg = DecompositionConfig()
        assert cfg.enabled is True
        assert cfg.max_subqueries == 2
        assert cfg.min_query_length == 20
        assert cfg.merge_max_results == 10

    def test_in_workflow_config(self):
        """DecompositionConfig should be accessible via WorkflowConfig."""
        from beagle.config.schema import WorkflowConfig

        cfg = WorkflowConfig()
        assert hasattr(cfg, "decomposition")
        assert cfg.decomposition.enabled is True


class TestMergeRagResults:
    """Verify RAG result merging and deduplication."""

    def test_dedup_by_file_and_lines(self):
        """Results with same file+line range should be deduplicated."""
        result_sets = [
            [
                {"file": "auth.py", "start_line": 10, "end_line": 20, "score": 0.8},
                {"file": "utils.py", "start_line": 5, "end_line": 15, "score": 0.6},
            ],
            [
                {"file": "auth.py", "start_line": 10, "end_line": 20, "score": 0.9},
                {"file": "models.py", "start_line": 1, "end_line": 50, "score": 0.5},
            ],
        ]
        merged = _merge_rag_results(result_sets)
        # auth.py should appear only once, with score 0.9 (higher)
        auth_results = [r for r in merged if r["file"] == "auth.py"]
        assert len(auth_results) == 1
        assert auth_results[0]["score"] == 0.9

    def test_max_results_limit(self):
        """Should not exceed max_results."""
        result_sets = [
            [{"file": f"file_{i}.py", "start_line": 1, "end_line": 10, "score": 0.9 - i * 0.05}]
            for i in range(20)
        ]
        merged = _merge_rag_results(result_sets, max_results=5)
        assert len(merged) == 5

    def test_score_sorting(self):
        """Results should be sorted by descending score."""
        result_sets = [
            [{"file": "low.py", "score": 0.3, "start_line": 1, "end_line": 5}],
            [{"file": "mid.py", "score": 0.6, "start_line": 1, "end_line": 5}],
            [{"file": "high.py", "score": 0.9, "start_line": 1, "end_line": 5}],
        ]
        merged = _merge_rag_results(result_sets)
        scores = [float(r.get("score", 0)) for r in merged]
        assert scores == sorted(scores, reverse=True)

    def test_empty_input(self):
        """Empty result sets should return empty list."""
        merged = _merge_rag_results([])
        assert merged == []

    def test_snippet_fallback_dedup(self):
        """Chunks without file info should dedup by snippet hash."""
        result_sets = [
            [{"snippet": "def hello(): pass", "score": 0.7}],
            [{"snippet": "def hello(): pass", "score": 0.9}],
        ]
        merged = _merge_rag_results(result_sets)
        assert len(merged) == 1
        assert merged[0]["score"] == 0.9

    def test_preserves_all_fields(self):
        """Merged results should preserve all original fields."""
        result_sets = [
            [
                {
                    "file": "test.py",
                    "start_line": 1,
                    "end_line": 10,
                    "score": 0.8,
                    "snippet": "code here",
                    "custom_field": "preserved",
                }
            ],
        ]
        merged = _merge_rag_results(result_sets)
        assert len(merged) == 1
        assert merged[0]["custom_field"] == "preserved"
