"""Verification blocks for quality assurance."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from beagle.config.paths import resolve_executable

from .base import python_block


@python_block(name="check_file_exists", description="Check if a file exists")
def check_file_exists(_ctx: Any, *, path: str) -> bool:
    return Path(path).exists()


@python_block(name="validate_schema", description="Validate dict against a JSON schema")
def validate_schema(_ctx: Any, *, data: dict, schema: dict) -> dict:
    errors: list[str] = []
    for key, expected_type in schema.items():
        if key not in data:
            errors.append(f"Missing key: {key}")
        elif expected_type == "string" and not isinstance(data[key], str):
            errors.append(f"{key}: expected string, got {type(data[key]).__name__}")
        elif expected_type == "int" and not isinstance(data[key], int):
            errors.append(f"{key}: expected int, got {type(data[key]).__name__}")
        elif expected_type == "list" and not isinstance(data[key], list):
            errors.append(f"{key}: expected list, got {type(data[key]).__name__}")
    return {"valid": len(errors) == 0, "errors": errors}


@python_block(name="run_linter", description="Run ruff on a file or directory")
def run_linter(_ctx: Any, *, target: str) -> dict:
    import subprocess

    result = subprocess.run(
        [resolve_executable("ruff"), "check", target],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


@python_block(name="run_tests", description="Run pytest on a target")
def run_tests(_ctx: Any, *, target: str, timeout: float = 120.0) -> dict:
    import subprocess

    result = subprocess.run(
        # sys.executable, not a PATH lookup for "python3": the tests must run
        # under the interpreter that is running this process, not whichever
        # python3 happens to come first on PATH.
        [sys.executable, "-m", "pytest", target, "-v", "--tb=short"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return {
        "returncode": result.returncode,
        "passed": " passed" in result.stdout,
        "stdout": result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout,
        "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
    }
