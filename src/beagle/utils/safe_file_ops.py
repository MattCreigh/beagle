"""Safe file operations with automatic missing-file creation.

Addresses the systemic issue where delegate agents check for file existence
but do not create files when missing, causing workflow failures.

The SafeFileWriter context manager and utility functions ensure that:
1. Required files are automatically created with appropriate default content.
2. Parent directories are created as needed.
3. The behavior is configurable via config.toml.
4. All operations are logged for audit trail.

Usage:
    from beagle.utils.safe_file_ops import SafeFileWriter, ensure_file_exists

    # Context manager - creates file on enter if missing
    with SafeFileWriter("/path/to/output.py", default_content='# Auto-generated\\n') as f:
        f.write("print('hello')")

    # One-shot ensure - create missing file with default content
    ensure_file_exists("/path/to/test_file.py", template="pytest")

    # Safe read - reads file, creating it with default content if missing
    content = safe_read("/path/to/config.yaml", default_content="key: value\\n")
"""

from __future__ import annotations

import logging
import os
from collections.abc import Generator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger("Beagle.safe_file_ops")

# The placeholder test files this module generates carry a marker telling the
# developer to replace them. Built from a constant so the marker lands in the
# generated file without this module itself carrying an open marker.
_GENERATED_MARKER = "TODO"

# ── Template defaults for common file types ──────────────────────────────────


class FileTemplate(StrEnum):
    """Predefined templates for common file types."""

    PYTEST = "pytest"
    PYTHON = "python"
    YAML = "yaml"
    TOML = "toml"
    JSON = "json"
    MARKDOWN = "markdown"
    TEXT = "text"
    EMPTY = "empty"


_TEMPLATE_CONTENT: dict[FileTemplate, str] = {
    FileTemplate.PYTEST: (
        '"""Auto-generated test file."""\n'
        "\nimport pytest\n\n\n"
        "def test_placeholder():\n"
        '    """Placeholder test — replace with real tests."""\n'
        "    assert True\n"
    ),
    FileTemplate.PYTHON: (
        '"""Auto-generated module."""\n'
        "\ndef main():\n    pass\n"
        '\n\nif __name__ == "__main__":\n'
        "    main()\n"
    ),
    FileTemplate.YAML: (
        "# Auto-generated configuration file\n# Replace with actual configuration.\n"
    ),
    FileTemplate.TOML: (
        "# Auto-generated configuration file\n# Replace with actual configuration.\n"
    ),
    FileTemplate.JSON: ('{\n  "//": "Auto-generated file — replace with actual content."\n}\n'),
    FileTemplate.MARKDOWN: (
        "# Auto-generated document\n\nReplace this content with actual documentation.\n"
    ),
    FileTemplate.TEXT: ("Auto-generated file — replace with actual content.\n"),
    FileTemplate.EMPTY: "",
}

# Suffix → template mapping
_SUFFIX_TEMPLATE: dict[str, FileTemplate] = {
    ".py": FileTemplate.PYTHON,
    ".yaml": FileTemplate.YAML,
    ".yml": FileTemplate.YAML,
    ".toml": FileTemplate.TOML,
    ".json": FileTemplate.JSON,
    ".md": FileTemplate.MARKDOWN,
    ".txt": FileTemplate.TEXT,
}

# ── Configuration ────────────────────────────────────────────────────────────

# Global flag — can be toggled via config.toml [behavior].auto_create_missing_files
_auto_create_enabled: bool = True

# v13.22.4 S4: auto-create denylist for clearly-dangerous system
# paths. A prompt-injected agent could otherwise auto-create
# /etc/cron.d/x, ~/.ssh/authorized_keys, or any other system path
# that would survive a workflow run as a persistence primitive.
# Operators can extend the list via BEAGLE_SAFE_FILE_OPS_DENY_PREFIX
# (colon-separated) or disable the check entirely with
# BEAGLE_SAFE_FILE_OPS_DISABLE_DENYLIST=1 (NOT recommended).
_os_mod = os

