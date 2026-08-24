"""I/O blocks for file operations.

v13.17.1: All file operations are now sandboxed to the project root.
Path.relative_to() containment ensures no traversal outside the workspace.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .base import python_block

# ── Sandbox containment ────────────────────────────────────────────────────
# _get_blocks_root() is evaluated lazily so tests can set BEAGLE_BLOCKS_ROOT
# after module import and have it take effect.


def _get_blocks_root() -> Path:
    """Resolve the sandbox root from environment or current directory."""
    return Path(os.environ.get("BEAGLE_BLOCKS_ROOT", os.getcwd())).resolve()


def _contained(path: Path) -> Path:
    """Resolve path and verify containment within BLOCKS_ROOT.

    Raises ValueError if the resolved path escapes the sandbox.
    Uses Path.relative_to() per doctrine (not str.startswith — symlink-bypass).
    """
    root = _get_blocks_root()
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError(
            f"Path {path!r} resolves to {resolved!r} which is outside the sandbox root {root!r}"
        ) from None
    return resolved


@python_block(name="read_file", description="Read contents of a file")
def read_file(_ctx: Any, *, path: str) -> str:
    return _contained(Path(path)).read_text(encoding="utf-8")


@python_block(name="write_file", description="Write contents to a file")
def write_file(_ctx: Any, *, path: str, content: str) -> str:
    target = _contained(Path(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return str(target)


@python_block(name="glob_files", description="Find files matching a pattern")
def glob_files(_ctx: Any, *, pattern: str, directory: str = ".") -> list[str]:
    root = _get_blocks_root()
    base = _contained(Path(directory))
    return [
        str(p) for p in base.rglob(pattern) if root in p.resolve().parents or p.resolve() == root
    ]
