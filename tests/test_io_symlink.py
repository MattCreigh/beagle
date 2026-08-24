"""Regression tests for symlink-bypass prevention in io.py (v13.12.9 B10 fix).

Ensures ``_validate_path`` rejects paths that traverse outside the
containment root, including via symlink indirection.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

try:
    from beagle_openclaw.server import _validate_path  # type: ignore[import-not-found]
except ImportError:  # plugin is optional; the containment contract moves with it
    pytest.skip("beagle-openclaw plugin not installed", allow_module_level=True)


class TestSymlinkBypass:
    """Symlink-bypass rejection tests."""

    def test_dotdot_rejected(self) -> None:
        """Direct ../ escape is rejected."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            with pytest.raises(ValueError, match="subpath"):
                _validate_path("../etc/passwd", root=root)

    def test_symlink_outside_rejected(self) -> None:
        """Symlink pointing outside root is rejected.

        Creates a symlink inside root that points to a target outside root.
        The target file must exist for .resolve() to follow the symlink.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            # Create a target file OUTSIDE root
            target_path = root.parent / "outside_target"
            target_path.write_text("forbidden")
            # Create symlink inside root pointing to outside target
            symlink_path = root / "escape"
            os.symlink(str(target_path), str(symlink_path))
            with pytest.raises(ValueError, match="subpath"):
                _validate_path("escape", root=root)

    def test_legitimate_file_allowed(self) -> None:
        """File inside root is allowed."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            (root / "legit.txt").write_text("hello")
            result = _validate_path("legit.txt", root=root)
            assert result == root / "legit.txt"

    def test_nested_subdir_allowed(self) -> None:
        """File in nested subdirectory inside root is allowed."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            nested = root / "sub" / "deep"
            nested.mkdir(parents=True)
            (nested / "data.txt").write_text("data")
            result = _validate_path("sub/deep/data.txt", root=root)
            assert result == nested / "data.txt"
