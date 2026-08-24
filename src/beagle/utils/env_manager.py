"""Environment and path utilities for Goose subprocess management.

Migrated from autonomous_orchestrator.py to reduce that module's size
and enforce strict separation between orchestration logic and subprocess
environment handling.
"""

from __future__ import annotations

__all__ = [
    "_build_safe_env",
    "build_goose_env",
    "get_output_dir",
    "get_recipes_dir",
    "get_workspace_root",
]

import os
from pathlib import Path
from typing import Final

# ── Environment allowlist ─────────────────────────────────────────────────────

# Essential system env vars that subprocesses always need
_ESSENTIAL_ENV_KEYS: Final[frozenset[str]] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "LC_ALL",
        "TERM",
    }
)

# Allowlisted Beagle/API keys — these are explicitly passed to Goose subprocesses
_ENV_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        # Cloud API keys
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OLLAMA_CLOUD_API_KEY",
        # Goose runtime config
        "GOOSE_BIN",
        "GOOSE_MODEL",
        "GOOSE_PROVIDER",
        "GOOSE_HOST",
        "GOOSE_DISABLE_KEYRING",
        "GOOSE_TELEMETRY_ENABLED",
        # Beagle config
        "WORKSPACE_ROOT",
        "BEAGLE_BUDGET_USD",
        "BEAGLE_CACHE_ENABLED",
        "BEAGLE_LOG_LEVEL",
        "BEAGLE_LOG_JSON",
        "BEAGLE_KNOWLEDGE_DIR",
        "BEAGLE_MCP_TRANSPORT",
        # Filesystem enforcement
        "BEAGLE_READONLY_MODE",
    }
)


def _build_safe_env(readonly: bool = False) -> dict[str, str]:
    """Build a sanitized environment for Goose subprocesses.

    Only passes essential system vars + explicitly allowlisted keys.
    Prevents leaking the full host environment to child processes.

    Returns:
        A sanitized dict of env vars.

    """
    safe: dict[str, str] = {}

    # Essential system vars
    for key in _ESSENTIAL_ENV_KEYS:
        if key in os.environ:
            safe[key] = os.environ[key]

    # Ensure ~/.local/bin is in PATH
    local_bin = os.path.expanduser("~/.local/bin")
    if "PATH" in safe:
        if local_bin not in safe["PATH"]:
            safe["PATH"] = f"{local_bin}:{safe['PATH']}"
    else:
        safe["PATH"] = f"{local_bin}:/usr/local/bin:/usr/bin:/bin"

    # Allowlisted Beagle/API keys
    for key in _ENV_ALLOWLIST:
        if key in os.environ:
            safe[key] = os.environ[key]

    # Goose requires PAGER=cat for headless mode
    safe["PAGER"] = "cat"

    # Gemini API key alias (some code paths still use GEMINI_API_KEY)
    if "GEMINI_API_KEY" in safe and "GOOGLE_API_KEY" not in safe:
        safe["GOOGLE_API_KEY"] = safe["GEMINI_API_KEY"]

    # Filesystem enforcement: signal read-only mode to subprocesses
    if readonly:
        safe["BEAGLE_READONLY_MODE"] = "1"

    # CRITICAL: Disable goose telemetry and keyring in all subprocesses to prevent
    # consent prompts and keyring access failures in headless mode
    safe["GOOSE_DISABLE_KEYRING"] = "true"
    safe["GOOSE_TELEMETRY_ENABLED"] = "false"

    return safe


build_safe_env = _build_safe_env


def build_goose_env(
    model: str,
    *,
    workspace: Path | None = None,
    knowledge_dir: Path | None = None,
) -> dict[str, str]:
    """Build a fresh, deterministic environment for a Goose subprocess.

    Unlike :func:`_build_safe_env` (which sanitises the *host* environment by
    pass-through), this constructs the fixed ten-key launcher environment used
    by the OpenClaw/TOML task runners.

    Single definition — before v1.0.0 the same body was copied into
    ``scripts/launch/launch_openclaw_research.py``,
    ``infrastructure/run_toml_tasks.py`` and
    ``infrastructure/mcp_openclaw_server.py`` (F7). The copies had already
    begun to drift (one pinned a retired model literal as its default).
    ``core/sandbox.py`` carries a *different* function of the same name — an
    allowlist-based sandbox env, not a Goose launcher env — and deliberately
    does NOT route through here.

    Args:
        model: Model name for ``GOOSE_MODEL``.
        workspace: Value for ``GOOSE_WORKSPACE``. Defaults to
            :func:`get_workspace_root` (the beagle package directory), which
            is what all three former copies computed by hand.
        knowledge_dir: Value for ``BEAGLE_KNOWLEDGE_DIR``. Defaults to
            ``get_data_root() / "ai" / "instance_rag"``, matching the former
            copies.

    Returns:
        Environment dict with the ten launcher keys, plus
        ``OLLAMA_CLOUD_API_KEY``/``OLLAMA_API_KEY`` when the secrets loader
        yields a key.

    """
    from ..config.paths import get_data_root  # lazy: break import cycle
    from ..secrets_loader import load_api_key

    if workspace is None:
        workspace = get_workspace_root()
    if knowledge_dir is None:
        knowledge_dir = get_data_root() / "ai" / "instance_rag"

    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        "GOOSE_PROVIDER": "ollama_cloud",
        "GOOSE_MODEL": model,
        "GOOSE_DISABLE_KEYRING": "true",
        "GOOSE_TELEMETRY_ENABLED": "false",
        "BEAGLE_KNOWLEDGE_DIR": str(knowledge_dir),
        "RAG_ENABLED": "true",
        "GOOSE_WORKSPACE": str(workspace),
    }
    key = load_api_key()
    if key:
        env["OLLAMA_CLOUD_API_KEY"] = key
        env["OLLAMA_API_KEY"] = key
    return env


