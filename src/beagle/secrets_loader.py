"""Centralized secrets loader for Beagle.

Replaces the duplicated _load_api_key() functions across 6+ infrastructure
files with a single, secure implementation that:

1. Checks environment variables first (preferred path).
2. Falls back to ~/.config/goose/secrets.yaml with proper YAML safe_load.
3. Validates file permissions (must be 0o600 or 0o400 on POSIX).
4. Caches results with configurable TTL — rotated secrets are picked up
   within the TTL window without restarting the process.
5. Never logs or exposes secret values.

Usage:
    from beagle.secrets_loader import load_secret

    api_key = load_secret("OLLAMA_CLOUD_API_KEY")

    # After rotating secrets:
    from beagle.secrets_loader import clear_secret_cache
    clear_secret_cache()
"""

from __future__ import annotations

import logging
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("Beagle.secrets_loader")

# Maximum acceptable file mode for secrets files (owner read/write only).
# Anything more permissive is a security risk on multi-tenant systems.
_MAX_SECRET_FILE_MODE = 0o600

# Default cache TTL in seconds (300 = 5 minutes).
# Set BEAGLE_SECRET_CACHE_TTL=0 to disable caching entirely.
_DEFAULT_CACHE_TTL = 300

# Path to goose secrets file
_SECRETS_PATH = Path.home() / ".config/goose/secrets.yaml"

# Module-level cache: {cache_key: (value, cached_at_timestamp)}
_secret_cache: dict[str, tuple[str, float]] = {}
_secret_cache_lock = threading.Lock()


def get_cache_ttl() -> int:
    """Get the current secret cache TTL in seconds.

    Reads from BEAGLE_SECRET_CACHE_TTL environment variable, falling back
    to _DEFAULT_CACHE_TTL (300 seconds). Set to 0 to disable caching.

    Returns:
        Cache TTL in seconds (0 = no caching).

    """
    try:
        ttl = int(os.environ.get("BEAGLE_SECRET_CACHE_TTL", str(_DEFAULT_CACHE_TTL)))
        return max(0, ttl)
    except (ValueError, TypeError):
        return _DEFAULT_CACHE_TTL


def _check_file_permissions(path: Path) -> None:
    """Validate that the secrets file has restrictive permissions.

    Raises PermissionError if the file is readable by group or others.
    """
    try:
        file_mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError as exc:
        raise PermissionError(f"Cannot stat secrets file {path}: {exc}") from exc

    if file_mode & ~0o600:
        # File is readable by group or others — block by default (S01 remediation).
        # Previously defaulted to warn-only; now raises unless explicitly opted out.
        extra = file_mode & ~0o600
        strict_secrets = os.environ.get("BEAGLE_STRICT_SECRETS", "1")
        if strict_secrets == "0":
            logger.warning(
                f"Secrets file {path} has overly permissive mode "
                f"{oct(file_mode)} (extra bits: {oct(extra)}). "
                f"Run: chmod 600 {path}"
            )
        else:
            # Default: block loading entirely — permissive secrets files are a
            # security risk on multi-tenant systems.
            raise PermissionError(
                f"Secrets file {path} has mode {oct(file_mode)} — "
                f"must be 0600 or 0400. "
                f"Run: chmod 600 {path}  "
                f"Set BEAGLE_STRICT_SECRETS=0 to downgrade to warning."
            )


