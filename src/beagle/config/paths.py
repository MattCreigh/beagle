"""Path management for Goose Agentic Workflow FHS-compliant deployment.

Provides standardized paths following XDG Base Directory Specification.

Usage:
    from beagle.config.paths import get_cache_root, get_reports_dir, get_memory_dir
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger("Beagle.config.paths")

# ── Base Directory Discovery ──────────────────────────────────────────────────


def _get_home_cache() -> Path:
    """Get user's cache directory (XDG-compliant)."""
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        return Path(xdg_cache)
    return Path.home() / ".cache"


def _get_home() -> Path:
    """Get user's home directory."""
    return Path.home()


# ── Root Getters ─────────────────────────────────────────────────────────────


def get_workspace_root() -> Path:
    """Return the workspace root from environment or default.

    Canonical definition (SP-7: previously delegated to utils.env_manager,
    which created a config.paths -> utils.env_manager -> config.paths cycle;
    now self-contained so paths is a pure leaf for env_manager to depend on).

    Resolution order:
      1. WORKSPACE_ROOT environment variable (if the dir exists)
      2. Default: the beagle PACKAGE directory — the one
         that contains metaprompts/, recipes/, config.toml, etc.
         paths.py is at <pkg>/config/paths.py, so parents[1] is the pkg dir.
    """
    ws = os.environ.get("WORKSPACE_ROOT", "")
    if ws:
        path = Path(ws)
        if path.is_dir():
            return path
    return Path(__file__).resolve().parents[1]