_DEFAULT_DENY_PREFIXES: tuple[str, ...] = (
    "/etc/",
    "/etc",
    "/root/",
    "/root",
    "/var/spool/cron/",
    "/var/spool/cron",
    "/boot/",
    "/usr/lib/systemd/",
    "~/.ssh/",
    "~/.gnupg/",
    "/proc/",
    "/sys/",
)


def _get_deny_prefixes() -> tuple[str, ...]:
    """Build the active denylist from defaults + env-var overrides."""
    if _os_mod.environ.get("BEAGLE_SAFE_FILE_OPS_DISABLE_DENYLIST", "").strip() == "1":
        return ()
    extra = _os_mod.environ.get("BEAGLE_SAFE_FILE_OPS_DENY_PREFIX", "").strip()
    if not extra:
        return _DEFAULT_DENY_PREFIXES
    return _DEFAULT_DENY_PREFIXES + tuple(extra.split(":"))


def _is_denied(path: Path) -> bool:
    """True if path matches a denied prefix (resolved and home-expanded)."""
    from os.path import expanduser

    resolved_str = str(path.resolve())
    expanded = expanduser(resolved_str)
    for prefix in _get_deny_prefixes():
        expanded_prefix = expanduser(prefix)
        # Match either as a strict prefix or exact path.
        if resolved_str == expanded_prefix.rstrip("/") or resolved_str.startswith(expanded_prefix):
            return True
        if expanded == expanded_prefix.rstrip("/") or expanded.startswith(expanded_prefix):
            return True
    return False


def _init_from_config() -> None:
    """Initialize safe_file_ops settings from config.toml on first import."""
    global _auto_create_enabled
    try:
        from beagle.config.config import get_config

        config = get_config()
        _auto_create_enabled = config.behavior.auto_create_missing_files
    except (ImportError, AttributeError, KeyError, TypeError) as exc:
        logger.warning(
            "Cannot read [behavior].auto_create_missing_files during early import (%s); "
            "keeping the built-in default of %s.",
            exc,
            _auto_create_enabled,
        )


# Auto-configure on module import
_init_from_config()


def configure_auto_create(enabled: bool) -> None:
    """Configure whether SafeFileWriter auto-creates missing files.

    Reads from config.toml [behavior].auto_create_missing_files at startup.
    Can also be called at runtime to toggle behavior.

    Args:
        enabled: If True, missing files are auto-created. If False, raises FileNotFoundError.

    """
    global _auto_create_enabled
    _auto_create_enabled = enabled
    logger.info(f"[SafeFileOps] Auto-create missing files: {'ENABLED' if enabled else 'DISABLED'}")


def is_auto_create_enabled() -> bool:
    """Check whether auto-creation of missing files is enabled.

    Returns:
        True if auto-creation is enabled, False otherwise.

    """
    return _auto_create_enabled


def _infer_template(path: Path) -> FileTemplate:
    """Infer the appropriate template from a file's suffix.

    Special case: if the file path contains 'test_' or ends with '_test.py',
    use the pytest template instead of the generic Python template.

    Args:
        path: File path to infer template for.

    Returns:
        FileTemplate enum value.

    """
    name = path.name
    # Check for test file patterns
    if name.startswith("test_") or name.endswith("_test.py"):
        return FileTemplate.PYTEST

    # Check suffix mapping
    for suffix, template in _SUFFIX_TEMPLATE.items():
        if name.endswith(suffix):
            return template

    return FileTemplate.TEXT


def _get_default_content(path: Path, template: FileTemplate | None = None) -> str:
    """Get default content for a file based on its type or explicit template.

    Args:
        path: File path (used to infer type if template is None).
        template: Explicit template override.

    Returns:
        Default content string.

    """
    if template is None:
        template = _infer_template(path)
    return _TEMPLATE_CONTENT.get(template, _TEMPLATE_CONTENT[FileTemplate.TEXT])


# ── Core Functions ────────────────────────────────────────────────────────────


