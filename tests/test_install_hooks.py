"""Tests for the hook-template renderer (QA-2, BGL-066/BGL-067).

The tracked source of truth is the ``*.template`` file.  The rendered
sibling names an absolute path and is git-ignored.  These tests prove the
renderer substitutes the repo root and is idempotent.
"""

from __future__ import annotations

from pathlib import Path

from scripts.install_hooks import render_templates


def test_render_substitutes_repo_root(tmp_path: Path) -> None:
    """A template holding ``{repo_root}`` renders to the absolute path."""
    plugins = tmp_path / "plugins"
    (plugins / "beagle-context-management" / "hooks").mkdir(parents=True)
    template = plugins / "beagle-context-management" / "hooks" / "hooks.json.template"
    template.write_text('{"command": "{repo_root}/scripts/hooks/x.py"}', encoding="utf-8")

    written = render_templates(tmp_path, plugins)

    assert written == [plugins / "beagle-context-management" / "hooks" / "hooks.json"]
    rendered = written[0].read_text(encoding="utf-8")
    assert "{repo_root}" not in rendered
    assert f"{tmp_path}/scripts/hooks/x.py" in rendered


def test_render_is_idempotent(tmp_path: Path) -> None:
    """Rendering twice produces identical bytes."""
    plugins = tmp_path / "plugins"
    (plugins / "p" / "hooks").mkdir(parents=True)
    template = plugins / "p" / "hooks" / "hooks.json.template"
    template.write_text('{"command": "{repo_root}/a.py"}', encoding="utf-8")

    render_templates(tmp_path, plugins)
    first = (plugins / "p" / "hooks" / "hooks.json").read_bytes()
    render_templates(tmp_path, plugins)
    second = (plugins / "p" / "hooks" / "hooks.json").read_bytes()

    assert first == second
