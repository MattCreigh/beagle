"""Tests for configurable memory index token budget and pruning strategies.

SP1-1A/1B/1C: Verifies that MemoryIndex supports configurable budgets,
multiple pruning strategies, and section protection.
"""

import os

import pytest

from beagle.memory.memory_index import (
    MemoryIndex,
    PruneStrategy,
)
from beagle.output.schema import (
    Finding,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def index_dir(tmp_path):
    """Create a temp directory for memory index files."""
    return tmp_path


@pytest.fixture(autouse=True)
def clean_env():
    """Remove Beagle env vars before each test."""
    saved = {}
    for key in list(os.environ):
        if key.startswith("BEAGLE_"):
            saved[key] = os.environ.pop(key, None)
    yield
    for key, val in saved.items():
        if val is not None:
            os.environ[key] = val
        else:
            os.environ.pop(key, None)


@pytest.fixture(autouse=True)
def clean_config_cache():
    """Clear the config singleton cache around each test.

    This used to assign ``beagle.config.config._cached_config = None``. The
    cache actually lives in ``beagle.config.loader``; ``config.py`` re-exports
    via ``from .loader import *``, which skips underscore-prefixed names, so
    that assignment created an unused attribute and cleared nothing. Because
    ``get_config()`` bakes env overrides into the cached object,
    ``test_env_var_budget``'s 8000-token budget leaked into whichever test ran
    next, making this class order-dependent (it failed roughly 1 run in 6
    under pytest-randomly).
    """
    from beagle.config.loader import reset_config_cache

    reset_config_cache()
    yield
    reset_config_cache()


# ── Token Budget Tests ────────────────────────────────────────────────────────


class TestTokenBudget:
    """Test configurable token budget."""

    def test_default_budget_is_2000(self, index_dir):
        """Default budget is 2000 tokens."""
        idx = MemoryIndex(index_dir)
        assert idx.token_budget == 2000

    def test_explicit_budget_override(self, index_dir):
        """Explicit token_budget constructor arg takes precedence."""
        idx = MemoryIndex(index_dir, token_budget=5000)
        assert idx.token_budget == 5000

    def test_env_var_budget(self, index_dir):
        """BEAGLE_MEMORY_INDEX_TOKEN_BUDGET env var overrides default."""
        os.environ["BEAGLE_MEMORY_INDEX_TOKEN_BUDGET"] = "8000"
        idx = MemoryIndex(index_dir)
        assert idx.token_budget == 8000

    def test_budget_below_minimum_clamped(self, index_dir):
        """Budget below 500 is clamped to 500."""
        os.environ["BEAGLE_MEMORY_INDEX_TOKEN_BUDGET"] = "100"
        idx = MemoryIndex(index_dir)
        assert idx.token_budget == 500

    def test_budget_minimum_500(self, index_dir):
        """Budget of exactly 500 is accepted."""
        idx = MemoryIndex(index_dir, token_budget=500)
        assert idx.token_budget == 500

    def test_invalid_env_var_budget_falls_back(self, index_dir):
        """Non-numeric env var falls back to default."""
        os.environ["BEAGLE_MEMORY_INDEX_TOKEN_BUDGET"] = "not_a_number"
        idx = MemoryIndex(index_dir)
        assert idx.token_budget == 2000  # Default

    def test_constructor_overrides_env_var(self, index_dir):
        """Constructor arg takes precedence over env var."""
        os.environ["BEAGLE_MEMORY_INDEX_TOKEN_BUDGET"] = "3000"
        idx = MemoryIndex(index_dir, token_budget=6000)
        assert idx.token_budget == 6000


# ── Prune Strategy Tests ───────────────────────────────────────────────────────


class TestPruneStrategy:
    """Test configurable pruning strategy."""

    def test_default_strategy_is_oldest_first(self, index_dir):
        idx = MemoryIndex(index_dir)
        assert idx.prune_strategy == PruneStrategy.OLDEST_FIRST

    def test_explicit_strategy_override(self, index_dir):
        idx = MemoryIndex(index_dir, prune_strategy="relevance_weighted")
        assert idx.prune_strategy == PruneStrategy.RELEVANCE_WEIGHTED

    def test_explicit_enum_strategy(self, index_dir):
        idx = MemoryIndex(index_dir, prune_strategy=PruneStrategy.HYBRID)
        assert idx.prune_strategy == PruneStrategy.HYBRID

    def test_env_var_strategy(self, index_dir):
        os.environ["BEAGLE_MEMORY_INDEX_PRUNE_STRATEGY"] = "hybrid"
        idx = MemoryIndex(index_dir)
        assert idx.prune_strategy == PruneStrategy.HYBRID

    def test_invalid_env_var_strategy_falls_back(self, index_dir):
        os.environ["BEAGLE_MEMORY_INDEX_PRUNE_STRATEGY"] = "random"
        idx = MemoryIndex(index_dir)
        assert idx.prune_strategy == PruneStrategy.OLDEST_FIRST  # Default

    def test_all_valid_strategies(self, index_dir):
        for strategy in ("oldest_first", "relevance_weighted", "hybrid"):
            idx = MemoryIndex(index_dir, prune_strategy=strategy)
            assert idx.prune_strategy == PruneStrategy(strategy)


# ── Pruning Behavior Tests ────────────────────────────────────────────────────


class TestOldestFirstPruning:
    """Test oldest_first pruning strategy."""

    def test_prunes_recent_findings_first(self, index_dir):
        """Over budget: removes oldest (bottom) entries from Recent Findings first."""
        idx = MemoryIndex(index_dir, token_budget=200)  # Very small budget
        # Add many findings to exceed budget
        findings = [
            Finding(category="bug", title=f"Finding {i}", file_path=f"file_{i}.py")
            for i in range(50)
        ]
        idx.update_from_findings(findings)
        content = idx.get_semantic_layer()
        # Should have been pruned to fit within budget
        from beagle.cost_tracker import estimate_tokens_agnostic

        assert estimate_tokens_agnostic(content) <= 200

    def test_does_not_prune_protected_sections(self, index_dir):
        """Core Skills and Key Patterns are not pruned under normal budget."""
        idx = MemoryIndex(index_dir, token_budget=500)
        # Manually write protected sections
        data = {
            "Core Skills": ["- skill: Python mastery [core.py] [2024-01-01]"] * 20,
            "Key Patterns": ["- pattern: Singleton implementation [patterns.py] [2024-01-01]"] * 20,
            "Recent Findings": ["- bug: null pointer [file.py] [2024-01-01]"] * 5,
        }
        idx._write_index(data)
        content = idx.get_semantic_layer()
        assert "Core Skills" in content
        assert "Key Patterns" in content


class TestRelevanceWeightedPruning:
    """Test relevance_weighted pruning strategy."""

    def test_prunes_low_score_entries_first(self, index_dir):
        """Low-scoring entries (no file, no date) are pruned first."""
        idx = MemoryIndex(index_dir, token_budget=300, prune_strategy="relevance_weighted")
        # Entries without file references or dates score low
        findings = [Finding(category="bug", title=f"Finding {i}") for i in range(30)]
        idx.update_from_findings(findings)
        content = idx.get_semantic_layer()
        from beagle.cost_tracker import estimate_tokens_agnostic

        assert estimate_tokens_agnostic(content) <= 300


class TestHybridPruning:
    """Test hybrid pruning strategy."""

    def test_hybrid_falls_back_to_oldest_first(self, index_dir):
        """Hybrid strategy falls back to oldest_first when relevance scores cluster."""
        idx = MemoryIndex(index_dir, token_budget=300, prune_strategy="hybrid")
        findings = [
            Finding(category="bug", title=f"Finding {i}", file_path=f"file_{i}.py")
            for i in range(30)
        ]
        idx.update_from_findings(findings)
        content = idx.get_semantic_layer()
        from beagle.cost_tracker import estimate_tokens_agnostic

        assert estimate_tokens_agnostic(content) <= 300


class TestSectionProtection:
    """Test that protected sections are handled correctly."""

    def test_protected_sections_pruned_at_150_percent(self, index_dir):
        """Protected sections are pruned when budget is exceeded by >150%."""
        # Set a very tiny budget so even with protected sections we need to prune
        idx = MemoryIndex(index_dir, token_budget=100)
        # Manually write lots of content including protected sections
        data = {
            "Core Skills": ["- skill: important skill [core.py] [2024-01-01]"] * 50,
            "Key Patterns": ["- pattern: critical pattern [pat.py] [2024-01-01]"] * 50,
            "Recent Findings": ["- bug: minor bug [file.py] [2024-01-01]"] * 50,
        }
        idx._write_index(data)
        # Content should still exist (pruned heavily but not empty)
        content = idx.get_semantic_layer()
        assert len(content) > 0


# ── Entry Scoring Tests ────────────────────────────────────────────────────────


class TestScoreEntry:
    """Test the _score_entry static method."""

    def test_newer_entries_score_higher(self):
        """Entries at position 0 (newest) score higher than later positions."""
        entry = "- bug: null pointer [file.py] [2024-01-01]"
        score_new = MemoryIndex._score_entry(entry, 0, 10)
        score_old = MemoryIndex._score_entry(entry, 9, 10)
        assert score_new > score_old

    def test_entries_with_file_references_score_higher(self):
        """Entries with [file.py] file references score higher."""
        entry_with_file = "- bug: null pointer [file.py] [2024-01-01]"
        entry_without_file = "- bug: null pointer"
        score_with = MemoryIndex._score_entry(entry_with_file, 0, 10)
        score_without = MemoryIndex._score_entry(entry_without_file, 0, 10)
        assert score_with > score_without

    def test_entries_with_dates_score_higher(self):
        """Entries with date references score higher."""
        entry_with_date = "- bug: null pointer [file.py] [2024-01-01]"
        entry_without_date = "- bug: null pointer [file.py]"
        score_with = MemoryIndex._score_entry(entry_with_date, 0, 10)
        score_without = MemoryIndex._score_entry(entry_without_date, 0, 10)
        assert score_with > score_without


# ── Helper function import ─────────────────────────────────────────────────────
