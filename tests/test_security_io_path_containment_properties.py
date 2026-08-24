"""Property-based tests for path containment using Hypothesis.

Locks down the contract that ``Path.relative_to``-based containment
rejects all paths whose resolved form escapes the root, regardless of
how the attacker constructs the input.

If these tests fail, the symlink-bypass vector is back.
"""

from __future__ import annotations

import string
from pathlib import Path

import pytest

hypothesis = pytest.importorskip("hypothesis")
given = hypothesis.given
settings = hypothesis.settings
HealthCheck = hypothesis.HealthCheck
st = hypothesis.strategies


# Strategies for safe path components (no traversal, no nulls).
_safe_component = st.text(
    alphabet=string.ascii_letters + string.digits + "_-",
    min_size=1,
    max_size=20,
)
_safe_path_inside = st.lists(_safe_component, min_size=1, max_size=5).map(
    lambda parts: Path(*parts)
)


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    """Create a fake blocks root and patch the module's getter."""
    root = tmp_path / "blocks_root"
    root.mkdir()
    # Create some subdirs so realistic relative paths can be tested
    for i in range(3):
        (root / f"subdir_{i}").mkdir()
    # Create an outside file that symlinks can target
    (tmp_path / "outside.txt").write_text("secret")

    monkeypatch.setattr(
        "beagle.blocks.python_blocks.io._get_blocks_root",
        lambda: root,
    )
    return root, tmp_path / "outside.txt"


def _import_contained():
    from beagle.blocks.python_blocks import io as io_mod

    return io_mod._contained  # type: ignore[attr-defined]


# ── Property: any path composed of safe components inside root → accepted ──


@given(components=st.lists(_safe_component, min_size=1, max_size=4))
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_inside_paths_always_accepted(fake_root, components):
    """A path built from safe components inside the root is accepted."""
    root, _outside = fake_root
    candidate = root.joinpath(*components)
    # The candidate must not exist — _contained only checks containment,
    # not existence. We pass a non-existent path and verify it's accepted
    # *if* its resolved form is inside the root.
    _contained = _import_contained()
    resolved = _contained(candidate)
    assert resolved.is_relative_to(root)


# ── Property: any ``..`` traversal escapes and is rejected ────────────────


@given(depth=st.integers(min_value=1, max_value=5))
@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_traversal_always_rejected(fake_root, depth):
    """A path with N '..' components escapes the root by definition."""
    root, _outside = fake_root
    traversal = root.joinpath(*([".."] * depth), "outside.txt")
    _contained = _import_contained()
    with pytest.raises(ValueError, match="outside the sandbox"):
        _contained(traversal)


# ── Property: symlink to outside is always rejected ───────────────────────


def test_symlink_bypass_always_rejected(fake_root):
    """A symlink inside the root that points outside is rejected.

    This is the canonical ``str.startswith``-bypass vector. The fix
    resolves symlinks before checking containment.
    """
    root, outside = fake_root
    link = root / "sneaky"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this filesystem")

    _contained = _import_contained()
    with pytest.raises(ValueError, match="outside the sandbox"):
        _contained(link)
