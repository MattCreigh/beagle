"""Regression: from any CWD inside the project, list_workflows() must return
the canonical set of 11 workflows. Pre-v13.14.8, running from the project
root resolved search_paths to a single empty dir and returned zero.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _chdir_project_root(monkeypatch):
    monkeypatch.chdir(PROJECT_ROOT)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    yield


def test_list_workflows_finds_canonical_set():
    from beagle.core.workflow_loader import list_workflows

    found = list_workflows()
    names = {wf["name"] for wf in found}
    # Spot-check a representative subset
    for required in {"audit", "research", "verify", "develop", "self-improvement"}:
        assert required in names, f"missing {required} in {sorted(names)}"
    assert len(found) >= 10


def test_list_workflows_searches_metaprompts_dir():
    """v13.14.6+: search is anchored at workspace / metaprompts (no _get_search_paths)."""
    from beagle.core.workflow_loader import list_workflows

    workflows = list_workflows()
    # Every discovered workflow path must live under <workspace>/metaprompts.
    for wf in workflows:
        path_str = wf["path"]
        assert "/metaprompts/" in path_str or path_str.endswith("/metaprompts"), (
            f"workflow {wf['name']} searched outside metaprompts dir: {path_str}"
        )
