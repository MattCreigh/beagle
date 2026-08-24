#!/usr/bin/env python3
"""Render every ``hooks.json.template`` under ``.agents/plugins/``.

The tracked source of truth is the ``*.template`` file, which carries the
literal ``{repo_root}`` placeholder.  The rendered sibling (the file with the
``.template`` suffix removed) names an absolute path and is therefore
git-ignored: it is a build artefact of this script, not reviewed source.

Usage:
    python3 scripts/install_hooks.py

Exit code 0 → every template rendered and written.
"""

from __future__ import annotations

from pathlib import Path

from beagle.utils.atomic import atomic_write_text


def render_templates(repo_root: Path, plugins_dir: Path) -> list[Path]:
    """Render every ``*.template`` under ``plugins_dir`` to its sibling.

    For each ``*.template`` file, write the sibling whose name is the
    template name with the ``.template`` suffix removed, replacing every
    occurrence of the literal ``{repo_root}`` with ``str(repo_root)``.

    Args:
        repo_root: Repository root, substituted for ``{repo_root}``.
        plugins_dir: Directory tree to scan for ``*.template`` files.

    Returns:
        The list of files written.

    Raises:
        OSError: A write failed.

    <invariant>
      Running this script twice leaves the rendered files byte-identical.
    </invariant>
    """
    written: list[Path] = []
    for template in sorted(plugins_dir.rglob("*.template")):
        rendered = template.with_suffix("")
        text = template.read_text(encoding="utf-8").replace("{repo_root}", str(repo_root))
        atomic_write_text(rendered, text, mode=0o644)
        written.append(rendered)
    return written


def main() -> int:
    """Render all hook templates and report what was written.

    Returns:
        0 on success.
    """
    repo_root = Path(__file__).resolve().parent.parent
    plugins_dir = repo_root / ".agents" / "plugins"
    for path in render_templates(repo_root, plugins_dir):
        print(f"rendered {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
