#!/usr/bin/env python3
"""Health Check Script for Beagle Agent Containers.

Verifies agent health by checking:
1. Goose binary is accessible
2. Orpheus ring directory is accessible
3. Agent can write to state directory
4. Agent-specific health checks
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from beagle.runtime.goose_cli import GooseCliRuntime

logger = logging.getLogger(__name__)


def check_goose_binary() -> tuple[bool, str]:
    """Verify Goose binary exists and is executable.

    B4: this check is advisory. When the configured sub-agent runtime is
    NOT ``goose_cli`` (e.g. ``http_agent`` over A2A), a missing local goose
    binary is expected and must not fail the overall health check.
    """
    from beagle.runtime.loader import runtime_plugin_name

    if runtime_plugin_name() != "goose_cli":
        return True, "Goose binary not required for the configured runtime"

    goose_path = GooseCliRuntime().binary_path

    # Also check in PATH
    if not os.path.exists(goose_path):
        import shutil

        goose_in_path = shutil.which("goose")
        if goose_in_path:
            goose_path = goose_in_path

    if not os.path.exists(goose_path):
        return False, f"Goose binary not found at {goose_path}"

    if not os.access(goose_path, os.X_OK):
        return False, f"Goose binary not executable: {goose_path}"

    return True, f"Goose binary OK: {goose_path}"


def check_ring_directory() -> tuple[bool, str]:
    """Verify Orpheus ring directory is accessible."""
    # v1.1.1 (S9): ring dir from env, else config [orpheus].ring_dir, else default.
    ring_dir = os.environ.get("ORPHEUS_RING_DIR", "")
    if not ring_dir:
        try:
            import tomllib

            from ..config._config_path import find_config_toml

            _cfg = find_config_toml()
            if _cfg.is_file():
                _data = tomllib.loads(_cfg.read_text(encoding="utf-8"))
                ring_dir = str((_data.get("orpheus") or {}).get("ring_dir") or "")
        except (OSError, ValueError, KeyError, TypeError, ImportError, tomllib.TOMLDecodeError):
            ring_dir = ""
    if not ring_dir:
        ring_dir = "/run/orpheus_ring"
    ring_path = Path(ring_dir)

    if not ring_path.exists():
        return False, f"Ring directory not found: {ring_dir}"

    if not os.access(ring_path, os.R_OK):
        return False, f"Ring directory not readable: {ring_dir}"

    if not os.access(ring_path, os.W_OK):
        return False, f"Ring directory not writable: {ring_dir}"

    return True, f"Ring directory OK: {ring_dir}"


def check_state_directory() -> tuple[bool, str]:
    """Verify agent state directory is writable."""
    state_dir = Path("/app/state")

    try:
        state_dir.mkdir(parents=True, exist_ok=True)

        # Try to write a test file
        test_file = state_dir / ".health_check"
        test_file.write_text("ok")
        test_file.unlink()

        return True, "State directory OK"
    except (OSError, ValueError) as e:
        return False, f"State directory error: {e}"


def check_output_directory() -> tuple[bool, str]:
    """Verify output directory is writable."""
    output_dir = Path("/app/output")

    try:
        output_dir.mkdir(parents=True, exist_ok=True)

        # Try to write a test file
        test_file = output_dir / ".health_check"
        test_file.write_text("ok")
        test_file.unlink()

        return True, "Output directory OK"
    except (OSError, ValueError) as e:
        return False, f"Output directory error: {e}"


def check_recipes() -> tuple[bool, str]:
    """Verify recipes directory is accessible."""
    recipes_dir = Path("/app/recipes")

    if not recipes_dir.exists():
        return False, f"Recipes directory not found: {recipes_dir}"

    recipe_files = list(recipes_dir.glob("*.xml"))
    if not recipe_files:
        return False, f"No recipes found in: {recipes_dir}"

    return True, f"Recipes OK ({len(recipe_files)} recipes)"


def check_agent_specific(agent_type: str) -> tuple[bool, str]:
    """Agent-specific health checks."""
    if agent_type == "planner":
        skill_file = Path("/app/recipes/research-planner.xml")
        if not skill_file.exists():
            return False, f"Planner skill not found: {skill_file}"
        return True, "Planner skill OK"

    elif agent_type == "executor":
        skill_file = Path("/app/recipes/search-executor.xml")
        if not skill_file.exists():
            return False, f"Executor skill not found: {skill_file}"
        return True, "Executor skill OK"

    elif agent_type == "verifier":
        skill_file = Path("/app/recipes/fact-checker.xml")
        if not skill_file.exists():
            return False, f"Verifier skill not found: {skill_file}"
        return True, "Verifier skill OK"

    elif agent_type == "synthesizer":
        skill_file = Path("/app/recipes/synthesis-writer.xml")
        if not skill_file.exists():
            return False, f"Synthesizer skill not found: {skill_file}"
        return True, "Synthesizer skill OK"

    elif agent_type == "orchestrator":
        # Orchestrator just needs basic checks
        return True, "Orchestrator OK"

    else:
        return True, f"Unknown agent type: {agent_type}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Beagle Agent Health Check")
    parser.add_argument("--agent", default="generic", help="Agent type")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    checks = [
        ("Goose Binary", check_goose_binary),
        ("Ring Directory", check_ring_directory),
        ("State Directory", check_state_directory),
        ("Output Directory", check_output_directory),
        ("Recipes", check_recipes),
        ("Agent Specific", lambda: check_agent_specific(args.agent)),
    ]

    all_passed = True
    results = []

    for name, check_fn in checks:
        passed, message = check_fn()
        results.append((name, passed, message))

        if not passed:
            all_passed = False
            logger.warning(f"[FAIL] {name}: {message}")
        elif args.verbose:
            logger.info(f"[OK] {name}: {message}")

    if all_passed:
        logger.info(f"[OK] Health check passed for {args.agent}")
        return 0
    else:
        logger.warning(f"[FAIL] Health check failed for {args.agent}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
