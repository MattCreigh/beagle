"""TC2 fidelity tests — `beagle.infrastructure.health_check` (performance-remediation-002).

Source brief: "Supplement 3.zip" / `performance-remediation-002.xml`, test class TC2
"Health check fidelity test": *inject a fault; assert the health check reports an
unhealthy status. A health check that reports a healthy status during an injected
fault is a defect.*

These pin the `beagle.infrastructure.health_check` functions (the container health
gate, distinct from `beagle.startup.health_check`). Each test injects a fault and
asserts the function returns ``(False, ...)`` — i.e. it fails CLOSED, matching the
T4.1 fail-closed doctrine in `security_baseline.toml`.

The functions under test resolve paths at call time and call ``os``/``Path``
directly, so a fault is injected by monkeypatching those module-level
dependencies — no network, no subprocess.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import beagle.infrastructure.health_check as hc


def _path_ctor(path: Path):
    """Return a Path callable that always returns *path* regardless of args.

    The health-check functions build ``Path(...)`` directly; this lets a test
    force any given path without a real filesystem side effect.
    """

    def _ctor(*_args, **_kwargs):
        return path

    return _ctor


# ---------------------------------------------------------------------------
# check_ring_directory — fault: missing / unreadable / unwritable ring dir
# ---------------------------------------------------------------------------


def test_ring_directory_missing_is_unhealthy(monkeypatch):
    """A non-existent ring directory must report unhealthy."""
    monkeypatch.setenv("ORPHEUS_RING_DIR", "/tmp/definitely_missing_ring")
    ok, msg = hc.check_ring_directory()
    assert ok is False, f"expected unhealthy, got ok={ok}: {msg}"
    assert "not found" in msg.lower()


def test_ring_directory_unreadable_is_unhealthy(monkeypatch):
    """An existing but unreadable ring directory must report unhealthy."""
    monkeypatch.setenv("ORPHEUS_RING_DIR", "/tmp")
    monkeypatch.setattr(hc.os, "access", lambda _p, _m: False)
    ok, msg = hc.check_ring_directory()
    assert ok is False
    assert "readable" in msg.lower()


def test_ring_directory_unwritable_is_unhealthy(monkeypatch):
    """An existing, readable but unwritable ring directory must report unhealthy."""
    monkeypatch.setenv("ORPHEUS_RING_DIR", "/tmp")

    def _fake_access(path: str, mode: int) -> bool:
        return mode != os.W_OK

    monkeypatch.setattr(hc.os, "access", _fake_access)
    ok, msg = hc.check_ring_directory()
    assert ok is False
    assert "writable" in msg.lower()


def test_ring_directory_ok_when_present(monkeypatch):
    """Happy path: existing, readable, writable ring dir -> healthy."""
    monkeypatch.setenv("ORPHEUS_RING_DIR", "/tmp")
    ok, msg = hc.check_ring_directory()
    assert ok is True


# ---------------------------------------------------------------------------
# check_state_directory / check_output_directory — fault: write fails
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fn", ["check_state_directory", "check_output_directory"])
def test_directory_write_failure_is_unhealthy(monkeypatch, fn):
    """If a probe write raises OSError, the directory check must report unhealthy."""
    failing_path = MagicMock()
    # mkdir succeeds; the probe write raises OSError (e.g. read-only fs / quota).
    failing_path.mkdir.return_value = None
    # The probe write is via `test_file = state_dir / ".health_check"`; the
    # __truediv__ result is a distinct mock whose write_text raises OSError.
    failing_path.__truediv__.return_value.write_text.side_effect = OSError(
        "read-only file system"
    )
    monkeypatch.setattr(hc, "Path", _path_ctor(failing_path))

    ok, msg = getattr(hc, fn)()
    assert ok is False, f"{fn} reported healthy under write fault: {msg}"
    assert "error" in msg.lower() or "OSError" in msg


# ---------------------------------------------------------------------------
# check_recipes / check_agent_specific — fault: recipes dir missing/empty
# ---------------------------------------------------------------------------


def test_recipes_missing_dir_is_unhealthy(monkeypatch):
    """Missing recipes directory -> unhealthy."""
    monkeypatch.setattr(hc, "Path", _path_ctor(Path("/tmp/definitely_missing_recipes")))
    ok, msg = hc.check_recipes()
    assert ok is False
    assert "not found" in msg.lower()


def test_recipes_empty_is_unhealthy(monkeypatch):
    """Recipes dir present but no .xml files -> unhealthy (no silent pass)."""
    empty_dir = Path("/tmp/empty_recipes_test")
    empty_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(hc, "Path", _path_ctor(empty_dir))
    ok, msg = hc.check_recipes()
    assert ok is False
    assert "no recipes" in msg.lower()


def test_recipes_ok_with_xml(monkeypatch, tmp_path):
    """Recipes dir with an .xml file -> healthy."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "research-planner.xml").write_text("<xml/>")
    monkeypatch.setattr(hc, "Path", _path_ctor(tmp_path))
    ok, msg = hc.check_recipes()
    assert ok is True


def test_agent_specific_missing_skill_is_unhealthy(monkeypatch):
    """A named agent skill that is absent -> unhealthy."""
    monkeypatch.setattr(hc, "Path", _path_ctor(Path("/tmp/missing_skill.xml")))
    ok, msg = hc.check_agent_specific("planner")
    assert ok is False
    assert "planner skill not found" in msg.lower()


def test_agent_specific_present_skill_is_healthy(monkeypatch, tmp_path):
    """Present named skill -> healthy."""
    skill = tmp_path / "research-planner.xml"
    skill.write_text("<xml/>")
    monkeypatch.setattr(hc, "Path", _path_ctor(skill))
    ok, _ = hc.check_agent_specific("planner")
    assert ok is True


# ---------------------------------------------------------------------------
# check_goose_binary — fault: binary missing / not executable
# ---------------------------------------------------------------------------


def test_goose_binary_missing_is_unhealthy(monkeypatch):
    """When the configured runtime is goose_cli and the binary is absent -> unhealthy."""
    monkeypatch.setattr("beagle.runtime.loader.runtime_plugin_name", lambda: "goose_cli")
    goose = MagicMock()
    goose.binary_path = "/tmp/definitely_missing_goose_bin"
    monkeypatch.setattr(hc, "GooseCliRuntime", lambda: goose)
    monkeypatch.setattr(hc.os.path, "exists", lambda _p: False)

    ok, msg = hc.check_goose_binary()
    assert ok is False
    assert "not found" in msg.lower()


def test_goose_binary_not_executable_is_unhealthy(monkeypatch):
    """Present but non-executable goose binary -> unhealthy."""
    monkeypatch.setattr("beagle.runtime.loader.runtime_plugin_name", lambda: "goose_cli")
    goose = MagicMock()
    goose.binary_path = "/usr/bin/goose"
    monkeypatch.setattr(hc, "GooseCliRuntime", lambda: goose)
    monkeypatch.setattr(hc.os.path, "exists", lambda _p: True)
    monkeypatch.setattr(hc.os, "access", lambda _p, _m: False)
    ok, err = hc.check_goose_binary()
    assert ok is False
    assert "not executable" in err.lower()


def test_goose_binary_advisory_when_not_goose_cli(monkeypatch):
    """When runtime is NOT goose_cli, a missing binary is advisory (healthy)."""
    monkeypatch.setattr("beagle.runtime.loader.runtime_plugin_name", lambda: "http_agent")
    ok, msg = hc.check_goose_binary()
    assert ok is True
    assert "not required" in msg.lower()