def get_recipes_dir() -> Path:
    """Get the recipes directory path."""
    return get_workspace_root() / "recipes"


def get_output_dir() -> Path:
    """Get the output directory for analysis reports.

    <invariant>
    Analysis reports are runtime STATE, so they anchor to data_root, not
    workspace_root. workspace_root points at *assets* and under a wheel
    install resolves into site-packages — reports were being written to
    ``<venv>/lib/python3.13/site-packages/beagle/ai/
    analysis_reports/`` where they are invisible to the user and destroyed
    by the next wheel install. get_data_root() names "analysis reports" in
    its own docstring and honours $BEAGLE_DATA_ROOT. Fixed 2026-07-28.

    v13.22.5: if a canonical, already-existing ``ai/analysis_reports``
    directory is found under the workspace root (e.g. the operator keeps
    reports in ``<project>/ai/analysis_reports``), its parent is used as a
    mirror target so reports are visible in the project tree rather than
    buried under ``~/.beagle``. Primary always auto-creates at data_root.
    </invariant>
    """
    from beagle.config.paths import get_data_root

    output_dir = get_data_root() / "ai" / "analysis_reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def get_output_dir_mirror() -> Path | None:
    """Return a canonical project-tree mirror for analysis reports, or None.

    The operator-facing reports location is ``<workspace_root>/ai/
    analysis_reports``. Under the Beagle source deployment that resolves to
    a workspace-relative ``ai/analysis_reports`` directory so reports are
    visible in the project tree where the workflow ran, not buried under
    ``~/.beagle``. Returns None when the workspace cannot be resolved (e.g.
    read-only wheel install) so callers keep the data_root primary.
    """
    try:
        from beagle.utils.env_manager import get_workspace_root

        # v1.2.0 (RG-6, BGL-009): use the resolved workspace root, not a
        # hardcoded host path. The prior code preferred
        # the repository root when it existed, coupling the
        # module to this host. ``get_workspace_root()`` honours the
        # WORKSPACE_ROOT env var and falls back to the package directory, so
        # the operator can point reports at a project tree explicitly.
        ws = get_workspace_root()
        mirror = Path(ws) / "ai" / "analysis_reports"
        mirror.mkdir(parents=True, exist_ok=True)
        return mirror
    except (ImportError, AttributeError, KeyError, ValueError, OSError):  # catch: NARROWED
        return None


def get_workspace_root() -> Path:
    """Return the workspace root from environment or default.

    The workspace root is the beagle package directory
    containing metaprompts/, recipes/, config.toml, etc. This is the
    single canonical definition — all other modules should import and
    use this function rather than computing their own paths.

    Resolution order:
      1. WORKSPACE_ROOT environment variable (if the dir exists)
      2. Default: the package directory (parent of utils/)
    """
    ws = os.environ.get("WORKSPACE_ROOT", "")
    if ws:
        path = Path(ws)
        if path.is_dir():
            return path
    # Default to the beagle PACKAGE directory — the one
    # that contains metaprompts/, recipes/, config.toml, etc.
    # utils/env_manager.py is at <pkg>/utils/env_manager.py, so parents[1]
    # is the package dir.
    return Path(__file__).resolve().parents[1]


# ── .env loading ───────────────────────────────────────────────────────────────

_ENV_PATH = Path.home() / ".env"


def load_env_file() -> None:
    """Load variables from ~/.env into os.environ if not already set.

    Only sets variables whose keys are in _ENV_ALLOWLIST.
    """
    if not _ENV_PATH.exists():
        return

    for line in _ENV_PATH.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, raw_value = stripped.partition("=")
        key = key.strip()
        value = raw_value.strip().strip("\"'")
        if key in _ENV_ALLOWLIST and key not in os.environ:
            os.environ[key] = value


# Load .env at module import time
load_env_file()
