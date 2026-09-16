"""Sandbox layering routing stub — best-effort, no hard wasmtime dependency.

Phase 3 of the DAG fault-recovery hardening: documents and implements the
strategy to layer wasmtime for untrusted payloads while keeping the AST
validator (``beagle.security.validate_python_code_ast``) as the first-pass
filter.

The routing reads the ``[sandbox] mode`` config flag (``native`` | ``wasm`` |
``hybrid``, default ``native``) and:

* ``native`` → AST validation only (current behaviour).
* ``wasm``  → attempt wasmtime; if unavailable, log and fall back to AST-only.
* ``hybrid`` → AST validation first, then wasmtime if the payload passes.

wasmtime is **never** a hard dependency: importing it is wrapped in a lazy
try/except, and every path that cannot use it degrades to the existing AST-only
behaviour with a debug log.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("Beagle.fault_recovery.sandbox")

VALID_MODES = frozenset({"native", "wasm", "hybrid"})

DEFAULT_MODE = "native"


def get_sandbox_mode() -> str:
    """Return the configured ``[sandbox] mode`` value (best-effort).

    Returns:
        One of ``native``, ``wasm``, ``hybrid``. Defaults to ``native`` when
        the config is unreadable or the value is unknown.
    """
    try:
        # Imported through the loader module (not by value from
        # config.config) so tests can substitute the accessor: a
        # ``from x import get_config`` binding would be immune to
        # patching ``x.get_config``.
        from beagle.config import loader

        cfg = loader.get_config()
        mode = getattr(cfg, "sandbox_mode", DEFAULT_MODE)
    except (
        OSError,
        RuntimeError,
        ImportError,
        ValueError,
        TypeError,
    ) as exc:  # RATIONALE=config layer unavailable — these are the failure modes get_config() can actually raise; a live payload route cannot abort on a bug in this daemon
        logger.debug("[Sandbox] Config unavailable; using native mode (%s)", exc)
        return DEFAULT_MODE
    if mode not in VALID_MODES:
        logger.warning(
            "[Sandbox] Unknown sandbox mode %r; falling back to native.", mode
        )
        return DEFAULT_MODE
    return str(mode)


def _wasmtime_available() -> bool:
    """Return whether the ``wasmtime`` package is importable (lazy probe)."""
    try:
        import wasmtime  # noqa: F401

        return True
    except ImportError:
        return False


def route_payload(
    code: str,
    *,
    mode: str | None = None,
) -> dict[str, Any]:
    """Route an untrusted payload through the configured sandbox layering.

    Args:
        code: The untrusted source to validate.
        mode: Override the config mode. Defaults to :func:`get_sandbox_mode`.

    Returns:
        A dict with keys:
        - ``mode``: the effective mode used.
        - ``verdict``: ``"pass"`` | ``"reject"`` | ``"fallback_native"``.
        - ``ast_passed``: whether the AST validator accepted the payload.
        - ``wasm_passed``: ``True``/``False``/``None`` (None = not attempted).
        - ``reason``: human-readable outcome.

    Notes:
        This is a best-effort stub. ``wasm``/``hybrid`` degrade to AST-only
        behaviour (``verdict`` = ``"fallback_native"``) when wasmtime is
        unavailable — it is never a hard dependency.
    """
    from beagle.security import validate_python_code_ast

    effective = mode or get_sandbox_mode()
    if effective not in VALID_MODES:
        effective = DEFAULT_MODE

    ast_valid, ast_error = validate_python_code_ast(code)
    ast_passed = ast_valid
    ast_reason = ast_error

    wasm_passed: bool | None = None
    verdict: str
    reason: str

    if not ast_passed:
        verdict = "reject"
        reason = f"AST validation rejected the payload: {ast_reason}"
    elif effective == "native":
        verdict = "pass"
        reason = "Native mode: AST-only validation."
    else:
        # wasm or hybrid: attempt wasmtime, degrade gracefully if missing.
        if not _wasmtime_available():
            verdict = "fallback_native"
            reason = (
                "wasmtime unavailable; degraded to AST-only validation "
                "(best-effort stub, no hard dependency)."
            )
            logger.debug("[Sandbox] wasmtime unavailable — falling back to native.")
        else:
            # D-09: wasmtime imports but no WebAssembly execution exists. The
            # previous branch set wasm_passed = ast_passed and verdict = "pass",
            # which reports a pass the stub never computed. The honest branch
            # two lines above already names the correct verdict; reuse it and
            # leave the pass uncomputed rather than asserting one we did not
            # perform.
            verdict = "fallback_native"
            reason = (
                f"Mode {effective}: wasmtime probe succeeded but no wasm "
                "execution backend exists yet; degraded to AST-only "
                "validation (best-effort stub)."
            )

    return {
        "mode": effective,
        "verdict": verdict,
        "ast_passed": ast_passed,
        "wasm_passed": wasm_passed,
        "reason": reason,
    }


__all__ = [
    "DEFAULT_MODE",
    "VALID_MODES",
    "get_sandbox_mode",
    "route_payload",
]
