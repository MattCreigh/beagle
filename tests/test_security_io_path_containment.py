"""Security regression tests for ``beagle/blocks/python_blocks/io.py``.

Locks down the path-containment fix that replaced ``str.startswith()``
with ``Path.relative_to()``. The ``startswith`` check is a symlink-
bypass vector: an attacker can create a symlink inside the blocks
root that resolves to a path whose string representation does NOT
begin with the root's string representation (e.g. on case-insensitive
filesystems, with trailing slashes, or with symlinks to /etc/).

If these tests are relaxed, the fix has been undone — escalate.
"""

from __future__ import annotations

import pytest


def _import_contained():
    """Import the private ``_contained`` function from the io module.

    The function is module-private (leading underscore) but its
    containment contract is part of the public security guarantee of
    the blocks subsystem, so testing it is legitimate.
    """
    from beagle.blocks.python_blocks import io as io_mod

    return io_mod._contained, io_mod._get_blocks_root  # type: ignore[attr-defined]


def test_contained_rejects_path_outside_root(tmp_path, monkeypatch):
    """A path whose resolved form escapes the blocks root must raise."""
    _contained, _get_blocks_root = _import_contained()
    fake_root = tmp_path / "blocks_root"
    fake_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret data")

    monkeypatch.setattr(
        "beagle.blocks.python_blocks.io._get_blocks_root",
        lambda: fake_root,
    )

    with pytest.raises(ValueError, match="outside the sandbox"):
        _contained(outside)


def test_contained_accepts_path_inside_root(tmp_path, monkeypatch):
    """A path inside the blocks root is accepted and returned resolved."""
    _contained, _get_blocks_root = _import_contained()
    fake_root = tmp_path / "blocks_root"
    fake_root.mkdir()
    inside = fake_root / "block.py"
    inside.write_text("# block")

    monkeypatch.setattr(
        "beagle.blocks.python_blocks.io._get_blocks_root",
        lambda: fake_root,
    )

    resolved = _contained(inside)
    assert resolved == inside.resolve()


def test_contained_resolves_symlink_to_outside_file(tmp_path, monkeypatch):
    """A symlink inside the root that points outside must be rejected.

    This is the canonical ``str.startswith``-bypass: the string form
    of the symlink path starts with the root's string form, but its
    resolved form does not. ``Path.relative_to`` catches this.
    """
    _contained, _get_blocks_root = _import_contained()
    fake_root = tmp_path / "blocks_root"
    fake_root.mkdir()
    outside = tmp_path / "outside_target.txt"
    outside.write_text("secret")
    symlink_inside = fake_root / "sneaky_link"
    symlink_inside.symlink_to(outside)

    monkeypatch.setattr(
        "beagle.blocks.python_blocks.io._get_blocks_root",
        lambda: fake_root,
    )

    with pytest.raises(ValueError, match="outside the sandbox"):
        _contained(symlink_inside)


def test_contained_handles_traversal_dotdot(tmp_path, monkeypatch):
    """``../`` traversal must be resolved before the containment check."""
    _contained, _get_blocks_root = _import_contained()
    fake_root = tmp_path / "blocks_root"
    fake_root.mkdir()
    subdir = fake_root / "subdir"
    subdir.mkdir()
    traversal = subdir / ".." / ".." / "outside.txt"
    # Ensure the traversal target exists for resolve() to work
    (tmp_path / "outside.txt").write_text("x")

    monkeypatch.setattr(
        "beagle.blocks.python_blocks.io._get_blocks_root",
        lambda: fake_root,
    )

    with pytest.raises(ValueError, match="outside the sandbox"):
        _contained(traversal)