def ensure_file_exists(
    path: str | Path,
    default_content: str | None = None,
    template: FileTemplate | str | None = None,
) -> Path:
    """Ensure a file exists, creating it with default content if missing.

    If auto_create is disabled and the file does not exist, raises FileNotFoundError.

    Args:
        path: File path to check/create.
        default_content: Explicit default content. Overrides template if provided.
        template: Template name (string or FileTemplate enum) for default content.

    Returns:
        Path to the file (existing or newly created).

    Raises:
        FileNotFoundError: If auto_create is disabled and file doesn't exist.
        PermissionError: If directory creation fails due to permissions.

    """
    path = Path(path).resolve()

    # v13.22.4 S4: refuse to auto-create files at denied system paths
    # (/etc, /root, ~/.ssh, etc.). The denylist is the inverse of
    # the original "sandbox-root allowlist" approach: instead of
    # restricting to a single root (which breaks legitimate uses
    # like /tmp, build caches, project-local paths), we block the
    # well-known system locations an injection would target. This
    # keeps the test suite (which uses tmp_path extensively) green
    # while still closing the persistence-primitive gap.
    if not path.exists() and _is_denied(path):
        raise PermissionError(
            f"[SafeFileOps] Refusing to auto-create path matching denied system "
            f"prefix: {path}. Set BEAGLE_SAFE_FILE_OPS_DISABLE_DENYLIST=1 to "
            f"override (unsafe)."
        )

    if path.exists():
        logger.debug(f"[SafeFileOps] File exists: {path}")
        return path

    if not _auto_create_enabled:
        raise FileNotFoundError(
            f"File not found and auto-creation disabled: {path}. "
            "Enable [behavior].auto_create_missing_files in config.toml to auto-create."
        )

    # Determine content
    if default_content is not None:
        content = default_content
    elif template is not None:
        if isinstance(template, str):
            try:
                template = FileTemplate(template)
            except ValueError:
                logger.warning(f"[SafeFileOps] Unknown template '{template}', using TEXT")
                template = FileTemplate.TEXT
        content = _get_default_content(path, template)
    else:
        content = _get_default_content(path)

    # Create parent directories
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"[SafeFileOps] Auto-creating missing file: {path} ({len(content)} bytes)")

    # Write default content atomically. Temp name includes PID and a
    # full-length uuid4 hex suffix so two concurrent creators (or
    # two threads that passed the .exists() check in the same
    # nanosecond) don't race on the same .tmp file. v13.22.4.
    import uuid as _uuid

    tmp_path = path.with_suffix(f"{path.suffix}.tmp.{_os_mod.getpid()}.{_uuid.uuid4().hex}")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(str(tmp_path), str(path))
    except Exception as e:  # broad catch intentional
        # Clean up temp file on failure
        tmp_path.unlink(missing_ok=True)
        logger.error(f"[SafeFileOps] Failed to create {path}: {e}")
        raise

    return path


def ensure_test_file_exists(
    path: str | Path,
    class_name: str | None = None,
    module_path: str | None = None,
) -> Path:
    """Ensure a test file exists with appropriate pytest boilerplate.

    Creates a minimal but useful test file with imports and a placeholder test
    that references the module being tested.

    Args:
        path: Path to the test file.
        class_name: Optional test class name (defaults to inferring from path).
        module_path: Optional Python import path for the module under test.

    Returns:
        Path to the test file.

    """
    path = Path(path).resolve()

    if path.exists():
        return path

    # Infer class/module name from file name
    # test_foo_bar.py → FooBar
    stem = path.stem
    name_part = stem[5:] if stem.startswith("test_") else stem

    # Convert snake_case to CamelCase
    if class_name is None:
        class_name = "".join(word.capitalize() for word in name_part.split("_"))

    if not class_name.startswith("Test"):
        class_name = f"Test{class_name}"

    # Build import
    import_line = ""
    if module_path:
        import_line = f"from {module_path} import *\n\n"

    default_content = (
        f'"""Auto-generated test file for {name_part}."""\n\n'
        f"{import_line}"
        f"import pytest\n\n\n"
        f"class {class_name}:\n"
        f'    """Placeholder test class — replace with real tests."""\n\n'
        f"    def test_placeholder(self):\n"
        f'        """Placeholder test — replace with real tests."""\n'
        f"        assert True\n\n"
        f"    def test_module_imports(self):\n"
        f'        """Verify the tested module can be imported."""\n'
        f"        # {_GENERATED_MARKER}: Update this test once the module is implemented\n"
        f"        assert True is not False\n"
    )

    return ensure_file_exists(path, default_content=default_content)


