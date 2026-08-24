"""Tests for intelligent memory consolidation."""

from __future__ import annotations

import time

import pytest

from beagle.memory.autodream import AutoDream
from beagle.memory.hierarchical_memory import (
    HierarchicalMemory,
    MemoryEntry,
    MemoryLevel,
)

# ── Helper fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def mem(tmp_path):
    """Create a fresh HierarchicalMemory instance for testing."""
    return HierarchicalMemory(memory_dir=tmp_path / "mem")


def _make_entry(
    content: str,
    level: MemoryLevel = MemoryLevel.EPISODIC,
    metadata: dict | None = None,
    age_hours: float = 0.0,
    access_count: int = 0,
) -> MemoryEntry:
    """Build a MemoryEntry with configurable age and access count."""
    entry = MemoryEntry(
        id=f"test-{content[:8]}",
        level=level,
        content=content,
        timestamp=time.time() - age_hours * 3600,
        metadata=metadata or {},
    )
    entry.access_count = access_count
    return entry


# ── HierarchicalMemory._score_relevance tests ─────────────────────────────


class TestScoreRelevance:
    """Verify multi-signal relevance scoring in HierarchicalMemory."""

    def test_jaccard_overlap_boosts_score(self, mem):
        """Entries with more term overlap should score higher."""
        entry = _make_entry("authentication login session token")
        score_high = mem._score_relevance(entry, "authentication session")
        score_low = mem._score_relevance(entry, "unrelated xyzzy")
        assert score_high > score_low

    def test_bigram_overlap_detected(self, mem):
        """Bigram overlap (phrase matching) should boost score."""
        entry = _make_entry("fix the authentication module in auth.py")
        score = mem._score_relevance(entry, "authentication module")
        assert score > 0

    def test_recency_exponential_decay(self, mem):
        """Recent entries should score higher than old ones."""
        recent = _make_entry("test query match", age_hours=0.5)
        old = _make_entry("test query match", age_hours=48)
        score_recent = mem._score_relevance(recent, "test query")
        score_old = mem._score_relevance(old, "test query")
        assert score_recent > score_old

    def test_access_frequency_bonus(self, mem):
        """Frequently accessed entries should get a small bonus."""
        frequent = _make_entry("test query entry", access_count=10)
        rare = _make_entry("test query entry", access_count=0)
        score_freq = mem._score_relevance(frequent, "test query")
        score_rare = mem._score_relevance(rare, "test query")
        assert score_freq > score_rare

    def test_working_memory_level_bias(self, mem):
        """Working memory should get a small boost over episodic."""
        working = _make_entry("data", level=MemoryLevel.WORKING)
        episodic = _make_entry("data", level=MemoryLevel.EPISODIC)
        score_w = mem._score_relevance(working, "data")
        score_e = mem._score_relevance(episodic, "data")
        assert score_w > score_e

    def test_metadata_match_boosts(self, mem):
        """Metadata containing query terms should boost score."""
        with_meta = _make_entry("some content", metadata={"phase": "research"})
        without_meta = _make_entry("some content", metadata={})
        score_with = mem._score_relevance(with_meta, "research")
        score_without = mem._score_relevance(without_meta, "research")
        assert score_with > score_without

    def test_empty_query_returns_base_score(self, mem):
        """Empty query should still return a valid score (no crash)."""
        entry = _make_entry("test content")
        score = mem._score_relevance(entry, "")
        assert score >= 0

    async def test_traverse_respects_budget(self, mem):
        """traverse() should respect token budget limits."""
        # Store several entries
        for i in range(10):
            await mem.store(
                f"Entry {i} with some content about topic {i}",
                MemoryLevel.EPISODIC,
            )

        # Budget of 50 tokens should not return all entries
        results = await mem.traverse("topic", budget_tokens=50)
        total_chars = sum(len(e.content) for e in results)
        # 50 tokens * 4 chars/token = 200 chars budget
        assert total_chars <= 300  # some slack for rounding


# ── AutoDream._score_entry_relevance tests ─────────────────────────────────


class TestScoreEntryRelevance:
    """Verify relevance scoring for AutoDream prune decisions."""

    def test_recent_entries_score_higher(self):
        """Entries near the end of a list (recent) score higher."""
        score_old = AutoDream._score_entry_relevance("old entry", 0, 10)
        score_new = AutoDream._score_entry_relevance("new entry", 9, 10)
        assert score_new > score_old

    def test_longer_entries_score_higher(self):
        """Information-dense entries should score higher."""
        short = AutoDream._score_entry_relevance("ok", 5, 10)
        long = AutoDream._score_entry_relevance(
            "detailed error report about authentication module auth.py line 42",
            5,
            10,
        )
        assert long > short

    def test_file_reference_boosts_score(self):
        """Entries with file path references get a relevance boost."""
        no_file = AutoDream._score_entry_relevance("general note about system", 5, 10)
        with_file = AutoDream._score_entry_relevance("fix in auth.py]", 5, 10)
        assert with_file > no_file

    def test_error_keywords_boost(self):
        """Entries with error/bug/fix keywords should score higher."""
        neutral = AutoDream._score_entry_relevance("updated the config", 5, 10)
        error = AutoDream._score_entry_relevance("error in auth module", 5, 10)
        assert error > neutral

    def test_status_words_penalize(self):
        """Entries with success/completed/ok words should score lower."""
        status = AutoDream._score_entry_relevance("completed successfully ok done", 5, 10)
        neutral = AutoDream._score_entry_relevance("generic content here", 5, 10)
        assert neutral > status

    def test_score_bounded(self):
        """Score should be bounded between 0 and 10."""
        for pos in range(10):
            for length in [1, 5, 20]:
                score = AutoDream._score_entry_relevance("test entry " * length, pos, 10)
                assert 0 <= score <= 10


# ── MemoryConsolidationConfig tests ───────────────────────────────────────


class TestMemoryConsolidationConfig:
    """Verify MemoryConsolidationConfig dataclass defaults."""

    def test_default_values(self):
        from beagle.config.schema import (
            MemoryConsolidationConfig,
        )

        cfg = MemoryConsolidationConfig()
        assert cfg.merge_enabled is True
        assert cfg.merge_min_group_size == 2
        assert cfg.merge_max_summary_parts == 5
        assert cfg.prune_relevance_threshold == 2.0
        assert cfg.prune_staleness_days == 30
        assert cfg.prune_dedup_enabled is True

    def test_in_workflow_config(self):
        from beagle.config.schema import WorkflowConfig

        cfg = WorkflowConfig()
        assert hasattr(cfg, "memory_consolidation")
        assert cfg.memory_consolidation.merge_enabled is True
