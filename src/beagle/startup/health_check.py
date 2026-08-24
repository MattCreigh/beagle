"""Startup health check — validates environment before orchestrator runs.

Each check is a callable that returns a :class:`StartupCheckResult`.
Checks are partitioned into *required* (failure blocks startup) and
*optional* (failure produces a warning but does not block).

Required checks:
  - Config loads without error
  - Essential directories are writable
  - Core modules are importable
  - Ollama Cloud endpoint is reachable (v13.22.3 — prevents the
    silent-fallback regression where ``PROVIDER_FALLBACK_CHAIN``
    hardcoded ``["openai"]`` masked api.openai.com 401s, the circuit
    breaker tripped after 5 calls, and every workflow silently
    returned "Retry after Ns" without surfacing the real cause)

Optional checks:
  - MCP server scripts are on disk
  - Orpheus ring directory is accessible
  - Firecracker binary is present (MicroVM sandbox)
  - Model provider is reachable
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from beagle.runtime.goose_cli import GooseCliRuntime
from beagle.security.validation import validate_http_url

logger = logging.getLogger("Beagle.startup")


@dataclass
class StartupCheckResult:
    """Result of a single startup check."""

    name: str
    status: str  # "ok" | "warn" | "fail" | "skip"
    message: str
    fix_hint: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"

    @property
    def is_fail(self) -> bool:
        return self.status == "fail"


# ── Individual Checks ────────────────────────────────────────────────────


def check_config_loads() -> StartupCheckResult:
    """Verify config.toml parses and loads into WorkflowConfig."""
    try:
        from beagle.config.loader import load_config

        config = load_config()
        if config.orchestrator is None:
            return StartupCheckResult(
                name="config",
                status="fail",
                message="WorkflowConfig.orchestrator is None",
                fix_hint="Check config.toml [orchestrator] section",
            )
        return StartupCheckResult(name="config", status="ok", message="Config loaded successfully")
    # Config loading can fail on: file I/O (OSError), TOML parse /
    # validation (ValueError, incl. tomllib.TOMLDecodeError), structural
    # access (KeyError, TypeError), or the loader import (ImportError).
    # These are the realistic failure modes; anything else should surface.
    except (OSError, ValueError, KeyError, TypeError, ImportError) as err:
        return StartupCheckResult(
            name="config",
            status="fail",
            message=f"Config load failed: {err}",
            fix_hint="Verify config.toml syntax with: python -c "
            "'from beagle.config.loader import load_config; "
            "load_config()'",
        )


def check_essential_directories() -> StartupCheckResult:
    """Verify essential directories exist or can be created."""
    from beagle.config.loader import load_config
    from beagle.reproducibility.recorder import DEFAULT_REPLAY_DIR

    config = load_config()
    data_root = config.paths.data_root or str(Path.home() / ".beagle")
    dirs_to_check = [
        ("data_root", data_root),
        ("checkpoint_dir", str(Path(data_root) / "checkpoints")),
        # v1.0.2: an unset replay_dir defers to the recorder's own resolver
        # (BEAGLE_REPLAY_DIR env, else <data_root>/replays) so config and
        # recorder cannot disagree about where manifests live.
        ("replay_dir", config.reproducibility.replay_dir or str(DEFAULT_REPLAY_DIR)),
    ]

    failures: list[str] = []
    for label, dir_path in dirs_to_check:
        p = Path(dir_path)
        if not p.is_absolute():
            # v1.0.2: resolve relative paths against data_root, NOT
            # workspace_root. workspace_root is the package directory, so the
            # old behaviour mkdir'd runtime state inside the install tree
            # (site-packages under a wheel install) on every startup check.
            p = Path(data_root) / dir_path
        try:
            p.mkdir(parents=True, exist_ok=True)
            # Test write
            test = p / ".startup_write_test"
            test.touch()
            test.unlink()
        except OSError as exc:
            failures.append(f"{label} ({dir_path}): {exc}")

    if failures:
        return StartupCheckResult(
            name="directories",
            status="fail",
            message=f"Directory check failed: {'; '.join(failures)}",
            fix_hint="Create missing directories or fix permissions",
        )
    return StartupCheckResult(
        name="directories",
        status="ok",
        message="Essential directories exist/writable",
    )


def check_core_modules_importable() -> StartupCheckResult:
    """Verify core Beagle modules can be imported."""
    required_modules = [
        "beagle.core.autonomous_orchestrator",
        "beagle.config.loader",
        "beagle.security",
        "beagle.events",
        "beagle.health",
        "beagle.startup",
    ]
    failures: list[str] = []
    for mod_name in required_modules:
        try:
            importlib.import_module(mod_name)
        except ImportError as exc:
            failures.append(f"{mod_name}: {exc}")

    if failures:
        return StartupCheckResult(
            name="core_modules",
            status="fail",
            message=f"Import failures: {'; '.join(failures)}",
            fix_hint="Run: pip install -e . from project root",
        )
    return StartupCheckResult(
        name="core_modules",
        status="ok",
        message="All core modules importable",
    )


def check_ollama_cloud_endpoint() -> StartupCheckResult:
    """Verify the Ollama Cloud endpoint is reachable.

    v13.22.3: This check is **required** (not optional) because it
    prevents the silent-fallback regression that motivated the
    check. Concretely:

    * If Ollama Cloud is unreachable, every subprocess goose call
      fails the HTTP transport layer, the per-model circuit
      breaker opens after 5 failures, and the workflow returns
      ``CircuitBreakerOpenError: retry after Ns`` — which looks
      like a transient rate-limit rather than a connectivity
      problem. Without this check, the orchestrator would happily
      fail every workflow without telling anyone the real reason.

    * The check probes the public ``/api/tags`` endpoint on
      ``https://ollama.com`` with a 10s timeout. A 200 response
      is treated as reachable; any non-2xx, network error, or
      timeout is treated as a hard failure.

    * Auth is NOT required for the tag listing endpoint; this
      check verifies connectivity only, not authentication.
      A misconfigured OLLAMA_CLOUD_API_KEY will still surface
      at the first real model call.

    The check is intentionally cheap (one HTTPS GET, no model
    invocation) so it adds <1s to startup.
    """
    import urllib.error
    import urllib.request

    # Probe the CONFIGURED provider endpoint — no preset host. When no
    # endpoint is configured the check reports "skipped", not failure.
    from ..config.loader import get_config as _load_config

    try:
        endpoint = str(_load_config().ollama_cloud.endpoint or "").rstrip("/")
        endpoint = f"{endpoint}/api/tags" if endpoint else ""
    except Exception as exc:  # noqa: BLE001 - health probe must never crash startup
        logger.debug("provider endpoint unavailable from config: %s", exc)
        endpoint = ""
    if not endpoint:
        return StartupCheckResult(
            name="ollama_cloud_endpoint",
            status="skip",
            message=(
                "no provider endpoint configured — set [ollama_cloud] endpoint "
                "in ~/.config/beagle config.toml to enable the reachability probe"
            ),
            fix_hint="point endpoint at any OpenAI-compatible API base URL (see README)",
        )
    timeout_s = 10.0
    try:
        req = urllib.request.Request(
            validate_http_url(endpoint),
            headers={"User-Agent": "Beagle-startup-check/13.22.3"},
        )
        # urlopen returns an http.client.HTTPResponse; read() is a
        # stream cursor — calling it twice gives you the next slice.
        # Concatenate a small head preview (for the failure path
        # where we only want a snippet) with the full body so the
        # JSON parse below has the complete payload.
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # nosec B310 - scheme checked by security.validation.validate_http_url before this call
            status = resp.status
            body_preview = resp.read(512).decode("utf-8", errors="replace")
            full_body = body_preview + resp.read().decode("utf-8", errors="replace")
    except TimeoutError as exc:
        return StartupCheckResult(
            name="ollama_cloud_endpoint",
            status="fail",
            message=(
                f"Ollama Cloud endpoint {endpoint} timed out after "
                f"{timeout_s}s — connectivity broken; workflows "
                f"will fall back through every model and trip the "
                f"circuit breaker"
            ),
            fix_hint=(
                "Check network/DNS routing to ollama.com; if behind "
                "a proxy, configure HTTPS_PROXY in the env"
            ),
            details={"endpoint": endpoint, "error": str(exc)},
        )
    except urllib.error.URLError as exc:
        return StartupCheckResult(
            name="ollama_cloud_endpoint",
            status="fail",
            message=(f"Ollama Cloud endpoint {endpoint} unreachable: {exc.reason}"),
            fix_hint=("Verify outbound HTTPS to ollama.com:443; check firewall + DNS"),
            details={"endpoint": endpoint, "error": str(exc)},
        )
    except (OSError, ValueError) as exc:
        # DNS failure, connection refused, TLS handshake error, etc.
        return StartupCheckResult(
            name="ollama_cloud_endpoint",
            status="fail",
            message=(f"Ollama Cloud endpoint {endpoint} unreachable: {exc}"),
            fix_hint=("Check DNS, TLS, and outbound network to ollama.com:443"),
            details={"endpoint": endpoint, "error": str(exc)},
        )

    if status != 200:
        return StartupCheckResult(
            name="ollama_cloud_endpoint",
            status="fail",
            message=(f"Ollama Cloud endpoint returned HTTP {status} — subprocess calls will fail"),
            fix_hint=(f"Probe {endpoint} directly; if 5xx, retry; if 4xx, the URL may have moved"),
            details={
                "endpoint": endpoint,
                "status": status,
                "body_preview": body_preview[:200],
            },
        )

    # Optional: parse the JSON body and confirm at least one
    # configured primary model is present. This catches the
    # "config.toml lists a model that ollama cloud doesn't host"
    # drift before the first subprocess call does. The full body
    # was hoisted out of the context-managed response above.
    import json

    missing: list[str] = []
    try:
        payload = json.loads(full_body)
        advertised = {m.get("name", "") for m in payload.get("models", [])}
        from beagle.config.allowlist import allowed_models

        configured = set(allowed_models())
        # v1.0.8: the allowlist now uses bare model names (e.g.
        # ``minimax-m3``, ``gemma4:31b``) without ``:cloud``/``-cloud``
        # suffixes. Check ALL configured models against the advertised
        # list, stripping any cloud suffix if present for the comparison.
        # The previous logic only checked cloud-suffixed entries, which
        # meant bare-name models were silently skipped — a retired model
        # would pass startup clean.

        def _bare_name(m: str) -> str:
            return m[: -len("-cloud")] if m.endswith((":cloud", "-cloud")) else m

        missing = sorted(m for m in configured if _bare_name(m) not in advertised)
    except (json.JSONDecodeError, AttributeError, KeyError, ValueError) as exc:
        # Parsing failure is non-fatal — connectivity is the primary
        # concern. Just log the parse error in details.
        return StartupCheckResult(
            name="ollama_cloud_endpoint",
            status="ok",
            message=(f"Ollama Cloud reachable ({status}); model-list parse skipped: {exc}"),
            details={"endpoint": endpoint, "status": status},
        )

    if missing:
        return StartupCheckResult(
            name="ollama_cloud_endpoint",
            status="fail",
            message=(
                f"Ollama Cloud reachable ({status}) BUT "
                f"{len(missing)} configured model(s) not advertised: "
                f"{', '.join(missing)}"
            ),
            fix_hint=(
                "Update [models.allowed] in config.toml to match "
                "the live /api/tags catalogue, or add the model to "
                "your Ollama Cloud account"
            ),
            details={
                "endpoint": endpoint,
                "missing": missing,
            },
        )

    return StartupCheckResult(
        name="ollama_cloud_endpoint",
        status="ok",
        message=("Ollama Cloud reachable; all configured :cloud models advertised"),
        details={"endpoint": endpoint, "status": status},
    )


def check_goose_binary() -> StartupCheckResult:
    """Verify goose binary is accessible."""
    from beagle.config.loader import load_config

    config = load_config()
    # v13.22.3: defer to the runtime's goose-binary resolver so the .orig
    # suffix only appears as a last-resort fallback (after shutil.which for
    # "goose" and "goose.orig" both come up empty). config.paths.goose_bin
    # already goes through that helper.
    # B4: when the configured runtime is not goose_cli, a missing goose
    # binary is expected; report OK, not a warning.
    from beagle.runtime.loader import runtime_plugin_name

    if runtime_plugin_name() != "goose_cli":
        return StartupCheckResult(
            name="goose_binary",
            status="ok",
            message="Goose binary not required for the configured runtime",
        )

    goose_path = config.paths.goose_bin or GooseCliRuntime().binary_path

    if not goose_path or not Path(goose_path).exists():
        return StartupCheckResult(
            name="goose_binary",
            status="warn",
            message=f"Goose binary not found at '{goose_path}'",
            fix_hint="Install goose or set GOOSE_BIN env var",
        )

    if not os.access(goose_path, os.X_OK):
        return StartupCheckResult(
            name="goose_binary",
            status="warn",
            message=f"Goose binary not executable: {goose_path}",
            fix_hint=f"chmod +x {goose_path}",
        )

    return StartupCheckResult(
        name="goose_binary",
        status="ok",
        message=f"Goose binary OK: {goose_path}",
    )


# ── Optional Checks ─────────────────────────────────────────────────────


def check_mcp_server_scripts() -> StartupCheckResult:
    """Verify MCP server Python modules are importable (optional)."""
    # MCP servers are invoked via `python3 -m beagle.infrastructure.mcp_rag_server`
    # Check importability rather than file paths
    mcp_modules = [
        ("rag_server", "beagle.infrastructure.mcp_rag_server"),
        ("utility_server", "beagle.infrastructure.mcp_utility_server"),
    ]
    missing: list[str] = []
    for label, mod_name in mcp_modules:
        try:
            importlib.import_module(mod_name)
        except ImportError as exc:
            missing.append(f"{label} ({mod_name}): {exc}")

    if missing:
        return StartupCheckResult(
            name="mcp_scripts",
            status="warn",
            message=f"MCP server import failures: {'; '.join(missing)}",
            fix_hint="Ensure package is installed: pip install -e .",
        )
    return StartupCheckResult(
        name="mcp_scripts",
        status="ok",
        message="MCP server modules importable",
    )


def check_orpheus_ring_dir() -> StartupCheckResult:
    """Verify Orpheus ring buffer directory is accessible (optional)."""
    ring_dir = Path(os.environ.get("ORPHEUS_RING_DIR", "/run/orpheus_ring"))

    if ring_dir.exists() and os.access(ring_dir, os.W_OK):
        return StartupCheckResult(
            name="orpheus_rings",
            status="ok",
            message=f"Orpheus ring dir OK: {ring_dir}",
        )

    # Try fallback. Previously hardcoded a distro-specific path (audit E9 /
    # CWE-377): a predictable world-writable temp path is a symlink-attack and
    # temp-file-prediction risk on multi-user hosts. Prefer the per-user
    # XDG_RUNTIME_DIR (0700, owner-only), which is the correct home for
    # runtime IPC sockets that must not collide across users.
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        fallback = Path(xdg_runtime) / "orpheus_ring"
    else:
        fallback = Path(tempfile.gettempdir()) / f"orpheus_ring-{os.getuid()}"
    if fallback.exists() and os.access(fallback, os.W_OK):
        return StartupCheckResult(
            name="orpheus_rings",
            status="ok",
            message=f"Orpheus using fallback: {fallback}",
        )

    return StartupCheckResult(
        name="orpheus_rings",
        status="warn",
        message=f"Orpheus ring dir not accessible: {ring_dir}",
        fix_hint="Run: scripts/setup_orpheus_rings.py "
        "or the fallback will be auto-created at startup",
    )


def check_firecracker_binary() -> StartupCheckResult:
    """Verify Firecracker binary exists (optional, for MicroVM)."""
    from beagle.config.loader import load_config

    config = load_config()
    if not config.sandbox_microvm.enabled:
        return StartupCheckResult(
            name="firecracker",
            status="ok",
            message="MicroVM sandbox disabled (by config)",
        )

    fc = Path(config.sandbox_microvm.firecracker_binary)
    if fc.exists() and os.access(fc, os.X_OK):
        return StartupCheckResult(
            name="firecracker",
            status="ok",
            message=f"Firecracker OK: {fc}",
        )

    return StartupCheckResult(
        name="firecracker",
        status="warn",
        message=f"Firecracker not found: {fc}",
        fix_hint="Install with: scripts/setup_firecracker.py",
    )


def check_edge_features_importable() -> StartupCheckResult:
    """Verify competitive-differentiator modules import (optional)."""
    features = [
        ("MicroVM sandbox", "beagle.core.sandbox"),
        ("A2A protocol", "beagle.core.a2a_protocol"),
        ("TurboQuant", "beagle.core.turboquant"),
        ("Replay engine", "beagle.reproducibility"),
    ]
    failures: list[str] = []
    for label, mod_name in features:
        try:
            importlib.import_module(mod_name)
        except ImportError as exc:
            failures.append(f"{label} ({mod_name}): {exc}")

    if failures:
        return StartupCheckResult(
            name="edge_features",
            status="warn",
            message=f"Edge feature import failures: {'; '.join(failures)}",
            fix_hint="Reinstall package: pip install -e .",
        )
    return StartupCheckResult(
        name="edge_features",
        status="ok",
        message="All edge features importable",
    )


def check_google_re2() -> StartupCheckResult:
    """Verify google-re2 is available for ReDoS-safe secret scrubbing."""
    # Probe via find_spec rather than `import re2`: a bare import would bind a
    # name we never use (vulture), while ImportError needs real import. find_spec
    # answers "is it installed" with no unused binding (audit E10 / vulture).
    try:
        re2_available = importlib.util.find_spec("re2") is not None
    except (ImportError, ValueError, AttributeError):
        re2_available = False
    if re2_available:
        return StartupCheckResult(
            name="google_re2",
            status="ok",
            message="google-re2 available — secret scrubbing is ReDoS-safe",
        )
    else:
        return StartupCheckResult(
            name="google_re2",
            status="warn",
            message="google-re2 not installed — secret scrubbing will FAIL CLOSED (ImportError)",
            fix_hint="Install with: pip install google-re2 (required for production)",
        )


# ── Orchestrator ─────────────────────────────────────────────────────────


def run_startup_checks(
    *,
    include_optional: bool = True,
    stop_on_fail: bool = False,
) -> list[StartupCheckResult]:
    """Run all startup health checks.

    Args:
        include_optional: Run optional (warn-only) checks.
        stop_on_fail: Stop after first required check failure.

    Returns:
        List of check results.

    """
    required: list[Callable[[], StartupCheckResult]] = [
        check_config_loads,
        check_essential_directories,
        check_core_modules_importable,
        # v13.22.3: connectivity is REQUIRED, not optional. The
        # silent-fallback regression we're guarding against was
        # a hard-to-detect breaker-trip loop with no obvious
        # cause. Failing loud at startup is the only safe option.
        check_ollama_cloud_endpoint,
    ]
    optional: list[Callable[[], StartupCheckResult]] = [
        check_goose_binary,
        check_mcp_server_scripts,
        check_orpheus_ring_dir,
        check_firecracker_binary,
        check_edge_features_importable,
        check_google_re2,
    ]

    results: list[StartupCheckResult] = []

    for check_fn in required:
        result = check_fn()
        results.append(result)
        logger.debug(
            "Startup check [%s]: %s — %s",
            result.name,
            result.status,
            result.message,
        )
        if result.is_fail and stop_on_fail:
            return results

    if include_optional:
        for check_fn in optional:
            result = check_fn()
            results.append(result)
            logger.debug(
                "Startup check [%s]: %s — %s",
                result.name,
                result.status,
                result.message,
            )

    return results


def format_startup_report(results: list[StartupCheckResult]) -> str:
    """Format check results into a human-readable report."""
    lines: list[str] = ["Beagle Startup Health Check", "=" * 40]

    ok_count = sum(1 for r in results if r.is_ok)
    warn_count = sum(1 for r in results if r.status == "warn")
    fail_count = sum(1 for r in results if r.is_fail)

    for r in results:
        icon = {"ok": "✓", "warn": "⚠", "fail": "✗"}[r.status]
        lines.append(f"  [{icon}] {r.name}: {r.message}")
        if r.fix_hint and r.status != "ok":
            lines.append(f"      → {r.fix_hint}")

    lines.append("")
    lines.append(f"Summary: {ok_count} ok, {warn_count} warnings, {fail_count} failures")
    return "\n".join(lines)
