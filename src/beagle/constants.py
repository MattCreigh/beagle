"""Top-level constants — single index for cross-module policy values.

This module is the **single source of truth** for project-wide policy
constants. It does NOT contain every constant in the codebase — domain
modules own their own (e.g. ``beagle.security.constants``,
``core.workflow_constants``). What lives here are the values whose
semantics span multiple modules: timeouts that the CLI and the runtime
both need, hard caps shared by validators, version metadata, and the
canonical list of supported workflows.

Why an index (not a re-export)?

  - Re-exports would couple the top-level module to every domain
    module, creating the very coupling the SSOT rule is meant to
    prevent.
  - Domain modules remain the authoritative source of their own
    constants. This module is the *phone book*.

If you add a new cross-cutting constant, add it here **and** document
which modules consume it.
"""

from __future__ import annotations

import logging
import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from pathlib import Path

logger = logging.getLogger("Beagle.constants")

# ─── Package version ────────────────────────────────────────────────────────
# `pyproject.toml [project].version` is the ONLY place the version is written.
# Every other site derives it — do not reintroduce a literal anywhere, in code,
# in the Dockerfile, or here.
#
# v1.0.2: this module used to hold its own `PACKAGE_VERSION = "1.0.1"` literal
# while calling itself the SSOT, and pyproject.toml and the Dockerfile each
# carried a third and fourth copy. That is three chances to drift and it had
# already happened twice: the v1.0.0 release left constants on 13.22.3 while
# __init__ said 1.0.0, and 84d5f10 was a separate fix for the Dockerfile pin
# lagging at 1.0.0. Deriving makes the drift class structurally impossible
# rather than test-detectable after the fact.


