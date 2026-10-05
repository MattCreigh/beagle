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