def get_data_root() -> Path:
    """Get the writable data root for Beagle runtime state.

    Distinct from get_workspace_root(): workspace_root anchors *assets* (recipes,
    metaprompts, config.toml — possibly under a read-only site-packages install),
    while data_root anchors *state* (tracking DBs, memory indices, event logs,
    compressed context folds, analysis reports). State must remain writable even
    when the package is installed read-only.

    Resolution order:
      1. $BEAGLE_DATA_ROOT environment variable (operator override)
      2. config.paths.data_root from config.toml (deployment override)
      3. $XDG_DATA_HOME/beagle/ (XDG Base Directory spec)
      4. ~/.beagle/ (final fallback — matches schema default)
    """
    env = os.environ.get("BEAGLE_DATA_ROOT")
    if env:
        return Path(env)
    # config.paths.data_root from config.toml (deployment override). Read it
    # directly from the TOML via the pure leaf module — importing the full
    # loader from paths would create config.paths -> config.loader ->
    # config.schema -> config.paths cycle.
    try:
        import tomllib

        from ._config_path import find_config_toml

        _cfg_path = find_config_toml()
        if _cfg_path.exists():
            _data = tomllib.loads(_cfg_path.read_text(encoding="utf-8"))
            _paths = _data.get("paths") or {}
            cfg_root = _paths.get("data_root")
            if cfg_root:
                return Path(cfg_root)
    except (ImportError, OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning(
            "Cannot read [paths].data_root from config.toml (%s); falling back to "
            "XDG_DATA_HOME or ~/.beagle.",
            exc,
        )
    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        return Path(xdg_data) / "beagle"
    return _get_home() / ".beagle"


def get_cache_root() -> Path:
    """Get cache root directory.

    Following XDG Base Directory Specification:
    - $XDG_CACHE_HOME/goose/beagle/
    - Or ~/.cache/goose/beagle/
    """
    custom = os.environ.get("GOOSE_CACHE_ROOT")
    if custom:
        return Path(custom)
    return _get_home_cache() / "goose" / "beagle"


def get_log_root() -> Path:
    """Get log root directory."""
    return get_cache_root() / "logs"


# ── Subdirectory Getters ──────────────────────────────────────────────────────


def get_reports_dir() -> Path:
    """Get reports directory for analysis output."""
    return get_cache_root() / "reports"


def get_memory_dir() -> Path:
    """Get memory storage directory."""
    return get_cache_root() / "memory"


def get_checkpoint_dir() -> Path:
    """Get checkpoint storage directory."""
    return get_cache_root() / "checkpoints"


def get_knowledge_dir() -> Path:
    """Get RAG knowledge graph directory."""
    return get_cache_root() / "knowledge_graph"


def get_session_dir() -> Path:
    """Get session storage directory."""
    return get_cache_root() / "sessions"


def get_constraints_dir() -> Path:
    """Get constraint storage directory.

    Anchored to :func:`get_data_root`, not :func:`get_workspace_root`.
    Constraints are runtime *state* that the registry writes to
    (``constraint_registry.py`` mkdirs this path and persists JSON into it),
    so it must obey the state/assets split documented on ``get_data_root``.

    v1.0.2: was ``get_workspace_root() / "constraints"``. workspace_root is
    the *package* directory, so the registry wrote into the install tree —
    ``src/constraints/`` under an editable install, and
    ``site-packages/beagle/constraints/`` under a wheel install. The
    site-packages case was actively harmful: it left a directory behind that
    pip did not own, and a leftover ``site-packages/beagle/`` with no
    ``__init__.py`` shadows the editable-install finder as a namespace
    package, breaking ``from beagle import ...`` entirely. This is the same
    bug ``reproducibility/recorder.py`` fixed for replay manifests; the fix
    is the same resolver.
    """
    return get_data_root() / "constraints"


def get_file_cache_dir() -> Path:
    """Get file cache directory."""
    return get_cache_root() / "cache"


# ── Legacy Path Compatibility ──────────────────────────────────────────────────


def get_legacy_workspace() -> Path:
    """Get the legacy workspace path — now delegates to canonical get_workspace_root()."""
    return get_workspace_root()


# ── Initialization ────────────────────────────────────────────────────────────


def ensure_cache_dirs() -> None:
    """Ensure all cache directories exist.

    Call this during application initialization.
    """
    dirs = [
        get_cache_root(),
        get_reports_dir(),
        get_memory_dir(),
        get_checkpoint_dir(),
        get_knowledge_dir(),
        get_session_dir(),
        get_file_cache_dir(),
        get_log_root(),
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def get_all_paths() -> dict[str, Path]:
    """Get all standard paths.

    Returns:
        Dictionary of path name -> Path

    """
    return {
        "workspace_root": get_workspace_root(),
        "cache_root": get_cache_root(),
        "log_root": get_log_root(),
        "reports_dir": get_reports_dir(),
        "memory_dir": get_memory_dir(),
        "checkpoint_dir": get_checkpoint_dir(),
        "knowledge_dir": get_knowledge_dir(),
        "session_dir": get_session_dir(),
        "file_cache_dir": get_file_cache_dir(),
    }


def resolve_goose_bin() -> str:
    """Resolve the goose binary path.

    The order is:
        1. ``GOOSE_BIN`` env var (always wins — same as the
           ``GooseConfig.binary_path`` schema default).
        2. ``shutil.which("goose")`` — finds the binary on PATH
           (works for a user-local ``~/.local/bin/goose``
           install, and for the ``goose`` shim that the
           package's own ``bin/`` directory ships).
        3. ``shutil.which("goose.orig")`` — backwards-compat fallback
           for installs that symlinked the binary under the older
           name.
        4. The literal ``~/.local/bin/goose`` path as a last-ditch
           fallback (the historical hardcoded default).

    Returns:
        Resolved path to the goose binary, or the empty string if
        nothing was found. Callers should handle the empty case
        explicitly rather than silently spawning an empty argv[0].

    v13.22.3: Centralised here after the v13.22.2 schema default
    (``~/.local/bin/goose.orig``) was discovered to never resolve
    on a clean install where the binary is named ``goose`` (no
    suffix). 10 callers in the codebase used the wrong literal;
    this helper is now the single source of truth.

    """
    env_override = os.environ.get("GOOSE_BIN")
    if env_override:
        return env_override
    found = shutil.which("goose")
    if found:
        return found
    found = shutil.which("goose.orig")
    if found:
        return found
    return str(Path.home() / ".local/bin/goose")


# ── External executable resolution ────────────────────────────────────────────

# <invariant>
# Every subprocess call in this package passes an absolute argv[0] resolved
# through resolve_executable(). Passing a bare name ("git", "ruff") defers the
# lookup to the OS, which searches PATH at exec time — so which binary runs
# depends on the environment the process inherited, and a PATH entry an
# attacker can write to becomes an execution primitive. Resolving once here
# fixes the binary for the life of the process and turns a missing tool into a
# named error instead of an obscure failure inside the child.
# </invariant>

# An operator can pin any tool explicitly. The variable name is the uppercased
# executable name with a _BIN suffix, matching the existing GOOSE_BIN contract.
_EXECUTABLE_ENV_SUFFIX = "_BIN"

_resolved_executables: dict[str, str] = {}


def resolve_executable(name: str) -> str:
    """Resolve an executable name to an absolute path.

    The order is:
        1. ``<NAME>_BIN`` env var (always wins), e.g. ``GIT_BIN`` for ``git``.
        2. ``shutil.which(name)`` — the first match on the current PATH.

    The result is cached, so the lookup happens once per process and every
    later call returns the same absolute path even if PATH changes underneath.
    Call :func:`reset_executable_cache` in a test that manipulates PATH.

    Args:
        name: Bare executable name, e.g. ``"git"``.

    Returns:
        Absolute path to the executable.

    Raises:
        FileNotFoundError: The executable is not on PATH and no env override
            is set. This is the same exception type that ``subprocess`` raises
            for a missing binary, so existing handlers keep working.

    """
    cached = _resolved_executables.get(name)
    if cached is not None:
        return cached

    override = os.environ.get(f"{name.upper().replace('-', '_')}{_EXECUTABLE_ENV_SUFFIX}")
    resolved = override or shutil.which(name)
    if not resolved:
        raise FileNotFoundError(
            f"Required executable {name!r} was not found on PATH. "
            f"Install it, or set {name.upper()}{_EXECUTABLE_ENV_SUFFIX} to its absolute path."
        )

    _resolved_executables[name] = resolved
    return resolved


def get_runtime_dir() -> Path:
    """Return a private per-user runtime directory, created with mode 0700.

    Prefers ``XDG_RUNTIME_DIR``, which the OS already creates per user with
    mode 0700 and clears on logout. Falls back to ``~/.beagle/runtime``, which
    is inside the user's own home rather than a shared world-writable root.

    This is the correct home for IPC and other runtime state. A fixed path
    under /tmp is not: it is predictable and world-writable, so any local user
    can pre-create it — or plant a symlink at it — and win the race against a
    later start-up.

    Returns:
        The runtime directory. It exists and is private to the user.

    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(xdg) / "beagle" if xdg else Path.home() / ".beagle" / "runtime"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base


# <invariant>
# Containment is decided by real path resolution, never by string prefix.
# `str(p).startswith("/tmp")` also matches "/tmpfoo/goose", so a binary parked
# one directory over slips past the denylist; Path.is_relative_to compares
# resolved path components and cannot be fooled that way. The caller must pass
# an already-resolved path so a symlink cannot point out of a temp directory.
# </invariant>
# nosec B108 - these literals are the denylist itself, not a write target.
# tempfile.gettempdir() is deliberately not used: it returns a single
# directory, and this check must cover all three temp roots regardless of
# which one TMPDIR happens to name.
_TEMP_EXECUTION_ROOTS = (Path("/tmp"), Path("/var/tmp"), Path("/dev/shm"))  # nosec B108


def is_in_temp_dir(path: Path) -> bool:
    """Report whether a resolved path lives under a world-writable temp root.

    Args:
        path: An already-resolved absolute path.

    Returns:
        True if the path is inside /tmp, /var/tmp or /dev/shm.

    """
    resolved = path.resolve()
    return any(resolved.is_relative_to(root) for root in _TEMP_EXECUTION_ROOTS)


def reset_executable_cache() -> None:
    """Clear the resolved-executable cache.

    Intended for tests that change PATH or an override variable between
    assertions. Production code resolves once and keeps the result.
    """
    _resolved_executables.clear()