def _resolve_package_version() -> str:
    """Resolve the package version, with pyproject.toml as the only literal.

    Resolution order:
      1. Installed distribution metadata — the version setuptools baked in
         from ``pyproject.toml`` at build time. Correct for both wheel and
         editable installs, which is every deployed and developed case.
      2. ``pyproject.toml`` read directly — covers running from an
         uninstalled source checkout (a bare ``PYTHONPATH=src`` invocation,
         or a build backend introspecting before install).

    Returns:
        The resolved version string, or ``"0.0.0+unknown"`` if neither source
        is readable. A sentinel is returned rather than raising because a
        version lookup must never be the reason the package fails to import.

    <invariant>
    No version literal appears in this file. Build-time templating was
    considered and rejected: rendering a value into this module would leave an
    unrendered placeholder in the source tree, and the test suite imports
    beagle from source, so every test would run against a package whose
    version is the literal string "{{ version }}".
    </invariant>

    """
    try:
        return _dist_version("beagle")
    except PackageNotFoundError as exc:
        logger.warning(
            "Package metadata for 'beagle' is not installed (%s); reading the version "
            "from pyproject.toml instead.",
            exc,
        )

    # src-layout: this module is at <repo>/src/beagle/constants.py, so the
    # pyproject.toml sits two parents up (parents[2] = repo root). parents[1]
    # is src/, which contains no pyproject.toml. The version-check CI gate
    # imports an uninstalled checkout, so this fallback is the path that
    # actually runs there.
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        with pyproject.open("rb") as fh:
            return str(tomllib.load(fh)["project"]["version"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return "0.0.0+unknown"


PACKAGE_VERSION: str = _resolve_package_version()
__version__ = PACKAGE_VERSION  # re-export for backward compat

# ─── Path / filesystem ──────────────────────────────────────────────────────
# Hard cap on user-supplied path lengths. Defends against pathological
# `Path("a" * N)` inputs.
MAX_PATH_LENGTH: int = 4096

# ─── MCP / network ──────────────────────────────────────────────────────────
# Hard caps for MCP tool input. The schema hardener enforces these
# via ``additionalProperties: false``; runtime enforces the size caps.
MCP_MAX_QUERY_LENGTH: int = 50_000  # characters
MCP_MAX_TOP_K: int = 10  # 1..10 per tool contract
MCP_MAX_HOPS: int = 3  # 1..3 graph traversal depth
MCP_A2A_MAX_BODY_BYTES: int = 1_048_576  # 1 MiB A2A payload cap
MCP_A2A_MAX_INPUT_KEYS: int = 50  # task input dict cap

# Default timeouts for the MCP layer (seconds).
MCP_DEFAULT_RPC_TIMEOUT_S: float = 30.0
MCP_RAG_SEARCH_TIMEOUT_S: float = 60.0
MCP_EMBED_TIMEOUT_S: float = 30.0

# ─── Rate limiting ──────────────────────────────────────────────────────────
# Default per-tenant / per-IP request rate. Per-tenant overrides are
# read from config.toml at startup.
RATE_LIMIT_DEFAULT_RPS: float = 10.0
RATE_LIMIT_DEFAULT_BURST: int = 20
RATE_LIMIT_PER_TENANT_RPS: float = 50.0  # premium tier ceiling
RATE_LIMIT_TENANT_FLAG: str = "BEAGLE_MULTI_TENANT"  # env-var toggle

# ─── Context / compaction ───────────────────────────────────────────────────
# The default policy is defined in config/defaults.py and config/schema.py.
# These are the canonical "policy invariants" the runtime enforces even
# if a user configures otherwise (a defense-in-depth ceiling).
CONTEXT_HARD_SOVEREIGN_CEILING: float = 0.95  # never fold past this
CONTEXT_AUTO_COMPRESS_FLOOR: float = 0.50  # never fold below this

# ─── Subprocess / node execution ───────────────────────────────────────────
DEFAULT_SUBPROCESS_TIMEOUT_S: int = 300
DEFAULT_VALIDATION_TIMEOUT_S: int = 60
SUBPROCESS_MEMORY_LIMIT_BYTES: int = 4 * 1024 * 1024 * 1024  # 4 GiB
GOOSE_READY_TIMEOUT_S: float = 10.0
LLM_CLIENT_DEFAULT_TIMEOUT_S: float = 30.0
LLM_CLIENT_MAX_CLIENTS: int = 64

# ─── Database / storage ─────────────────────────────────────────────────────
RING_BUFFER_MAX_BYTES: int = 10 * 1024 * 1024  # 10 MiB
FILE_EMITTER_MAX_BYTES: int = 10 * 1024 * 1024  # 10 MiB
CACHE_MAX_SIZE: int = 200

# ─── Secrets / Guardian ─────────────────────────────────────────────────────
SECRETS_CACHE_TTL_S: int = 300  # 5 minutes
GUARDIAN_APPROVAL_TIMEOUT_S: int = 60
GUARDIAN_APPROVAL_CACHE_TTL_S: int = 300

# ─── Health / watchdog ──────────────────────────────────────────────────────
# Thresholds above which the watchdog escalates.
WATCHDOG_WARN_PCT: float = 0.70
WATCHDOG_CRITICAL_PCT: float = 0.85
HEALTHCHECK_INTERVAL_S: int = 30
HEALTHCHECK_TIMEOUT_S: int = 10
PROGRESS_MAX_AGE_HOURS: int = 24

# ─── Workflows ──────────────────────────────────────────────────────────────
# The canonical set of workflows Beagle ships. Used by the CLI for
# tab-completion, the loader for validation, and the docs for
# discoverability. Adding a workflow here without registering it in
# workflow_loader.py is a startup error.
SUPPORTED_WORKFLOWS: tuple[str, ...] = (
    "research",
    "deep-planning",
    "develop",
    "self-improvement",
    "devops",
    "db-migration",
    "audit",
    "security",
    "incident",
    "verify",
)
WORKFLOW_DEFAULT: str = "research"

# ─── Logging ────────────────────────────────────────────────────────────────
# Loggers that should be exposed at INFO by default; everything else
# stays at WARNING until explicitly enabled.
DEFAULT_INFO_LOGGERS: tuple[str, ...] = (
    "Beagle.orchestrator",
    "Beagle.cli",
    "Beagle.security",
    "Beagle.rag",
)

# ─── Feature flags ─────────────────────────────────────────────────────────
# Centralised so the CLI ``beagle doctor`` command can surface them.
FEATURE_FLAGS: dict[str, bool] = {
    "telemetry_enabled": True,
    "adaptive_turboquant": True,
    "multi_tenant_rate_limit": False,  # gated by env var
    "openclaw_provider": True,
    "ensemble_panel": True,
}


def is_workflow_supported(name: str) -> bool:
    """Return True if ``name`` is a first-class Beagle workflow.

    This is the runtime check the CLI uses to validate ``beagle run <workflow>``
    arguments. Custom workflows registered at runtime may also be valid —
    this function only answers "is it one of the *built-in* workflows?"
    """
    return name in SUPPORTED_WORKFLOWS


def assert_valid_constant_name(name: str) -> None:
    """Raise if ``name`` does not follow the project naming convention.

    Convention: UPPER_SNAKE_CASE for module-level constants, with
    optional subsystem prefix (e.g. ``MCP_MAX_QUERY_LENGTH``,
    ``RATE_LIMIT_DEFAULT_RPS``).
    """
    if not name or not name.isupper():
        raise ValueError(
            f"Constant {name!r} must be UPPER_SNAKE_CASE; "
            "see beagle/constants.py for the convention."
        )


__all__ = [
    "CACHE_MAX_SIZE",
    "CONTEXT_AUTO_COMPRESS_FLOOR",
    "CONTEXT_HARD_SOVEREIGN_CEILING",
    "DEFAULT_INFO_LOGGERS",
    "DEFAULT_SUBPROCESS_TIMEOUT_S",
    "DEFAULT_VALIDATION_TIMEOUT_S",
    "FEATURE_FLAGS",
    "FILE_EMITTER_MAX_BYTES",
    "GOOSE_READY_TIMEOUT_S",
    "GUARDIAN_APPROVAL_CACHE_TTL_S",
    "GUARDIAN_APPROVAL_TIMEOUT_S",
    "HEALTHCHECK_INTERVAL_S",
    "HEALTHCHECK_TIMEOUT_S",
    "LLM_CLIENT_DEFAULT_TIMEOUT_S",
    "LLM_CLIENT_MAX_CLIENTS",
    "MAX_PATH_LENGTH",
    "MCP_A2A_MAX_BODY_BYTES",
    "MCP_A2A_MAX_INPUT_KEYS",
    "MCP_DEFAULT_RPC_TIMEOUT_S",
    "MCP_EMBED_TIMEOUT_S",
    "MCP_MAX_HOPS",
    "MCP_MAX_QUERY_LENGTH",
    "MCP_MAX_TOP_K",
    "MCP_RAG_SEARCH_TIMEOUT_S",
    "PACKAGE_VERSION",
    "PROGRESS_MAX_AGE_HOURS",
    "RATE_LIMIT_DEFAULT_BURST",
    "RATE_LIMIT_DEFAULT_RPS",
    "RATE_LIMIT_PER_TENANT_RPS",
    "RATE_LIMIT_TENANT_FLAG",
    "RING_BUFFER_MAX_BYTES",
    "SECRETS_CACHE_TTL_S",
    "SUBPROCESS_MEMORY_LIMIT_BYTES",
    "SUPPORTED_WORKFLOWS",
    "WATCHDOG_CRITICAL_PCT",
    "WATCHDOG_WARN_PCT",
    "WORKFLOW_DEFAULT",
    "__version__",
    "assert_valid_constant_name",
    "is_workflow_supported",
]
