"""Tests for hierarchical_memory.py"""

from pathlib import Path

import pytest

from beagle.memory.hierarchical_memory import (
    HierarchicalMemory,
    MemoryEntry,
    MemoryLevel,
)


class TestHierarchicalMemory:
    """Test hierarchical memory system."""

    @pytest.mark.asyncio
    async def test_store_and_retrieve(self):
        """Test basic store and retrieve."""
        mem = HierarchicalMemory()

        entry_id = await mem.store("Test content", MemoryLevel.WORKING, metadata={"test": True})

        entry = await mem.retrieve(entry_id)

        assert entry is not None
        assert entry.content == "Test content"
        assert entry.metadata["test"] is True

    @pytest.mark.asyncio
    async def test_traverse_with_query(self):
        """Test query-based traversal."""
        mem = HierarchicalMemory()

        # Store multiple entries
        await mem.store("Python code analysis", MemoryLevel.EPISODIC)
        await mem.store("JavaScript web development", MemoryLevel.EPISODIC)
        await mem.store("Database SQL queries", MemoryLevel.EPISODIC)

        # Query for Python
        results = await mem.traverse("Python code", budget_tokens=500)

        # Should find Python entry
        assert len(results) >= 1
        python_found = any("Python" in r.content for r in results)
        assert python_found

    def test_extract_operator(self):
        """Test extraction operator (a)."""
        mem = HierarchicalMemory()

        raw = "This is sentence one. This is sentence two. This is sentence three."
        result = mem.extract(raw, query="one two")

        assert len(result.atoms) <= 3
        assert result.extraction_time_ms >= 0

    def test_coarsen_operator(self):
        """Test coarsening operator (C)."""
        mem = HierarchicalMemory()

        atoms = [f"Atom {i}" for i in range(10)]
        result = mem.coarsen(atoms, num_groups=3)

        # 10 atoms / 3 groups = ceil(3.33) = 4 groups
        assert len(result.groups) == 4
        assert len(result.representatives) == 4

    @pytest.mark.asyncio
    async def test_consolidate(self):
        """Test working to episodic consolidation."""
        mem = HierarchicalMemory()

        # Store in working memory
        for i in range(5):
            await mem.store(f"Working memory item {i}", MemoryLevel.WORKING)

        # Consolidate
        atoms = await mem.consolidate(max_atoms=3)

        assert atoms <= 3
        assert len(mem._working) == 0  # Working should be cleared

    @pytest.mark.asyncio
    async def test_stats(self):
        """Test memory statistics."""
        # Use a fresh memory directory to avoid leftover data
        import shutil
        import tempfile

        tmpdir = tempfile.mkdtemp()
        try:
            mem = HierarchicalMemory(memory_dir=Path(tmpdir))

            await mem.store("Entry 1", MemoryLevel.WORKING)
            await mem.store("Entry 2", MemoryLevel.EPISODIC)

            stats = await mem.get_stats()

            assert stats["working_count"] == 1
            assert stats["episodic_count"] == 1
            assert stats["total_entries"] == 2
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestMemoryEntry:
    """Test memory entry functionality."""

    def test_touch_updates_access(self):
        """Test that touch updates access statistics."""
        entry = MemoryEntry(
            id="test",
            level=MemoryLevel.WORKING,
            content="Test",
        )

        assert entry.access_count == 0

        entry.touch()
        assert entry.access_count == 1
        assert entry.last_access > 0


class TestMemoryLevel:
    """Test memory level enum."""

    def test_memory_levels_exist(self):
        """Test all memory levels are defined."""
        assert MemoryLevel.WORKING.value == "working"
        assert MemoryLevel.EPISODIC.value == "episodic"
        assert MemoryLevel.LONG_TERM.value == "long_term"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
