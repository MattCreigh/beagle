"""Tests for Memory Index.

Tests for the tiered memory index layers.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from beagle.memory.memory_index import INDEX_HEADER, MemoryIndex
from beagle.output.schema import Finding

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TestMemoryIndex:
    """Test MemoryIndex class."""

    def test_memory_index_creation(self):
        """MemoryIndex can be created."""
        # Import here to avoid issues with TrackingDatabase singleton
        from beagle.memory.memory_index import MemoryIndex

        with tempfile.TemporaryDirectory() as tmpdir:
            index = MemoryIndex(tmpdir)
            assert index.workspace_root == Path(tmpdir)
            assert index.index_path.exists()

    def test_get_semantic_layer(self):
        """get_semantic_layer returns index content."""
        from beagle.memory.memory_index import MemoryIndex

        with tempfile.TemporaryDirectory() as tmpdir:
            index = MemoryIndex(tmpdir)
            content = index.get_semantic_layer()
            assert "# Beagle Memory Index" in content

    def test_update_from_findings(self):
        """Index can be updated from findings."""
        from beagle.memory.memory_index import MemoryIndex
        from beagle.output.schema import Finding

        with tempfile.TemporaryDirectory() as tmpdir:
            index = MemoryIndex(tmpdir)

            findings = [
                Finding(
                    title="Security issue",
                    description="Test",
                    severity="high",
                    category="security",
                    file_path="auth.py",
                )
            ]

            index.update_from_findings(findings)

            content = index.get_semantic_layer()
            assert "Security issue" in content
            assert "security" in content

    def test_update_from_findings_multiple(self):
        """Index handles multiple findings."""
        from beagle.memory.memory_index import MemoryIndex
        from beagle.output.schema import Finding

        with tempfile.TemporaryDirectory() as tmpdir:
            index = MemoryIndex(tmpdir)

            findings = [
                Finding(title="Bug 1", description="D", severity="low", category="code"),
                Finding(title="Bug 2", description="D", severity="medium", category="code"),
                Finding(title="Bug 3", description="D", severity="high", category="security"),
            ]

            index.update_from_findings(findings)

            content = index.get_semantic_layer()
            assert "Bug 1" in content
            assert "Bug 2" in content
            assert "Bug 3" in content

    def test_update_from_decision(self):
        """Index can be updated from decisions."""
        from beagle.memory.memory_index import MemoryIndex

        with tempfile.TemporaryDirectory() as tmpdir:
            index = MemoryIndex(tmpdir)

            index.update_from_decision("Architecture", "Use PostgreSQL for database")

            content = index.get_semantic_layer()
            assert "Architecture" in content
            assert "PostgreSQL" in content

    def test_update_from_decision_update_existing(self):
        """Existing decision can be updated."""
        from beagle.memory.memory_index import MemoryIndex

        with tempfile.TemporaryDirectory() as tmpdir:
            index = MemoryIndex(tmpdir)

            index.update_from_decision("Data Store", "Use MySQL")
            index.update_from_decision("Data Store", "Use SQLite")

            content = index.get_semantic_layer()
            assert "SQLite" in content
            # MySQL should have been replaced
            assert content.count("Data Store") == 1


class TestMemoryIndexIntegration:
    """Integration tests for memory index."""

    def test_full_workflow(self):
        """Test complete workflow with index."""
        from beagle.memory.memory_index import MemoryIndex
        from beagle.output.schema import Finding

        with tempfile.TemporaryDirectory() as tmpdir:
            index = MemoryIndex(tmpdir)

            # Initial state
            initial = index.get_semantic_layer()
            assert len(initial) > 0

            # Add findings
            findings = [
                Finding(
                    title="Code smell detected",
                    description="Long method",
                    severity="low",
                    category="code_quality",
                    file_path="src/main.py",
                    line_start=42,
                )
            ]
            index.update_from_findings(findings)

            # Add decision
            index.update_from_decision("Refactor Strategy", "Break into smaller methods")

            # Verify final state
            content = index.get_semantic_layer()
            assert "Code smell detected" in content
            assert "Refactor Strategy" in content

    def test_index_persistence(self):
        """Index persists across instances."""
        from beagle.memory.memory_index import MemoryIndex
        from beagle.output.schema import Finding

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create and update first instance
            index1 = MemoryIndex(tmpdir)
            findings = [
                Finding(
                    title="Test Finding",
                    description="D",
                    severity="high",
                    category="test",
                )
            ]
            index1.update_from_findings(findings)

            # Create second instance
            index2 = MemoryIndex(tmpdir)
            content = index2.get_semantic_layer()

            assert "Test Finding" in content


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])


# ── Merged from test_memory_index_inner.py (v1.0.0 consolidation) ────
@pytest.fixture
def temp_workspace(tmp_path):
    # Setup a mock workspace
    (tmp_path / ".beagle").mkdir()
    return tmp_path


def test_index_creation_empty(temp_workspace):
    """Test index creation from empty state."""
    index = MemoryIndex(temp_workspace)
    assert index.index_path.exists()
    content = index.get_semantic_layer()
    assert INDEX_HEADER in content
    assert "## Architecture" in content
    assert "## Recent Findings" in content
    assert "## Active Decisions" in content


def test_update_from_findings(temp_workspace):
    """Test update_from_findings adds pointers correctly."""
    index = MemoryIndex(temp_workspace)

    findings = [
        Finding(title="Bug 1", category="security", severity="high", file_path="auth.py"),
        Finding(title="Perf 1", category="performance", severity="low", file_path="db.py"),
    ]

    index.update_from_findings(findings)
    content = index.get_semantic_layer()

    assert "- security: Bug 1 [auth.py]" in content
    assert "- performance: Perf 1 [db.py]" in content
    # Verify date is present
    assert time.strftime("%Y-%m-%d") in content


def test_update_from_decision(temp_workspace):
    """Test update_from_decision adds/updates entries."""
    index = MemoryIndex(temp_workspace)

    index.update_from_decision("provider", "ollama_cloud")
    content = index.get_semantic_layer()
    assert "- provider: ollama_cloud" in content

    # Update same key
    index.update_from_decision("provider", "local")
    content = index.get_semantic_layer()
    assert "- provider: local" in content
    assert "- provider: ollama_cloud" not in content


def test_budget_enforcement(temp_workspace):
    """Test 2000-token budget enforcement."""
    # Patch _get_token_budget so MemoryIndex gets a tiny budget
    with patch("beagle.memory.memory_index._get_token_budget", return_value=10):
        index = MemoryIndex(temp_workspace)

        # Add many findings
        findings = [Finding(title=f"Bug {i}", category="cat") for i in range(50)]

        index.update_from_findings(findings)
        content = index.get_semantic_layer()

        # Should have pruned some
        assert len(content.splitlines()) < 50


@pytest.mark.asyncio
async def test_search_history(temp_workspace):
    """Test search_history returns results from mock tracking DB."""
    index = MemoryIndex(temp_workspace)

    mock_run = MagicMock()
    mock_run.query = "auth query"
    mock_run.error_summary = None
    mock_run.to_dict.return_value = {"id": "run1", "query": "auth query"}

    with patch("beagle.tracking.database.TrackingDatabase.get_instance") as mock_db_cls:
        mock_db = MagicMock()
        mock_db.get_workflow_runs.return_value = [mock_run]
        mock_db_cls.return_value = mock_db

        results = await index.search_history("auth")
        assert len(results) == 1
        assert results[0]["id"] == "run1"
