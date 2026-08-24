"""Staged file writing with lint-before-write validation and deterministic patching.

This module enforces two architectural rules:
1. **Staged Validation (Lint-Before-Write):** Proposed file content is written to a
   temporary file first, validated by a linter (py_compile for .py, yaml.safe_load
   for .yaml), and only atomically replaced on success.
2. **Deterministic In-Memory Patching:** File updates are applied via pure-Python
   line-level diff operations — never via the `patch` CLI tool, which is prone to
   hunk-matching failures in agentic workflows.

Integration with Beagle:
    The autonomous orchestrator uses staged_write() for all file outputs
    (final reports, playbooks) instead of raw open/write calls, ensuring that
    no corrupt files are written to disk.

Usage:
    from beagle.utils.file_writer import staged_write, apply_patch

    # Full-file write with validation
    result = staged_write("/path/to/module.py", new_content)
    if not result.success:
        logger.info(f"Lint error: {result.error}")

    # Deterministic in-memory patch + validated write
    result = apply_patch("/path/to/module.py", original_lines, patched_lines)
"""

from __future__ import annotations

import contextlib
import logging
import os
import py_compile
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path

from beagle.config.paths import resolve_executable

logger = logging.getLogger("Beagle.file_writer")

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteResult:
    """Result of a staged write or patch operation.

    Attributes:
        success: Whether the write/patch succeeded.
        path: Target file path.
        error: Error message (empty string if success is True).

    """

    success: bool
    path: str
    error: str = ""

    def __bool__(self) -> bool:
        return self.success


# ---------------------------------------------------------------------------
# Lint / validation helpers
# ---------------------------------------------------------------------------


