"""Tests for Beagle autoDream (Phase 8.4)."""

import time
from unittest.mock import MagicMock, patch

import pytest

from beagle.memory.autodream import AutoDream
from beagle.memory.memory_index import INDEX_HEADER
from beagle.output.schema import Finding


@pytest.fixture
def temp_workspace(tmp_path):
    (tmp_path / ".beagle").mkdir()
    return tmp_path


@pytest.mark.asyncio
async def test_autodream_refresh(temp_workspace):
    """Test refresh operation adds findings to index."""
    dreamer = AutoDream(temp_workspace)

    # Mock DB findings
    mock_run = MagicMock()
    mock_run.id = "run1"

    mock_finding = MagicMock()
    mock_finding.category = "security"
    mock_finding.title = "New Bug"
    mock_finding.file_path = "test.py"

    with (
        patch.object(dreamer.db, "get_workflow_runs", return_value=[mock_run]),
        patch.object(dreamer.db, "get_findings_for_run", return_value=[mock_finding]),
    ):
        count = await dreamer.refresh()
        assert count == 1

        # Verify index was updated
        content = dreamer.memory_index.get_semantic_layer()
        assert "New Bug" in content


@pytest.mark.asyncio
async def test_full_consolidate(temp_workspace):
    """Test full consolidate runs all operations."""
    dreamer = AutoDream(temp_workspace)

    with (
        patch.object(dreamer, "prune", return_value=1) as mock_prune,
        patch.object(dreamer, "merge", return_value=2) as mock_merge,
        patch.object(dreamer, "refresh", return_value=3) as mock_refresh,
    ):
        report = await dreamer.consolidate()

        assert report.pruned_count == 1
        assert report.merged_count == 2
        assert report.refreshed_count == 3
        mock_prune.assert_called_once()
        mock_merge.assert_called_once()
        mock_refresh.assert_called_once()


def test_autodream_initialization(temp_workspace):
    """Test that AutoDream initializes correctly."""
    dreamer = AutoDream(temp_workspace)
    assert dreamer.memory_index is not None
    assert dreamer.workspace_root == temp_workspace


@pytest.mark.asyncio
async def test_autodream_prune_stale(temp_workspace):
    """Test that stale pointers (>30 days) are removed."""
    dreamer = AutoDream(temp_workspace)

    # Create an index with an old entry
    old_date = "2020-01-01"
    content = f"{INDEX_HEADER}## Recent Findings\n- security: Old Bug [test.py] [{old_date}]\n"
    dreamer.memory_index.index_path.write_text(content)

    pruned = await dreamer.prune()
    assert pruned == 1

    new_content = dreamer.memory_index.get_semantic_layer()
    assert "Old Bug" not in new_content


@pytest.mark.asyncio
async def test_autodream_prune_duplicates(temp_workspace):
    """Test that duplicate pointers are removed."""
    dreamer = AutoDream(temp_workspace)

    # Create an index with duplicate entries
    date = time.strftime("%Y-%m-%d")
    content = (
        f"{INDEX_HEADER}## Recent Findings\n"
        f"- security: Bug [f.py] [{date}]\n"
        f"- security: Bug [f.py] [{date}]\n"
    )
    dreamer.memory_index.index_path.write_text(content)

    pruned = await dreamer.prune()
    assert pruned == 1

    new_content = dreamer.memory_index.get_semantic_layer()
    assert new_content.count("Bug") == 1


@pytest.mark.asyncio
async def test_autodream_prune_preserves_recent(temp_workspace):
    """Test that recent pointers are kept."""
    dreamer = AutoDream(temp_workspace)

    date = time.strftime("%Y-%m-%d")
    content = f"{INDEX_HEADER}## Recent Findings\n- security: Fresh Bug [f.py] [{date}]\n"
    dreamer.memory_index.index_path.write_text(content)

    pruned = await dreamer.prune()
    assert pruned == 0
    assert "Fresh Bug" in dreamer.memory_index.get_semantic_layer()


@pytest.mark.asyncio
async def test_autodream_token_budget_enforcement(temp_workspace):
    """Test that token budget is enforced during consolidation."""
    dreamer = AutoDream(temp_workspace)

    # Create a large index
    date = time.strftime("%Y-%m-%d")
    pointers = [f"- cat: Bug {i} [f.py] [{date}]" for i in range(100)]
    content = f"{INDEX_HEADER}## Recent Findings\n" + "\n".join(pointers)
    dreamer.memory_index.index_path.write_text(content)

    # Mock budget to be small (patch _get_token_budget AND set instance attr,
    # since MemoryIndex resolves budget at __init__ time)
    with patch("beagle.memory.memory_index._get_token_budget", return_value=100):
        dreamer.memory_index.token_budget = 100
        await dreamer.consolidate()

        new_content = dreamer.memory_index.get_semantic_layer()
        # Should be significantly shorter
        assert len(new_content.splitlines()) < 100


@pytest.mark.asyncio
async def test_autodream_empty_index(temp_workspace):
    """Test consolidation with empty index."""
    dreamer = AutoDream(temp_workspace)
    # Ensure empty file
    dreamer.memory_index.index_path.write_text(INDEX_HEADER)

    report = await dreamer.consolidate()
    assert report.errors == []
    assert report.pruned_count == 0


@pytest.mark.asyncio
async def test_autodream_event_publication(temp_workspace):
    """Test that autoDream publishes events to the bus."""
    dreamer = AutoDream(temp_workspace)

    with (
        patch("beagle.events.bus.EventBus.publish") as mock_publish,
        patch.object(dreamer.db, "get_workflow_runs", return_value=[]),
    ):
        report = await dreamer.consolidate()
        assert report.errors == []
        # Should publish prune, merge, refresh, and completed
        print("Calls:", mock_publish.call_args_list)
        assert mock_publish.call_count == 4


@pytest.mark.asyncio
async def test_autodream_error_capture(temp_workspace):
    """Test that errors are captured in the report."""
    dreamer = AutoDream(temp_workspace)

    with patch.object(dreamer, "prune", side_effect=Exception("Prune failed")):
        report = await dreamer.consolidate()
        assert "Prune failed" in report.errors


@pytest.mark.asyncio
async def test_autodream_refresh_multiple_runs(temp_workspace):
    """Test refresh from multiple past runs."""
    dreamer = AutoDream(temp_workspace)

    mock_run1 = MagicMock(id="r1")
    mock_run2 = MagicMock(id="r2")

    f1 = Finding(title="F1", category="c", severity="s")
    f2 = Finding(title="F2", category="c", severity="s")

    with (
        patch.object(dreamer.db, "get_workflow_runs", return_value=[mock_run1, mock_run2]),
        patch.object(dreamer.db, "get_findings_for_run", side_effect=[[f1], [f2]]),
    ):
        count = await dreamer.refresh()
        assert count == 2
        content = dreamer.memory_index.get_semantic_layer()
        assert "F1" in content
        assert "F2" in content


@pytest.mark.asyncio
async def test_autodream_report_metrics_accuracy(temp_workspace):
    """Test report metrics are calculated correctly."""
    dreamer = AutoDream(temp_workspace)

    with (
        patch.object(dreamer, "prune", return_value=5),
        patch.object(dreamer, "merge", return_value=2),
        patch.object(dreamer, "refresh", return_value=10),
    ):
        report = await dreamer.consolidate()
        assert report.pruned_count == 5
        assert report.merged_count == 2
        assert report.refreshed_count == 10
        assert report.duration_seconds > 0
