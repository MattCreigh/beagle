# Copyright (c) 2026 Matt Creigh. Released under the MIT License.
# SPDX-License-Identifier: MIT
"""Wire the editable-install gate into the pytest suite.

House policy is non-editable at every layer, but the policy was stated in the
style guides and never enforced, so 25 editable installs accumulated across the
project venvs and were only found by a manual sweep. These tests make the gate
live: a regression in the detector, or a new editable install in the environment
running the suite, fails the build instead of landing unnoticed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "check_no_editable_installs.py"


def _run(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=_REPO_ROOT,
        check=False,
    )


def test_gate_selftest_detects_planted_violation() -> None:
    """The detector still catches both editable mechanisms (--selftest mode)."""
    result = _run("--selftest", timeout=60)
    assert result.returncode == 0, f"selftest failed:\n{result.stdout}\n{result.stderr}"
    assert "selftest passed" in result.stdout


def test_environment_running_the_suite_is_not_editable() -> None:
    """The interpreter executing this test resolves its packages from site-packages.

    This is the assertion that would have caught the original defect: an
    editable install puts ``src/`` on ``sys.path``, which makes mypy treat the
    package as a library and silently suppress errors in followed modules.
    """
    venv = Path(sys.prefix)
    if not (venv / "pyvenv.cfg").is_file():
        return  # not a venv (e.g. system interpreter) — nothing to assert
    result = _run("--venv", str(venv))
    assert result.returncode == 0, (
        f"editable install in the active environment:\n{result.stdout}\n{result.stderr}"
    )


def test_planted_editable_install_is_reported(tmp_path: Path) -> None:
    """A planted PEP 610 editable marker exits 1 — the gate is live, not a literal."""
    site = tmp_path / "venv" / "lib" / "python3.13" / "site-packages"
    site.mkdir(parents=True)
    (tmp_path / "venv" / "pyvenv.cfg").write_text("", encoding="utf-8")
    dist_info = site / "planted-1.0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "direct_url.json").write_text(
        json.dumps({"url": "file:///src/planted", "dir_info": {"editable": True}}),
        encoding="utf-8",
    )

    result = _run("--venv", str(tmp_path / "venv"))
    assert result.returncode == 1, f"gate did not fire:\n{result.stdout}\n{result.stderr}"
    assert "planted-1.0.0.dist-info" in result.stdout

    # And the same environment passes once the marker is gone.
    (dist_info / "direct_url.json").unlink()
    clean = _run("--venv", str(tmp_path / "venv"))
    assert clean.returncode == 0, f"gate did not clear:\n{clean.stdout}\n{clean.stderr}"


def test_json_output_reports_count() -> None:
    """--json emits a machine-readable envelope with a count."""
    result = _run("--json", "--venv", sys.prefix, timeout=60)
    assert result.returncode == 0, f"json scan failed:\n{result.stdout}\n{result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["count"] == 0
    assert payload["status"] == "clean"
