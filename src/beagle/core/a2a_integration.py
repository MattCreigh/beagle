"""A2A Integration for Beagle Orchestrator.

Wires the A2A (Agent-to-Agent) protocol into the DAG orchestrator for
cryptographic message signing and verification between parent/sub agents.

When A2A signing is enabled, every spawned agent delegation message is
HMAC-signed, and every incoming agent ping result is verified. This
provides cryptographic proof of authenticity in multi-agent workflows.

Config (config.toml):
    [a2a]
    enabled = true                      # Enable A2A signing/verification
    require_signatures = false          # Set true for strict mode (reject unsigned)
    keypair_path = "~/.beagle/a2a_secret"  # Path to HMAC secret file
    auto_generate_keypair = true        # Auto-generate secret if missing
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from beagle.core.a2a_protocol import (
    _A2A_AUTH_SECRET,
    _A2A_SECRET_FILE,
    _compute_hmac,
    _verify_hmac,
)

logger = logging.getLogger("Beagle.a2a_integration")

# Configuration state
_a2a_enabled: bool = False
_a2a_require_signatures: bool = False


def configure_a2a(
    enabled: bool = True,
    require_signatures: bool = False,
) -> None:
    """Configure A2A integration for the orchestrator.

    Args:
        enabled: Whether A2A signing/verification is active.
        require_signatures: If True, reject unsigned agent messages.

    """
    global _a2a_enabled, _a2a_require_signatures
    _a2a_enabled = enabled
    _a2a_require_signatures = require_signatures

    if enabled:
        # Ensure we have an auth secret
        _ensure_auth_secret()
        logger.info("A2A integration enabled (require_signatures=%s)", require_signatures)
    else:
        logger.info("A2A integration disabled")


def _ensure_auth_secret() -> str:
    """Ensure an A2A auth secret exists, generating one if needed.

    Returns:
        The auth secret for signing.

    """
    if _A2A_AUTH_SECRET:
        return _A2A_AUTH_SECRET

    # Generate a new secret
    import hashlib

    secret = hashlib.sha256(os.urandom(32)).hexdigest()
    try:
        _A2A_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(_A2A_SECRET_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, secret.encode())
        finally:
            os.close(fd)
        logger.info("A2A: Generated and persisted auth secret at %s", _A2A_SECRET_FILE)
    except OSError as e:
        logger.warning("A2A: Could not persist auth secret: %s", e)

    return secret


def sign_delegation(
    workflow_id: str,
    agent_id: str,
    task_description: str,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Sign a delegation message from parent orchestrator to subagent.

    Args:
        workflow_id: The parent workflow ID.
        agent_id: The spawned agent ID.
        task_description: The task being delegated.
        permissions: List of permission strings the subagent has.

    Returns:
        Signed delegation dict with HMAC signature.

    """
    if not _a2a_enabled:
        return {
            "workflow_id": workflow_id,
            "agent_id": agent_id,
            "task": task_description,
            "permissions": permissions or [],
        }

    secret = _A2A_AUTH_SECRET or _ensure_auth_secret()
    timestamp = str(int(time.time()))

    payload: dict[str, Any] = {
        "workflow_id": workflow_id,
        "agent_id": agent_id,
        "task": task_description,
        "permissions": permissions or [],
        "timestamp": timestamp,
    }

    # Compute HMAC over the canonical JSON representation
    payload["a2a_version"] = "1.0"
    canonical = json.dumps(payload, sort_keys=True)
    signature = _compute_hmac(canonical, secret)
    payload["signature"] = signature

    logger.debug("A2A: Signed delegation for agent %s in workflow %s", agent_id, workflow_id)
    return payload


def verify_agent_result(
    result: dict[str, Any],
    strict: bool | None = None,
) -> bool:
    """Verify an incoming agent result message.

    Args:
        result: The agent result dict, potentially with A2A signature.
        strict: Override require_signatures for this check.

    Returns:
        True if the result is authentic or A2A is disabled.

    Note (B-5, audit v13.22.0):
        This function is part of the A2A **v1** (HMAC-SHA256) track that
        protects **in-process** messages on the agent channel — the
        sub-agents that Beagle spawns locally and the messages exchanged
        between orchestrator nodes. The v1 path is appropriate here because
        both endpoints of the channel are inside the same Python process
        and share the same secret via ``_A2A_AUTH_SECRET``.

        The A2A **v2** (Ed25519) track lives in
        ``beagle.bridges.a2a_server`` /
        ``.a2a_client`` and protects **remote** inter-agent calls over
        HTTP. v2 is fail-closed: if PyNaCl is missing, it raises
        ``RuntimeError`` rather than silently downgrading to HMAC.

        The two tracks are intentionally separate. Migrating the
        in-process channel to v2 is a multi-day refactor (the message
        bus does not carry a peer public key) and is tracked as LT-1
        in ``audits/golden_master_v13.22.0_metaplan.md``.

    """
    if not _a2a_enabled:
        return True

    require_sig = strict if strict is not None else _a2a_require_signatures
    signature = result.get("signature")

    if not signature:
        if require_sig:
            logger.warning(
                "A2A: Rejected unsigned result from agent %s (strict mode)",
                result.get("agent_id", "unknown"),
            )
            return False
        # Accept unsigned in non-strict mode
        return True

    # Verify HMAC signature
    secret = _A2A_AUTH_SECRET
    if not secret:
        logger.warning("A2A: No auth secret available for verification")
        return not require_sig

    # Reconstruct payload without signature for verification
    payload = {k: v for k, v in result.items() if k not in ("signature",)}
    canonical = json.dumps(payload, sort_keys=True)

    return _verify_hmac(canonical, signature, secret)


def is_a2a_enabled() -> bool:
    """Check if A2A integration is currently enabled."""
    return _a2a_enabled