def _load_secrets_yaml(path: Path) -> dict[str, Any]:
    """Load and parse secrets.yaml using yaml.safe_load.

    v13.20.2 (R2.3): PyYAML is a hard dependency (declared in
    pyproject.toml [project] dependencies). The previous line-by-line
    fallback `_parse_secrets_line_by_line` has been deleted — the R2.3
    doctrine is "consolidate to the canonical implementation, not ship
    a degraded fallback for missing optional deps". A missing PyYAML
    is now a hard fail with an actionable install message; a missing
    secrets.yaml returns `{}` (consistent with prior behaviour for the
    file-absent path).

    Returns:
        Dict of secret key-value pairs (empty dict on file-absent,
        hard-fail on PyYAML-missing).

    """
    try:
        import yaml

        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            return data
        if data is None:
            return {}
        logger.warning(f"Secrets file {path} did not parse as a dict (got {type(data).__name__})")
        return {}

    except FileNotFoundError:
        return {}

    except ImportError as exc:
        # v13.20.2: was a warning + line-by-line fallback. Now a hard fail
        # because the fallback was best-effort and could not handle
        # multi-line values, nested structures, or YAML anchors — which
        # are exactly the kind of structured secrets a real deployment
        # would store (think multi-line SSH keys, OIDC client secrets,
        # nested per-service credentials). A degraded fallback that
        # silently drops keys is worse than a hard fail.
        raise ImportError(
            f"PyYAML is required to load {path}. "
            f"Install it with: pip install pyyaml. "
            f"(v13.20.2: hard dependency, no fallback.)"
        ) from exc


def load_secret(key: str, *, allow_env: bool = True, allow_file: bool = True) -> str:
    """Load a secret value by key name.

    Resolution order:
      1. Environment variable (if allow_env is True)
      2. ~/.config/goose/secrets.yaml (if allow_file is True)
      3. Empty string if not found

    Results are cached with a configurable TTL (default 300s) to avoid
    repeated file I/O while ensuring rotated secrets are picked up.
    Set BEAGLE_SECRET_CACHE_TTL=0 to disable caching entirely.

    Args:
        key: Secret key name (e.g., "OLLAMA_CLOUD_API_KEY")
        allow_env: Check os.environ first (default: True)
        allow_file: Fall back to secrets.yaml (default: True)

    Returns:
        The secret value as a string, or "" if not found.

    """
    cache_key = f"{key}:{allow_env}:{allow_file}"
    ttl = get_cache_ttl()

    # Check cache with TTL expiry (thread-safe)
    with _secret_cache_lock:
        if ttl > 0 and cache_key in _secret_cache:
            value, cached_at = _secret_cache[cache_key]
            if time.monotonic() - cached_at < ttl:
                return value
            # TTL expired — evict and re-fetch
            del _secret_cache[cache_key]

    value = ""

    # Path 1: Environment variable (always fresh — no caching concern)
    if allow_env:
        value = os.environ.get(key, "")

    # Path 2: secrets.yaml
    if not value and allow_file and _SECRETS_PATH.exists():
        try:
            _check_file_permissions(_SECRETS_PATH)
            secrets = _load_secrets_yaml(_SECRETS_PATH)
            # D8 (Fable 5 DD 2026-06-11): the previous `str(secrets.get(key, ""))`
            # coerced a YAML null (`None`) to the literal string "None", which
            # is truthy and looks like a real (wrong) secret. Treat None and
            # missing identically → empty string.
            raw = secrets.get(key, "")
            value = "" if raw is None else str(raw)
        except PermissionError:
            logger.error(f"Permission check failed for {_SECRETS_PATH}; skipping file load")
        except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.error(f"Error loading secrets from {_SECRETS_PATH}: {exc}")

    # Cache result (even if empty — avoids repeated lookups for missing keys)
    with _secret_cache_lock:
        if ttl > 0:
            _secret_cache[cache_key] = (value, time.monotonic())

    return value


def clear_cache() -> None:
    """Clear the in-process secret cache.

    Useful after rotating secrets or in testing.
    """
    with _secret_cache_lock:
        _secret_cache.clear()


# Backward-compatible alias
clear_secret_cache = clear_cache


def load_api_key() -> str:
    """Convenience wrapper: load OLLAMA_CLOUD_API_KEY.

    This replaces all the duplicated _load_api_key() functions
    across the infrastructure modules.
    """
    return load_secret("OLLAMA_CLOUD_API_KEY")
