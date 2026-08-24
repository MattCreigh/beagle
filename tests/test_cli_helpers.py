"""SP-5: tests for cli/helpers + the deprecated shims.

beagle-spotless-phase2, work package SP-5. The merged CLI helpers module
(cli/helpers) and the two deprecated shims (cli/_helpers, cli/cli_helpers)
are covered here. The shims re-export the canonical functions and emit a
DeprecationWarning.
"""

from __future__ import annotations

import warnings

from beagle.cli.helpers import resolve_workflow


def test_resolve_workflow_returns_path() -> None:
    """resolve_workflow resolves a workflow name to a YAML path."""
    from beagle.config.paths import get_workspace_root

    wf = resolve_workflow("research")
    assert wf.exists()
    assert str(wf).endswith(".yaml") or str(wf).startswith(str(get_workspace_root()))


def test_resolve_workflow_literal_path() -> None:
    """resolve_workflow accepts an existing file path."""
    from beagle.config.paths import get_workspace_root

    target = get_workspace_root() / "metaprompts" / "research.yaml"
    if target.exists():
        assert resolve_workflow(str(target)) == target


def test_resolve_workflow_raises_on_missing() -> None:
    """resolve_workflow raises FileNotFoundError for an unknown workflow."""
    import pytest

    with pytest.raises(FileNotFoundError):
        resolve_workflow("definitely-not-a-real-workflow-xyz")


def test_shim_reexports_canonical() -> None:
    """The deprecated shims re-export the canonical functions."""
    from beagle.cli import _helpers, cli_helpers

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert _helpers.resolve_workflow is resolve_workflow
        assert cli_helpers.resolve_workflow is resolve_workflow


def test_shim_emits_deprecation_warning() -> None:
    """Importing a shim emits a DeprecationWarning."""
    import importlib

    for mod in ("beagle.cli._helpers", "beagle.cli.cli_helpers"):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            importlib.reload(importlib.import_module(mod))
        assert any(issubclass(w.category, DeprecationWarning) for w in caught)
