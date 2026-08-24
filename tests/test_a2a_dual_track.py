"""B-5 regression test: A2A v1/v2 dual-track is intentional, not a bug.

Beagle has two A2A tracks:
- v1 (HMAC-SHA256) in core/a2a_integration.py — protects in-process
  messages on the agent channel. Both endpoints are inside the same
  Python process and share _A2A_AUTH_SECRET.
- v2 (Ed25519) in bridges/a2a_server.py + bridges/a2a_client.py —
  protects remote inter-agent calls over HTTP. Fail-closed: raises
  RuntimeError if PyNaCl is missing.

This test asserts that:
1. v1's verify_agent_result is callable and handles unsigned messages
   (in-process default), signed messages (HMAC), and tampered messages.
2. v2's A2AServer._verify_signature refuses HMAC fallback (fail-closed
   on missing nacl) and validates Ed25519 signatures when nacl is
   present.

Reference: audit/golden_master_v13.22.0.md B-5
"""

from __future__ import annotations

import importlib

import pytest

# ── v1 (HMAC) tests ─────────────────────────────────────────────────────────


def test_v1_in_process_unsigned_passes_by_default():
    """v1: when _a2a_enabled is False, any message is accepted."""
    mod = importlib.import_module("beagle.core.a2a_integration")
    mod.configure_a2a(enabled=False, require_signatures=False)
    assert mod.verify_agent_result({"agent_id": "x", "result": "ok"}) is True


def test_v1_in_process_hmac_round_trip():
    """v1: HMAC-signed messages are accepted when signature matches."""
    mod = importlib.import_module("beagle.core.a2a_integration")
    mod.configure_a2a(enabled=True, require_signatures=True)
    # Use the same auth secret the module just generated
    from beagle.core.a2a_protocol import (
        _A2A_AUTH_SECRET,
        _compute_hmac,
    )

    payload = {"agent_id": "agent-1", "result": "ok"}
    canonical = '{"agent_id": "agent-1", "result": "ok"}'  # JSON sort_keys
    payload["signature"] = _compute_hmac(canonical, _A2A_AUTH_SECRET)
    assert mod.verify_agent_result(payload) is True


def test_v1_in_process_hmac_tampered_rejected():
    """v1: tampered HMAC signature is rejected."""
    mod = importlib.import_module("beagle.core.a2a_integration")
    mod.configure_a2a(enabled=True, require_signatures=True)
    payload = {
        "agent_id": "agent-1",
        "result": "ok",
        "signature": "0" * 64,  # bogus signature
    }
    assert mod.verify_agent_result(payload) is False


# ── v2 (Ed25519) tests ──────────────────────────────────────────────────────


def test_v2_refuses_when_pynacl_missing(monkeypatch):
    """v2: A2AServer._verify_signature must fail-closed if nacl is missing."""
    # Simulate nacl being unavailable
    import sys as _sys

    monkeypatch.setitem(_sys.modules, "nacl.signing", None)
    monkeypatch.setitem(_sys.modules, "nacl", None)

    mod = importlib.import_module("beagle.bridges.a2a_server")
    srv = mod.BeagleToA2ABridge()
    # Force the config to require signatures
    srv.config.require_signatures = True

    result = srv._verify_signature(b"payload", "deadbeef", peer_key=b"\x00" * 32)
    assert result is False, (
        "v2 _verify_signature must return False (fail-closed) when nacl is not importable"
    )


def test_v2_valid_ed25519_signature_accepted(monkeypatch):
    """v2: a valid Ed25519 signature is accepted when nacl is available."""
    # Use real nacl if available, else skip
    nacl = pytest.importorskip("nacl.signing")

    mod = importlib.import_module("beagle.bridges.a2a_server")
    srv = mod.BeagleToA2ABridge()
    srv.config.require_signatures = True

    sk = nacl.SigningKey.generate()
    vk = sk.verify_key
    payload = b"hello, world"
    sig = sk.sign(payload).signature

    result = srv._verify_signature(payload, sig.hex(), peer_key=bytes(vk))
    assert result is True, "v2 must accept a valid Ed25519 signature"


def test_v2_invalid_ed25519_signature_rejected():
    """v2: a tampered Ed25519 signature is rejected."""
    nacl = pytest.importorskip("nacl.signing")

    mod = importlib.import_module("beagle.bridges.a2a_server")
    srv = mod.BeagleToA2ABridge()
    srv.config.require_signatures = True

    sk = nacl.SigningKey.generate()
    vk = sk.verify_key
    # Sign one message but verify a different one
    sig = sk.sign(b"original").signature

    result = srv._verify_signature(b"tampered", sig.hex(), peer_key=bytes(vk))
    assert result is False, "v2 must reject a tampered Ed25519 signature"


# ── Dual-track contract ────────────────────────────────────────────────────


def test_dual_track_documented_in_verify_agent_result():
    """The v1 verify_agent_result docstring must reference v2 + the audit."""
    mod = importlib.import_module("beagle.core.a2a_integration")
    doc = mod.verify_agent_result.__doc__ or ""
    assert "v1" in doc, "verify_agent_result docstring should mark itself as v1"
    assert "v2" in doc, "verify_agent_result docstring should reference the v2 track"
    assert "B-5" in doc, "verify_agent_result docstring should reference the audit bug ID"
