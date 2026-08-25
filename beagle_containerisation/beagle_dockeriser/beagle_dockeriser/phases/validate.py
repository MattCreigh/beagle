"""Phase 1: Golden Master Validation — The Gatekeeper."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ..constants import (
    PYTEST_TESTPATH,
    PYTEST_XDIST_FLAG,
    RUFF_CHECK_TARGETS,
    VALIDATOR_TIMEOUT,
    VESTIGIAL_DIRS,
    VESTIGIAL_EXCLUDE_PREFIXES,
    VULTURE_MIN_CONFIDENCE,
)
from ..models import PipelineState


def run_validation(state: PipelineState) -> PipelineState:
    """Run all Golden Master validation checks.

    Returns modified state with phase1_passed = True only if ALL checks pass.
    On failure, state.errors is populated and pipeline will abort.
    """
    project_root = state.project_root
    results: list[tuple[str, bool, str]] = []

    # ── Check 1: Ruff Lint ──────────────────────────────────────────
    cmd = [sys.executable, "-m", "ruff", "check", *RUFF_CHECK_TARGETS]
    ok, detail = _run_command(cmd, project_root, "ruff lint")
    results.append(("Ruff Lint", ok, detail))

    # ── Check 2: Vulture Dead Code ──────────────────────────────────
    cmd = [
        sys.executable,
        "-m",
        "vulture",
        *RUFF_CHECK_TARGETS,
        f"--min-confidence={VULTURE_MIN_CONFIDENCE}",
    ]
    ok, detail = _run_command(cmd, project_root, "vulture")
    results.append(("Vulture", ok, detail))

    # ── Check 3: Pytest + xdist ─────────────────────────────────────
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        PYTEST_TESTPATH,
        "-v",
        "--tb=short",
        PYTEST_XDIST_FLAG,
    ]
    ok, detail = _run_command(cmd, project_root, "pytest-xdist")
    results.append(("Pytest (xdist)", ok, detail))

    # ── Check 4: Vestigial Directory Scan ───────────────────────────
    vestigial_found = _scan_vestigial(project_root)
    vestigial_ok = len(vestigial_found) == 0
    vestigial_detail = (
        "Clean"
        if vestigial_ok
        else f"Found {len(vestigial_found)} artifacts: {', '.join(vestigial_found[:5])}"
    )
    results.append(("Vestigial Scan", vestigial_ok, vestigial_detail))

    # ── Summary ──────────────────────────────────────────────────────
    all_passed = all(ok for _, ok, _ in results)

    state.phase1_passed = all_passed
    state.phase_details["phase1_detail"] = (
        "All checks passed"
        if all_passed
        else ", ".join(n for n, ok, _ in results if not ok) + " FAILED"
    )

    if not all_passed:
        failed = [n for n, ok, _ in results if not ok]
        state.errors.append(f"Phase 1 validation failed: {', '.join(failed)}")

    return state


def _run_command(
    cmd: list[str],
    cwd: Path,
    label: str,
) -> tuple[bool, str]:
    """Run a subprocess command and return (success, detail_string)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=VALIDATOR_TIMEOUT,
        )
        if result.returncode == 0:
            if "pytest" in label:
                for line in result.stdout.splitlines():
                    if "passed" in line:
                        return True, line.strip()
                return True, "All tests passed"
            return True, "OK"
        else:
            err_lines = result.stderr.strip().splitlines()[:3]
            return False, " | ".join(err_lines)
    except subprocess.TimeoutExpired:
        return False, f"Timeout after {VALIDATOR_TIMEOUT}s"
    except FileNotFoundError:
        return False, f"Command not found: {cmd[0]}"


def _scan_vestigial(project_root: Path) -> list[str]:
    """Scan project root for vestigial build artifacts.

    Checks for: build/, dist/, *.egg-info/, __pycache__/,
    .pytest_cache/, .ruff_cache/, htmlcov/, .mypy_cache/

    Does NOT flag ai/ or skills/ — these are active package modules
    embedded in the wheel, not vestigial directories.
    """
    found: list[str] = []

    for pattern in VESTIGIAL_DIRS:
        if pattern == "__pycache__":
            matches = list(project_root.rglob("__pycache__"))
            exclude_targets = [project_root / p for p in VESTIGIAL_EXCLUDE_PREFIXES]
            matches = [
                m for m in matches if not any(_is_contained(m, et) for et in exclude_targets)
            ]
            if matches:
                found.append(f"__pycache__/ ({len(matches)} directories)")
        elif pattern == "*.egg-info":
            matches = list(project_root.glob("*.egg-info"))
            if matches:
                found.extend(str(m.relative_to(project_root)) for m in matches)
        else:
            target = project_root / pattern
            if target.exists():
                found.append(pattern + "/")

    return found


def _is_contained(path: Path, prefix: Path) -> bool:
    """Check if path is within prefix, handling relative_to failures."""
    try:
        path.resolve().relative_to(prefix.resolve())
        return True
    except ValueError:
        return False