def _lint_python(tmp_path: str) -> str | None:
    """Validate a Python file via py_compile (always available).

    Falls back gracefully — returns the error string on failure, None on success.
    Also tries `ruff check` if available for stricter linting.
    """
    # Phase 1: syntax check (mandatory — catches SyntaxError)
    try:
        py_compile.compile(tmp_path, doraise=True)
    except py_compile.PyCompileError as exc:
        return f"SyntaxError: {exc}"

    # Phase 2: ruff (optional — catches import errors, unused vars, etc.)
    try:
        import subprocess

        result = subprocess.run(
            # resolve_executable raises FileNotFoundError when the tool is
            # absent — the same type the `except FileNotFoundError` below
            # already treats as "not installed, that's fine".
            [resolve_executable("ruff"), "check", "--select", "E,F", "--no-fix", tmp_path],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            output = (result.stdout + result.stderr).strip()
            return f"ruff: {output}" if output else "ruff: unknown lint error"
    except FileNotFoundError as exc:
        logger.warning(
            "ruff is not available (%s); the staged file passed py_compile only, so "
            "import and unused-name errors were not checked before the write.",
            exc,
        )
    except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.debug("ruff check failed (non-fatal): %s", exc)

    return None  # All checks passed


def _lint_yaml(tmp_path: str) -> str | None:
    """Validate a YAML file by attempting to parse it.

    Uses yaml.safe_load. Also tries `yamllint` if installed.
    """
    try:
        import yaml as _yaml  # type: ignore[import-untyped]
    except ImportError:
        logger.debug("PyYAML not installed — skipping YAML validation")
        return None

    try:
        with open(tmp_path, encoding="utf-8") as f:
            _yaml.safe_load(f)
    except _yaml.YAMLError as exc:
        return f"YAML parse error: {exc}"

    # Optional stricter lint
    try:
        import subprocess

        result = subprocess.run(
            [resolve_executable("yamllint"), "-d", "relaxed", tmp_path],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            output = (result.stdout + result.stderr).strip()
            return f"yamllint: {output}" if output else "yamllint: unknown lint error"
    except FileNotFoundError as exc:
        logger.warning(
            "yamllint is not available (%s); the staged file was parsed by yaml.safe_load "
            "only, so style and duplicate-key problems were not checked.",
            exc,
        )
    except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.debug("yamllint check failed (non-fatal): %s", exc)

    return None


def _lint_toml(tmp_path: str) -> str | None:
    """Validate a TOML file by attempting to parse it."""
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            logger.debug("No TOML parser available — skipping TOML validation")
            return None

    try:
        with open(tmp_path, "rb") as f:
            tomllib.load(f)
    except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
        return f"TOML parse error: {exc}"

    return None


_LINTERS: dict[str, Callable[[str], str | None]] = {
    ".py": _lint_python,
    ".yaml": _lint_yaml,
    ".yml": _lint_yaml,
    ".toml": _lint_toml,
}


def _validate_tmp(tmp_path: str, suffix: str) -> str | None:
    """Run the appropriate linter for the given file suffix.

    Returns None on success, or an error string on failure.
    """
    linter = _LINTERS.get(suffix)
    if linter is None:
        return None  # No linter for this file type — pass through
    return linter(tmp_path)


# ---------------------------------------------------------------------------
# Staged write (lint-before-write)
# ---------------------------------------------------------------------------


def staged_write(target: str | Path, content: str, *, encoding: str = "utf-8") -> WriteResult:
    """Write *content* to *target* with staged lint validation.

    1. Write content to a temporary file in the same directory as *target*.
    2. Run the appropriate linter against the temp file.
    3. On lint failure → delete temp, return WriteResult(success=False, error=...).
    4. On lint success → atomically replace *target* via os.replace().

    Args:
        target: Destination file path.
        content: Full file content to write.
        encoding: Text encoding (default utf-8).

    Returns:
        WriteResult indicating success or failure with error details.

    """
    target = Path(target).resolve()
    suffix = target.suffix.lower()

    # Ensure parent directory exists
    target.parent.mkdir(parents=True, exist_ok=True)

    # Step 1: write to temp file in the same directory (same filesystem → atomic rename)
    fd, tmp_path = tempfile.mkstemp(
        suffix=suffix,
        prefix=".staged_",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())

        # Step 2: lint
        error = _validate_tmp(tmp_path, suffix)
        if error:
            logger.warning("[StagedWrite] Lint FAILED for %s: %s", target.name, error)
            return WriteResult(success=False, path=str(target), error=error)

        # Step 3: atomic replace
        os.replace(tmp_path, str(target))
        logger.info("[StagedWrite] Wrote %s (%d bytes)", target.name, len(content))
        return WriteResult(success=True, path=str(target))

    except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.error("[StagedWrite] Unexpected error writing %s: %s", target, exc)
        return WriteResult(success=False, path=str(target), error=str(exc))
    finally:
        # Clean up temp file if it still exists (lint failure or exception path)
        if os.path.exists(tmp_path):
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Deterministic in-memory patching (replaces `patch` CLI)
# ---------------------------------------------------------------------------


def apply_patch(
    target: str | Path,
    old_lines: Sequence[str],
    new_lines: Sequence[str],
    *,
    encoding: str = "utf-8",
    create_backup: bool = False,
) -> WriteResult:
    """Apply a deterministic in-memory diff to *target* and write via staged validation.

    Instead of shelling out to `patch` (which is fragile due to hunk-matching),
    this function:
    1. Reads the current file content.
    2. Verifies *old_lines* match a contiguous block in the file (exact match).
    3. Replaces that block with *new_lines* in memory.
    4. Writes the result through staged_write() for lint validation + atomic replace.

    Args:
        target: File to patch.
        old_lines: Exact lines to find (must appear contiguously in the file).
        new_lines: Replacement lines.
        encoding: Text encoding.
        create_backup: If True, copy original to target.bak before replacing.

    Returns:
        WriteResult indicating success or failure with error details.

    """
    target = Path(target).resolve()

    # Read current content
    if not target.exists():
        return WriteResult(
            success=False,
            path=str(target),
            error=f"Target file does not exist: {target}",
        )

    current_content = target.read_text(encoding=encoding)
    current_lines = current_content.splitlines(keepends=True)

    # Normalize old_lines to have line endings for matching
    normalized_old = _normalize_lines(old_lines)

    # Find the exact contiguous block
    match_start = _find_contiguous_block(current_lines, normalized_old)
    if match_start is None:
        return WriteResult(
            success=False,
            path=str(target),
            error=(
                f"Patch REJECTED: old_lines block ({len(normalized_old)} lines) "
                f"not found contiguously in {target.name}. "
                f"First old line: {repr(normalized_old[0][:80]) if normalized_old else '(empty)'}"
            ),
        )

    # Build patched content in memory
    normalized_new = _normalize_lines(new_lines)
    patched_lines = (
        current_lines[:match_start]
        + normalized_new
        + current_lines[match_start + len(normalized_old) :]
    )
    patched_content = "".join(patched_lines)

    # Optional backup
    if create_backup:
        backup_path = target.with_suffix(target.suffix + ".bak")
        shutil.copy2(str(target), str(backup_path))
        logger.info("[Patch] Backup created: %s", backup_path.name)

    # Write through staged validation
    result = staged_write(target, patched_content, encoding=encoding)
    if result.success:
        logger.info(
            "[Patch] Applied patch to %s: replaced %d lines with %d lines",
            target.name,
            len(normalized_old),
            len(normalized_new),
        )
    return result


def apply_full_diff(
    target: str | Path,
    new_content: str,
    *,
    encoding: str = "utf-8",
    create_backup: bool = False,
) -> WriteResult:
    """Replace file content entirely with deterministic validation.

    This is the recommended method for agent-driven file updates where the
    agent produces the complete new file content. Avoids all hunk-matching
    complexity.

    Args:
        target: File to update.
        new_content: Complete new file content.
        encoding: Text encoding.
        create_backup: If True, copy original to target.bak before replacing.

    Returns:
        WriteResult indicating success or failure.

    """
    target = Path(target).resolve()

    if create_backup and target.exists():
        backup_path = target.with_suffix(target.suffix + ".bak")
        shutil.copy2(str(target), str(backup_path))
        logger.info("[FullDiff] Backup created: %s", backup_path.name)

    return staged_write(target, new_content, encoding=encoding)


def preview_diff(
    target: str | Path,
    new_content: str,
    *,
    encoding: str = "utf-8",
    context_lines: int = 3,
) -> str:
    """Generate a unified diff preview without modifying the file.

    Useful for agent introspection — the agent can review the diff before
    committing to the write.

    Returns:
        Unified diff string.

    """
    target = Path(target).resolve()

    old_content = target.read_text(encoding=encoding) if target.exists() else ""

    diff = unified_diff(
        old_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"a/{target.name}",
        tofile=f"b/{target.name}",
        n=context_lines,
    )
    return "".join(diff)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalize_lines(lines: Sequence[str]) -> list[str]:
    """Ensure every line has a trailing newline for consistent matching."""
    result = []
    for line in lines:
        if not line.endswith("\n"):
            result.append(line + "\n")
        else:
            result.append(line)
    return result


def _find_contiguous_block(haystack: list[str], needle: list[str]) -> int | None:
    """Find the starting index of *needle* as a contiguous sub-sequence in *haystack*.

    Uses stripped comparison for robustness against trailing whitespace differences,
    but requires exact ordering.

    Returns:
        Starting index or None if not found.

    """
    if not needle:
        return None

    needle_len = len(needle)
    haystack_len = len(haystack)

    if needle_len > haystack_len:
        return None

    # First pass: exact match (preferred)
    for i in range(haystack_len - needle_len + 1):
        if haystack[i : i + needle_len] == needle:
            return i

    # Second pass: stripped match (tolerates trailing whitespace)
    stripped_needle = [line.rstrip() for line in needle]
    for i in range(haystack_len - needle_len + 1):
        stripped_block = [line.rstrip() for line in haystack[i : i + needle_len]]
        if stripped_block == stripped_needle:
            return i

    return None