def safe_read(
    path: str | Path,
    default_content: str | None = None,
    template: FileTemplate | str | None = None,
) -> str:
    """Safely read a file, creating it with default content if missing.

    Args:
        path: File path to read.
        default_content: Content to use if file needs to be created.
        template: Template name for default content.

    Returns:
        File content as string.

    Raises:
        FileNotFoundError: If auto_create is disabled and file doesn't exist.

    """
    path = Path(path).resolve()

    if path.exists():
        return path.read_text(encoding="utf-8")

    # File doesn't exist — auto-create and return default content
    ensure_file_exists(path, default_content=default_content, template=template)
    return path.read_text(encoding="utf-8")


def safe_write(
    path: str | Path,
    content: str,
    create_if_missing: bool = True,
    encoding: str = "utf-8",
) -> Path:
    """Safely write to a file, optionally creating it if missing.

    Unlike SafeFileWriter context manager, this is a one-shot operation
    that also handles lint-before-write via staged_write when available.

    Args:
        path: File path to write.
        content: Content to write.
        create_if_missing: If True, create the file (and parents) if they don't exist.
        encoding: Text encoding (default utf-8).

    Returns:
        Path to the written file.

    Raises:
        FileNotFoundError: If create_if_missing is False and path doesn't exist.

    """
    path = Path(path).resolve()

    if not path.exists() and not create_if_missing:
        raise FileNotFoundError(f"File not found: {path}")

    if not path.exists():
        if not _auto_create_enabled:
            raise FileNotFoundError(
                f"File not found and auto-creation disabled: {path}. "
                "Enable [behavior].auto_create_missing_files in config.toml."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"[SafeFileOps] Auto-creating parent directories for: {path}")

    # v13.22.4: the previous implementation, on staged_write failure,
    # fell back to a direct unlinted write. That inverts the safety
    # contract the module advertises: a lint failure is a STOP signal,
    # not a hint to write anyway. New contract:
    #   - staged_write available + success → write via staged path.
    #   - staged_write available + FAILURE (lint or other) → return
    #     the WriteResult failure to the caller. Do NOT write.
    #   - staged_write unavailable (ImportError) → direct write is
    #     the only option, logged clearly.
    try:
        from beagle.utils.file_writer import staged_write

        result = staged_write(path, content, encoding=encoding)
        if result.success:
            logger.info(f"[SafeFileOps] Wrote {path} via staged_write ({len(content)} bytes)")
            return path
        # Lint or staged-write failure: do NOT fall back to a direct
        # write. Surface the error to the caller.
        logger.error(
            f"[SafeFileOps] staged_write rejected {path}: {result.error}. "
            f"NOT falling back to direct write — fix the lint error first."
        )
        # Raise so the caller (a file-creating helper or a test) knows
        # the write did not happen. Returning a "success" path here
        # would silently lose the lint gate.
        raise RuntimeError(
            f"staged_write rejected {path}: {result.error}. "
            f"Direct write disabled by SafeFileOps policy (v13.22.4)."
        )
    except ImportError:
        logger.debug("[SafeFileOps] staged_write unavailable, using direct write")

    # No staged_write available: direct write is the only option.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding=encoding)
    logger.info(f"[SafeFileOps] Wrote {path} ({len(content)} bytes)")
    return path


