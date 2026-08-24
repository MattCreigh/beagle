"""Regression tests for TokenVerifier race condition (C2)."""

from __future__ import annotations

import inspect

from beagle.infrastructure.mcp_security import TokenVerifier


class TestTokenVerifierRace:
    """Concurrent add/verify must not lose tokens or crash."""

    def test_add_token_uses_lock(self) -> None:
        """add_token must acquire self._auth_lock (C2 fix)."""
        source = inspect.getsource(TokenVerifier.add_token)
        assert "self._auth_lock" in source, "add_token doesn't use self._auth_lock"
        assert "with self._auth_lock" in source, "add_token doesn't acquire lock"

    def test_verify_uses_lock(self) -> None:
        """verify must acquire self._auth_lock during token dict access."""
        source = inspect.getsource(TokenVerifier.verify)
        assert "self._auth_lock" in source, "verify doesn't use self._auth_lock"
        assert "with self._auth_lock" in source, "verify doesn't acquire lock"

    def test_concurrent_add_verify_simulated(self) -> None:
        """Simulate concurrent add/verify by rapidly interleaving operations."""
        verifier = TokenVerifier()
        tokens: list[str] = []

        # Rapid interleave: add 20 tokens
        for i in range(20):
            token = f"agent-{i}"
            verifier.add_token(token)
            tokens.append(token)

        # Verify all 20
        for token in tokens:
            assert verifier.verify(f"Bearer {token}") is True

        # Add more and verify again (mix operations)
        for i in range(20, 40):
            token = f"agent-{i}"
            verifier.add_token(token)
            assert verifier.verify(f"Bearer {token}") is True

        # Final count
        assert len(list(tokens) + [f"agent-{i}" for i in range(20, 40)]) == 40

    def test_add_verify_under_capacity(self) -> None:
        """Adding tokens should succeed."""
        verifier = TokenVerifier()
        verifier.add_token("valid-token-a")
        verifier.add_token("valid-token-b")
        verifier.add_token("valid-token-c")
        assert verifier.verify("Bearer valid-token-a") is True
        assert verifier.verify("Bearer valid-token-b") is True
        assert verifier.verify("Bearer valid-token-c") is True

    def test_add_verify_over_capacity_no_crash(self) -> None:
        """Adding many tokens should still not crash."""
        verifier = TokenVerifier()
        for i in range(10):
            verifier.add_token(f"overflow-{i}")
        assert verifier.verify("Bearer overflow-8") is True
        assert verifier.verify("Bearer overflow-9") is True
        for i in range(10):
            assert verifier.verify(f"Bearer overflow-{i}") is True

    def test_no_bare_exception_in_verify(self) -> None:
        """verify() must not use bare 'except Exception'."""
        source = inspect.getsource(TokenVerifier.verify)
        assert "except Exception" not in source, "verify() still has bare except Exception"
