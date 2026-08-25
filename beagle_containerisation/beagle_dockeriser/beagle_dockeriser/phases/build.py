"""Phase 2: Wheel Builder — Source to Wheel via uv build."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..constants import BUILD_TIMEOUT, PROJECT_VERSION, UV_BINARY
from ..models import PipelineState, WheelSpec


def run_build(state: PipelineState) -> PipelineState:
    """Build the production wheel using uv.

    Steps:
    1. Clean previous dist/ artifacts
    2. Run `uv build` in project root
    3. Locate and validate the generated .whl
    4. Populate state.wheel_spec
    """
    project_root = state.project_root
    dist_dir = project_root / "dist"

    # ── Step 1: Clean previous dist/ ─────────────────────────────────
    _clean_dist(dist_dir)

    # ── Step 2: Run uv build ─────────────────────────────────────────
    cmd = [UV_BINARY, "build", str(project_root)]
    ok, output = _run_uv_build(cmd, project_root)

    if not ok:
        state.errors.append(f"Phase 2 build failed: {output}")
        state.phase2_passed = False
        return state

    # ── Step 3: Locate the generated .whl ────────────────────────────
    wheel_files = list(dist_dir.glob("*.whl"))
    if not wheel_files:
        state.errors.append("Phase 2: No .whl file found in dist/ after build")
        state.phase2_passed = False
        return state

    wheel_path = wheel_files[0]

    # ── Step 4: Build WheelSpec ──────────────────────────────────────
    wheel_name = wheel_path.name
    if PROJECT_VERSION not in wheel_name:
        state.warnings.append(f"Wheel version mismatch: expected {PROJECT_VERSION} in {wheel_name}")

    wheel_spec = WheelSpec(
        path=wheel_path,
        name=wheel_name,
        size_bytes=wheel_path.stat().st_size,
        version=PROJECT_VERSION,
    )

    state.wheel_spec = wheel_spec
    state.phase2_passed = True
    state.phase_details["phase2_detail"] = f"{wheel_spec.name} ({wheel_spec.size_mb}MB)"

    return state


def _clean_dist(dist_dir: Path) -> None:
    """Remove all files from dist/ directory."""
    if dist_dir.exists():
        for f in dist_dir.iterdir():
            if f.is_file():
                f.unlink()


def _run_uv_build(cmd: list[str], cwd: Path) -> tuple[bool, str]:
    """Execute uv build and return (success, output_or_error)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, result.stderr.strip()[:500]
    except FileNotFoundError:
        return False, (
            f"uv not found at {UV_BINARY}. "
            "Install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
        )
    except subprocess.TimeoutExpired:
        return False, "uv build timed out after 120s"
