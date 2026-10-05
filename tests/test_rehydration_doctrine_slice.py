"""BEAGLE MIND RELAY P2 — doctrine slice inside the rehydration sidecar prompt."""

from __future__ import annotations

from pathlib import Path

from beagle.context.post_compaction_rehydration import build_rehydration_prompt


def test_prompt_contains_compact_doctrine_slice() -> None:
    p = build_rehydration_prompt(
        workflow_id="t",
        query="q",
        project_dir=Path("/home/server/Projects/beagle"),
    )
    assert "<doctrine_slice>" in p
    assert "<beagle_top_of_mind compact=\"true\">" in p
    # bounded by COMPACT_MAX_BYTES
    start = p.index("<doctrine_slice>")
    end = p.index("</doctrine_slice>")
    assert end - start <= 2048 + 200  # cap + wrapper slack


def test_no_project_dir_still_slices() -> None:
    p = build_rehydration_prompt(workflow_id="t2", query="q2")
    assert "<doctrine_slice>" in p


def test_pd2_doctrine_slice_soak_100_folds() -> None:
    """P-D2 chaos drill (liveness L2): 100 consecutive folds must keep the
    sidecar doctrine block <= 8 KB and must never duplicate the full doctrine.

    Re-runnable regression guard standing in for the plan's one-shot drill.
    """
    project = Path("/home/server/Projects/beagle")
    max_block = 0
    for i in range(100):
        p = build_rehydration_prompt(
            workflow_id=f"soak-{i}",
            query="soak",
            project_dir=project,
        )
        start = p.index("<doctrine_slice>")
        end = p.index("</doctrine_slice>")
        block = p[start:end]
        max_block = max(max_block, len(block))
        assert block, f"fold {i}: empty doctrine slice"
        assert len(block) <= 8192, f"fold {i}: doctrine block {len(block)}B > 8 KB"
        assert block.count("<beagle_top_of_mind") == 1, f"fold {i}: doctrine duplicated"
    assert max_block <= 8192
