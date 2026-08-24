"""B-3 regression test: version SSOT consistency.

``pyproject.toml [project].version`` is the ONLY place the version is written.
Everything else derives it:

- ``beagle.constants.PACKAGE_VERSION`` resolves it from installed distribution
  metadata (which setuptools builds from pyproject), falling back to reading
  pyproject directly for an uninstalled source tree.
- ``beagle/__init__.py`` re-exports that.
- The Dockerfile installs a version-agnostic wheel glob.

v1.0.2: these tests used to compare three *literals* and assert they matched.
That only detected drift after it shipped, and it had shipped twice — the
v1.0.0 release left constants.py on 13.22.3 while __init__ said 1.0.0, and
84d5f10 was a separate fix for the Dockerfile pin lagging at 1.0.0. The tests
now assert the structural property that makes drift impossible: no version
literal exists outside pyproject.toml.

Reference: audit/golden_master_v13.22.0.md B-3, B-4
"""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import version as dist_version
from pathlib import Path

import pytest

from beagle.constants import PACKAGE_VERSION

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read_pyproject_version() -> str:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return pyproject["project"]["version"]


def _init_derives_version_from_constants() -> bool:
    """True if __init__.py takes __version__ from constants.PACKAGE_VERSION.

    constants.py declares itself the version SSOT and says "Do NOT hardcode
    version strings elsewhere". This used to look for a literal
    ``__version__ = "..."`` in __init__.py — i.e. it required the very
    duplicate that directive forbids, and the duplicate had already drifted
    (``__init__`` said 1.0.0 while ``constants`` said 13.22.3).

    Deriving the value makes drift structurally impossible, so the check is
    now "does it derive?" rather than "do the two literals happen to match?".
    """
    text = (REPO_ROOT / "src/beagle/__init__.py").read_text()
    return bool(re.search(r"^__version__\s*=\s*PACKAGE_VERSION\s*$", text, re.MULTILINE)) and bool(
        re.search(r"^from\s+\.constants\s+import\s+.*\bPACKAGE_VERSION\b", text, re.MULTILINE)
    )


def _read_init_version() -> str | None:
    """Resolved ``__version__`` for __init__.py.

    When __init__ derives from constants (the intended shape) the effective
    value is constants' — reading that is what "the package reports version
    X" actually means.
    """
    if _init_derives_version_from_constants():
        return _read_constants_version()
    text = (REPO_ROOT / "src/beagle/__init__.py").read_text()
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else None


def _read_constants_version() -> str | None:
    """Resolved constants version — the derived value, not a parsed literal.

    v1.0.2: constants.py no longer holds a literal to parse, by design.
    """
    return PACKAGE_VERSION


def test_init_does_not_hardcode_a_second_version_literal():
    """__init__.py must derive __version__, not re-declare it.

    constants.py is the declared SSOT ("Do NOT hardcode version strings
    elsewhere"; enforced by the no-hardcoded-version-string pre-commit hook).
    A second literal is free to drift, and did: the v1.0.0 release updated
    __init__.py and pyproject.toml but left constants.py on 13.22.3, so the
    package reported two different versions depending on which name you read.
    """
    assert _init_derives_version_from_constants(), (
        "src/beagle/__init__.py must set `__version__ = PACKAGE_VERSION` imported "
        "from .constants, not a hardcoded literal — constants.py is the SSOT."
    )


def test_pyproject_matches_init():
    """__init__.py __version__ must equal pyproject.toml version."""
    pyproject = _read_pyproject_version()
    init_py = _read_init_version()
    assert init_py is not None, "__init__.py missing __version__"
    assert init_py == pyproject, (
        f"__init__.__version__={init_py!r} != pyproject.toml version={pyproject!r}"
    )


def test_pyproject_matches_constants():
    """constants.PACKAGE_VERSION must equal pyproject.toml version."""
    pyproject = _read_pyproject_version()
    const = _read_constants_version()
    assert const is not None, "constants.py missing PACKAGE_VERSION"
    assert const == pyproject, (
        f"constants.PACKAGE_VERSION={const!r} != pyproject.toml version={pyproject!r}. "
        "PACKAGE_VERSION resolves from installed distribution metadata, so under a "
        "non-editable (wheel) install this means the installed wheel is stale "
        "relative to the working tree — rebuild and reinstall: "
        "`uv build --wheel && uv pip install --python <venv>/bin/python --no-deps "
        "--reinstall dist/beagle-*.whl`."
    )


def test_constants_holds_no_version_literal():
    """constants.py must derive the version, never declare one.

    This is the check that makes drift structurally impossible rather than
    merely detectable. A literal here is free to fall out of step with
    pyproject.toml, and historically did.
    """
    text = (REPO_ROOT / "src/beagle/constants.py").read_text()
    literal = re.search(r'PACKAGE_VERSION\s*:\s*str\s*=\s*"[0-9]', text)
    assert literal is None, (
        "src/beagle/constants.py declares a hardcoded PACKAGE_VERSION literal. "
        "pyproject.toml [project].version is the single source — constants.py "
        "must resolve it via importlib.metadata, not restate it."
    )


def test_resolved_version_matches_installed_distribution():
    """The exported version must be the installed distribution's version."""
    installed = dist_version("beagle")
    assert installed == PACKAGE_VERSION, (
        f"PACKAGE_VERSION={PACKAGE_VERSION!r} != installed dist {installed!r} — "
        "the resolver is not reading distribution metadata."
    )


def test_init_matches_constants():
    """__init__.py and constants.py must agree (transitive check)."""
    init_py = _read_init_version()
    const = _read_constants_version()
    assert init_py is not None and const is not None
    assert init_py == const, (
        f"__init__.__version__={init_py!r} != constants.PACKAGE_VERSION={const!r}"
    )


def test_dockerfile_no_ssot_drift_comment():
    """B-4: Dockerfile must not contain the 'SSOT drift' annotation."""
    dockerfile = REPO_ROOT / "beagle_containerisation/Dockerfile"
    if not dockerfile.exists():
        pytest.skip("Dockerfile not present in this checkout")
    text = dockerfile.read_text()
    assert "SSOT drift" not in text, (
        "Dockerfile contains the SSOT drift annotation — version patch was manual. "
        "Re-run beagle-dockeriser or hand-update with a clean comment."
    )


def test_dockerfile_holds_no_version_literal():
    """Dockerfile must install a version-agnostic wheel glob.

    v1.0.2: this used to assert the Dockerfile pinned the exact wheel filename
    `beagle-<pyproject version>-py3-none-any.whl`. That REQUIRED a fourth copy
    of the version and so guaranteed the drift it was meant to catch — 84d5f10
    exists solely because that pin was left at 1.0.0 for a 1.0.1 release. The
    glob installs whatever `uv build` produced, which is by construction the
    pyproject version.
    """
    dockerfile = REPO_ROOT / "beagle_containerisation/Dockerfile"
    if not dockerfile.exists():
        pytest.skip("Dockerfile not present in this checkout")
    text = dockerfile.read_text()

    pinned = re.search(r"beagle-\d+\.\d+\.\d+[^\s]*\.whl", text)
    assert pinned is None, (
        f"Dockerfile pins an exact wheel version ({pinned.group(0) if pinned else ''}). "
        "Use the beagle-*-py3-none-any.whl glob — pyproject.toml is the only "
        "place the version may be written."
    )
    assert "beagle-*-py3-none-any.whl" in text, (
        "Dockerfile no longer installs the beagle wheel via the expected glob"
    )
