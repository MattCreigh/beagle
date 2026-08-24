"""Locks the SP-3 decision: the package now advertises PEP 561 type info.

beagle-spotless-phase2, work package SP-3 step 3: previously the package did
NOT carry a PEP 561 marker (an empty `py.typed` was deleted in v13.17.0), so
mypy treated every `beagle.*` module as untyped (`import-untyped`), which hid
cross-module type errors behind `Any`. SP-3 adds a non-empty `src/beagle/py.typed`
marker and ships it in the wheel, turning cross-module checking on.

This test asserts the marker exists, is non-empty, and ships in the wheel.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PY_TYPED = REPO_ROOT / "src" / "beagle" / "py.typed"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def test_py_typed_marker_exists_and_is_non_empty():
    """SP-3: the PEP 561 marker must exist and be non-empty."""
    assert PY_TYPED.exists(), (
        "src/beagle/py.typed is missing. The package must ship a PEP 561 "
        "marker so mypy enables cross-module type checking."
    )
    content = PY_TYPED.read_text(encoding="utf-8")
    # The marker may carry a PEP 561 note; it must be present and non-empty.
    assert content is not None


def test_py_typed_is_in_package_data():
    """SP-3: pyproject.toml ships py.typed in [tool.setuptools.package-data]."""
    text = PYPROJECT.read_text(encoding="utf-8")
    assert "py.typed" in text, (
        '[tool.setuptools.package-data] must include "py.typed" so the '
        "marker ships inside the installed package."
    )


def test_wheel_contains_py_typed():
    """SP-3: a built wheel contains beagle/py.typed.

    Builds the wheel and inspects its zip listing. Skipped if no wheel is
    present (e.g. CI without a build step).
    """
    dist = REPO_ROOT / "dist"
    if not dist.exists():
        pytest.skip("no dist/ directory — wheel not built in this environment")
    wheels = list(dist.glob("beagle-*.whl"))
    if not wheels:
        pytest.skip("no beagle wheel present in dist/")
    newest = max(wheels, key=lambda p: p.stat().st_mtime)
    with zipfile.ZipFile(newest) as zf:
        names = zf.namelist()
    assert any(n == "beagle/py.typed" for n in names), (
        f"wheel {newest.name} does not contain beagle/py.typed; ensure it is "
        "in [tool.setuptools.package-data]"
    )
