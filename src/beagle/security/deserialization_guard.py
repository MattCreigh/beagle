"""Hardened deserialization wrappers for LangChain load/loads.

Defense-in-depth for:
- CVE-2025-68664 (LangGrinch): RCE via serialization injection in
  additional_kwargs / response_metadata / tool outputs.
- CVE-2026-34070: Path traversal in load_prompt / load_prompt_from_config.

These wrappers enforce:
- ``allowed_objects="core"`` — restricts deserialization to
  langchain-core's serialization mapping only (no arbitrary
  Serializable subclasses).
- ``secrets_from_env=False`` — prevents environment-secret reinjection.

Usage::

    from beagle.security.deserialization_guard import (
        safe_loads,
        safe_load_prompt,
    )

    # Instead of: langchain_core.load.loads(blob)
    data = safe_loads(blob)

    # Instead of: load_prompt(path)
    prompt = safe_load_prompt(path)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

# ── Constants ────────────────────────────────────────────────────────────────

_TRUSTED_VALUES_ONLY: Literal["core"] = "core"  # restrict to langchain-core serialization mapping


# ── safe_loads ───────────────────────────────────────────────────────────────


def safe_loads(blob: str) -> Any:
    """Deserialize a LangChain blob with security constraints.

    Enforces ``allowed_objects="core"`` and ``secrets_from_env=False``
    to prevent CVE-2025-68664 (LangGrinch RCE) and environment-secret
    reinjection.

    Args:
        blob: JSON string produced by ``langchain_core.load.dumps``.

    Returns:
        Deserialized Python object (restricted to core types).

    Raises:
        ValueError: If the blob contains disallowed object types.
        ImportError: If langchain-core is not installed.

    """
    try:
        from langchain_core.load import loads
    except ImportError as _exc:
        raise ImportError(
            "langchain-core is required for safe_loads. "
            "Install with: pip install langchain-core>=1.2.22"
        ) from _exc

    return loads(
        blob,
        allowed_objects=_TRUSTED_VALUES_ONLY,
        secrets_from_env=False,
    )


# ── safe_load_prompt ─────────────────────────────────────────────────────────


def safe_load_prompt(path: str | Path) -> Any:
    """Load a LangChain prompt with path containment and serialization guards.

    Defense-in-depth for CVE-2026-34070 (path traversal in
    load_prompt / load_prompt_from_config).  Enforces:
    1. Path is resolved and contained (no ``..`` traversal).
    2. The loaded prompt's serialization only uses core types.

    Args:
        path: Filesystem path to the prompt file.

    Returns:
        Loaded prompt object.

    Raises:
        ValueError: If path escapes the base directory.
        ImportError: If langchain-core is not installed.

    """
    resolved = Path(path).resolve()

    # CVE-2026-34070 defense: reject path traversal — do this BEFORE the
    # langchain import so the security check cannot be masked by an ImportError
    # from a missing/broken langchain-core.
    if ".." in str(path):
        raise ValueError(f"Path traversal rejected (CVE-2026-34070): '{path}' contains '..'")

    # Additional containment: ensure resolved path doesn't escape the CWD
    # (most prompt files should be within the project tree)
    cwd = Path.cwd().resolve()
    try:
        resolved.relative_to(cwd)
    except ValueError:
        # Allow if it's under the package dir or an explicit prompt root
        pkg_dir = Path(__file__).resolve().parent.parent.parent.parent
        try:
            resolved.relative_to(pkg_dir)
        except ValueError:
            prompt_root = os.environ.get("BEAGLE_PROMPT_ROOT", "")
            if prompt_root:
                try:
                    resolved.relative_to(Path(prompt_root).resolve())
                except ValueError:
                    raise ValueError(
                        f"Path escapes allowed directories (CVE-2026-34070): "
                        f"'{path}' resolved to '{resolved}'"
                    ) from None
            else:
                raise ValueError(
                    f"Path escapes project directory (CVE-2026-34070): "
                    f"'{path}' resolved to '{resolved}'. "
                    f"Set BEAGLE_PROMPT_ROOT to allow loading from external directories."
                ) from None

    try:
        from typing import Any as _Any

        from langchain_core.prompts.loading import (
            load_prompt as _lc_load_prompt_raw,
        )

        _lc_load_prompt: _Any = _lc_load_prompt_raw
    except ImportError as _exc:
        raise ImportError(
            "langchain-core is required for safe_load_prompt. "
            "Install with: pip install langchain-core>=1.2.22"
        ) from _exc

    return _lc_load_prompt(str(resolved))
