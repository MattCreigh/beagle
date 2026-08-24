"""Tests for semantic prompt cache."""

from __future__ import annotations

import pytest

from beagle.context.semantic_prompt_cache import (
    SemanticPromptCache,
    _cosine_similarity,
)


class TestCosineSimilarity:
    """Test cosine similarity function."""

    def test_identical_vectors(self):
        """Identical vectors have similarity 1.0."""
        v = [1.0, 0.0, 0.0]
        assert _cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        """Orthogonal vectors have similarity 0.0."""
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert _cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        """Opposite vectors have similarity -1.0."""
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert _cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_zero_vector(self):
        """Zero vector returns 0.0 similarity."""
        assert _cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0

    def test_different_lengths(self):
        """Mismatched vector lengths return 0.0."""
        assert _cosine_similarity([1.0], [1.0, 2.0]) == 0.0


class TestSemanticPromptCache:
    """Test semantic prompt cache operations."""

    def test_store_and_lookup_exact(self):
        """Exact text match returns cached response."""
        cache = SemanticPromptCache(max_entries=10)
        cache.store([1.0, 0.0], "hello world", "response 1")
        result = cache.lookup([0.0, 1.0], "hello world")
        assert result == "response 1"

    def test_lookup_miss(self):
        """Unmatched prompt returns None."""
        cache = SemanticPromptCache(max_entries=10, similarity_threshold=0.99)
        cache.store([1.0, 0.0], "hello", "response")
        result = cache.lookup([0.0, 1.0], "different prompt")
        assert result is None

    def test_semantic_hit(self):
        """High-similarity embedding returns cached response."""
        cache = SemanticPromptCache(max_entries=10, similarity_threshold=0.95)
        emb = [1.0, 0.0, 0.0]
        cache.store(emb, "prompt", "response")
        # Very similar embedding
        result = cache.lookup([0.99, 0.14, 0.0], "slightly different")
        assert result is not None

    def test_lru_eviction(self):
        """LRU eviction when cache is full."""
        cache = SemanticPromptCache(max_entries=2)
        cache.store([1.0, 0.0], "a", "1")
        cache.store([0.0, 1.0], "b", "2")
        cache.store([1.0, 1.0], "c", "3")  # Should evict oldest
        assert cache.stats["entries"] <= 2

    def test_clear_cache(self):
        """Clear removes all entries."""
        cache = SemanticPromptCache()
        cache.store([1.0], "a", "1")
        count = cache.clear()
        assert count >= 1
        assert cache.stats["entries"] == 0

    def test_hit_rate_tracking(self):
        """Hit rate is tracked correctly."""
        cache = SemanticPromptCache()
        cache.store([1.0, 0.0], "test", "response")
        cache.lookup([1.0, 0.0], "test")  # hit
        cache.lookup([0.0, 1.0], "miss")  # miss
        assert cache.hit_rate > 0.0