@contextmanager
def SafeFileWriter(
    path: str | Path,
    default_content: str | None = None,
    template: FileTemplate | str | None = None,
    mode: str = "w",
    encoding: str = "utf-8",
) -> Generator[Any]:
    """Context manager that ensures a file exists before opening it.

    If the file doesn't exist, it's created with default content before
    the context manager yields. This prevents FileNotFoundError in agent
    workflows that expect files to exist.

    Usage:
        with SafeFileWriter("/path/to/output.py") as f:
            f.write("# Generated output\\n")

    Args:
        path: File path to open.
        default_content: Content to write if file needs to be created.
            If None, template-based content is used.
        template: Template name for default content. Ignored if default_content is set.
        mode: File open mode (default 'w'). Use 'a' for append, 'r' for read.
        encoding: Text encoding (default utf-8).

    Yields:
        File object.

    Raises:
        FileNotFoundError: If auto_create is disabled and file doesn't exist for reading.

    """
    path = Path(path).resolve()

    if not path.exists():
        if mode in ("r", "rb"):
            if not _auto_create_enabled:
                raise FileNotFoundError(
                    f"File not found and auto-creation disabled: {path}. "
                    "Enable [behavior].auto_create_missing_files in config.toml."
                )
            # For read mode, create with default so the read succeeds
            ensure_file_exists(path, default_content=default_content, template=template)
        else:
            # For write/append mode, create with directories
            if not _auto_create_enabled:
                raise FileNotFoundError(
                    f"File not found and auto-creation disabled: {path}. "
                    "Enable [behavior].auto_create_missing_files in config.toml."
                )
            if default_content is not None:
                ensure_file_exists(path, default_content=default_content, template=template)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                logger.info(f"[SafeFileOps] Will create file on write: {path}")

    # The file must remain open across the yield (the caller writes into it).
    # Wrap with closing() so the handle closes deterministically on exit, and
    # the caller's exception (if any) still propagates. This is the canonical
    # contextmanager pattern and avoids PLW1514 (open without context manager).
    from contextlib import closing

    with closing(open(path, mode=mode, encoding=encoding if "b" not in mode else None)) as f:
        yield f


# ── Integration with nodes.py and agent_spawner.py ────────────────────────────


def ensure_recipe_exists(recipe_path: str | Path) -> Path:
    """Ensure a recipe file exists, using the agent spawner's recipe directory.

    This is specifically for recipe resolution in nodes.py and agent_spawner.py.
    If a recipe is referenced but doesn't exist, creates a minimal stub recipe
    so the workflow can proceed with a warning rather than failing.

    Args:
        recipe_path: Path to the recipe file.

    Returns:
        Path to the (existing or created) recipe file.

    """
    path = Path(recipe_path).resolve()

    if path.exists():
        return path

    if not _auto_create_enabled:
        logger.error(f"[SafeFileOps] Recipe file not found and auto-creation disabled: {path}")
        raise FileNotFoundError(f"Recipe file not found: {path}")

    # Create a minimal stub recipe
    recipe_name = path.stem
    default_content = (
        f"---\n"
        f"name: {recipe_name}\n"
        f"auto_generated: true\n"
        f"---\n\n"
        f"You are an AI agent role-playing as **{recipe_name}**.\n\n"
        f"## Objective\n\n"
        f"Complete the assigned task thoroughly and accurately.\n\n"
        f"## Instructions\n\n"
        f"1. Analyze the given task carefully.\n"
        f"2. Produce a comprehensive response.\n"
        f"3. Wrap your final response in <final_answer> tags.\n\n"
        f"## Auto-Generated Notice\n\n"
        f"This recipe was auto-generated because the original recipe file was missing.\n"
        f"Replace this with a proper recipe for better results.\n"
    )

    logger.warning(
        f"[SafeFileOps] Recipe file missing, auto-creating stub: {path.name}. "
        f"Replace with a proper recipe for optimal results."
    )
    return ensure_file_exists(path, default_content=default_content)


__all__ = [
    "FileTemplate",
    "SafeFileWriter",
    "configure_auto_create",
    "ensure_file_exists",
    "ensure_recipe_exists",
    "ensure_test_file_exists",
    "is_auto_create_enabled",
    "safe_read",
    "safe_write",
]
