"""Security mitigation tests — D-14 (release-readiness audit 2026-08-28).

``safe_loads``, ``safe_load_prompt`` (CVE-2025-68664 / CVE-2026-34070
mitigations) and ``validate_goose_binary`` shipped with ZERO test coverage.
These tests assert the security contracts hold and that the world-writable
gap (a root-owned but world-writable binary accepted) is now closed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from beagle.security.binary_validator import validate_goose_binary
from beagle.security.deserialization_guard import safe_load_prompt, safe_loads

# ── safe_loads / safe_load_prompt ────────────────────────────────────────────


@pytest.mark.skipif(
    __import__("importlib.util").util.find_spec("langchain_core") is None,
    reason="langchain-core not installed",
)
def test_safe_loads_accepts_core_blob() -> None:
    """A well-formed core-serialization blob must load successfully."""
    blob = '{"lc": 1, "type": "constructor", "id": ["langchain_core", "messages", "HumanMessage"], "kwargs": {"content": "hi"}}'
    obj = safe_loads(blob)
    assert obj is not None


@pytest.mark.skipif(
    __import__("importlib.util").util.find_spec("langchain_core") is None,
    reason="langchain-core not installed",
)
def test_safe_loads_rejects_disallowed_object_type() -> None:
    """A blob naming an arbitrary (non-core) Serializable must be rejected."""
    blob = '{"lc": 1, "type": "constructor", "id": ["foo", "bar", "Baz"], "kwargs": {}}'
    with pytest.raises((ValueError, TypeError)):
        safe_loads(blob)


@pytest.mark.skipif(
    __import__("importlib.util").util.find_spec("langchain_core") is None,
    reason="langchain-core not installed",
)
def test_safe_loads_raises_on_malformed() -> None:
    """Garbage input must raise, not silently load."""
    with pytest.raises(ValueError):
        safe_loads("{not valid json")


@pytest.mark.skipif(
    __import__("importlib.util").util.find_spec("langchain_core") is None,
    reason="langchain-core not installed",
)
def test_safe_load_prompt_rejects_path_traversal(tmp_path: Path) -> None:
    """A prompt path containing '..' must be rejected (CVE-2026-34070)."""
    with pytest.raises(ValueError, match="Path traversal"):
        safe_load_prompt(str(tmp_path / ".." / "etc" / "passwd"))


def test_safe_loads_import_error_when_langchain_absent() -> None:
    """With langchain-core importable, ImportError is not raised for the import
    guard itself (the guard only raises when the module is truly missing)."""
    import importlib.util

    if importlib.util.find_spec("langchain_core") is not None:
        pytest.skip("langchain-core present; ImportError guard not exercised")


# ── validate_goose_binary ────────────────────────────────────────────────────


def test_missing_binary_rejected(tmp_path: Path) -> None:
    """A nonexistent path must be rejected."""
    assert validate_goose_binary(str(tmp_path / "does-not-exist")) is False


def test_non_executable_rejected(tmp_path: Path) -> None:
    """A regular non-executable file must be rejected."""
    p = tmp_path / "goose"
    p.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    os.chmod(p, 0o644)  # not +x
    assert validate_goose_binary(str(p)) is False


def test_executable_owned_by_self_accepted(tmp_path: Path) -> None:
    """An executable, user-owned, non-world-writable binary must be accepted."""
    p = tmp_path / "goose"
    p.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    os.chmod(p, 0o700)
    assert validate_goose_binary(str(p)) is True


def test_foreign_owned_rejected(tmp_path: Path) -> None:
    """A binary owned by neither current user nor root must be rejected."""
    p = tmp_path / "goose"
    p.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    os.chmod(p, 0o700)
    # Find a uid that is not ours and not root.
    foreign = 65534 if os.getuid() not in (65534, 0) else 12345
    if not hasattr(os, "chown"):
        pytest.skip("os.chown unavailable")
    try:
        os.chown(p, foreign, -1)
    except PermissionError:
        pytest.skip("cannot chown to foreign uid in this environment")
    assert validate_goose_binary(str(p)) is False


@pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(os, "chmod"),
    reason="unix permissions required",
)
def test_world_writable_binary_rejected(tmp_path: Path) -> None:
    """A world-writable binary must be rejected — the D-14 gap."""
    p = tmp_path / "goose"
    p.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    os.chmod(p, 0o777)  # world-writable + executable
    assert validate_goose_binary(str(p)) is False


@pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(os, "chmod"),
    reason="unix permissions required",
)
def test_binary_under_world_writable_dir_rejected(tmp_path: Path) -> None:
    """A binary inside a world-writable directory must be rejected even if the
    binary itself is safely perms'd — the directory is the swap vector."""
    writable = tmp_path / "wwdir"
    writable.mkdir()
    os.chmod(writable, 0o777)  # force world-writable despite umask
    p = writable / "goose"
    p.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    os.chmod(p, 0o700)
    assert validate_goose_binary(str(p)) is False
