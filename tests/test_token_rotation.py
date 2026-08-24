"""Section 9.3: Token rotation tests for TokenVerifier.

Validates rotate_token(), revoke_token(), and TTL-based expiry.
"""

from __future__ import annotations

import hashlib
import os
import time
from unittest.mock import patch

from beagle.infrastructure.mcp_security import (
    MCPAuthConfig,
    TokenVerifier,
    generate_token,
)


class TestTokenRotation:
    """rotate_token() replaces old token with new one."""

    def test_rotate_replaces_old_with_new(self):
        """After rotation, old token fails and new token works."""
        tv = TokenVerifier(MCPAuthConfig(enabled=True, tokens=["old-token"]))
        assert tv.verify("Bearer old-token")

        tv.rotate_token("old-token", "new-token")
        assert not tv.verify("Bearer old-token")
        assert tv.verify("Bearer new-token")

    def test_rotate_nonexistent_returns_false(self):
        """Rotating a token that doesn't exist returns False."""
        tv = TokenVerifier(MCPAuthConfig(enabled=True, tokens=["existing"]))
        assert not tv.rotate_token("nonexistent", "replacement")
        # Original still works
        assert tv.verify("Bearer existing")

    def test_rotate_updates_created_at(self):
        """Rotated token gets a fresh created_at timestamp."""
        tv = TokenVerifier(MCPAuthConfig(enabled=True, tokens=["old"]))
        old_hash = hashlib.sha256(b"old").hexdigest()
        old_time = tv._tokens[old_hash]

        time.sleep(0.01)
        tv.rotate_token("old", "fresh")
        new_hash = hashlib.sha256(b"fresh").hexdigest()
        assert tv._tokens[new_hash] > old_time


class TestTokenRevocation:
    """revoke_token() removes a specific token."""

    def test_revoke_removes_token(self):
        """Revoked token no longer authenticates."""
        tv = TokenVerifier(MCPAuthConfig(enabled=True, tokens=["to-revoke"]))
        assert tv.verify("Bearer to-revoke")

        tv.revoke_token("to-revoke")
        assert not tv.verify("Bearer to-revoke")

    def test_revoke_nonexistent_returns_false(self):
        """Revoking a nonexistent token returns False."""
        tv = TokenVerifier(MCPAuthConfig(enabled=True, tokens=["real"]))
        assert not tv.revoke_token("fake")

    def test_revoke_preserves_other_tokens(self):
        """Revoking one token doesn't affect others."""
        tv = TokenVerifier(MCPAuthConfig(enabled=True, tokens=["a", "b"]))
        tv.revoke_token("a")
        assert not tv.verify("Bearer a")
        assert tv.verify("Bearer b")


class TestTokenTTLExpiry:
    """Tokens expire after configurable TTL."""

    def test_expired_token_fails_verify(self):
        """Tokens older than TTL are rejected on verify."""
        tv = TokenVerifier(MCPAuthConfig(enabled=True, tokens=["expiring"]))
        # Backdate the token to simulate expiry
        token_hash = hashlib.sha256(b"expiring").hexdigest()
        tv._tokens[token_hash] = time.monotonic() - 7200  # 2 hours ago

        with patch.dict(os.environ, {"BEAGLE_MCP_TOKEN_TTL": "3600"}):
            tv._token_ttl = 3600
            assert not tv.verify("Bearer expiring")

    def test_ttl_zero_means_no_expiry(self):
        """TTL=0 means tokens never expire."""
        tv = TokenVerifier(MCPAuthConfig(enabled=True, tokens=["permanent"]))
        token_hash = hashlib.sha256(b"permanent").hexdigest()
        tv._tokens[token_hash] = time.monotonic() - 999999

        tv._token_ttl = 0
        assert tv.verify("Bearer permanent")

    def test_evict_expired_tokens_removes_old(self):
        """_evict_expired_tokens removes expired entries from _tokens."""
        tv = TokenVerifier(MCPAuthConfig(enabled=True, tokens=["fresh"]))
        # Add an already-expired token manually
        expired_hash = hashlib.sha256(b"expired").hexdigest()
        tv._tokens[expired_hash] = time.monotonic() - 7200
        tv._token_ttl = 3600

        with tv._auth_lock:
            tv._evict_expired_tokens()

        assert expired_hash not in tv._tokens
        fresh_hash = hashlib.sha256(b"fresh").hexdigest()
        assert fresh_hash in tv._tokens


class TestGenerateToken:
    """generate_token() produces secure tokens."""

    def test_generate_token_prefix(self):
        """Generated tokens have 'beagle-' prefix."""
        token = generate_token()
        assert token.startswith("beagle-")

    def test_generate_token_uniqueness(self):
        """Each generated token is unique."""
        tokens = {generate_token() for _ in range(10)}
        assert len(tokens) == 10

    def test_generate_token_works_with_verifier(self):
        """Generated token can be registered and verified."""
        tv = TokenVerifier(MCPAuthConfig(enabled=True))
        token = generate_token()
        tv.add_token(token)
        assert tv.verify(f"Bearer {token}")
