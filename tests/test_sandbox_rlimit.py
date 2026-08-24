"""Regression tests for sandbox rlimit leak (C1)."""

from __future__ import annotations

import resource
from unittest.mock import patch

import pytest

from beagle.core.sandbox import SandboxContext


class TestSandboxRlimit:
    """Verify rlimit capture/restore lifecycle."""

    def test_capture_inside_context(self) -> None:
        """SandboxContext captures ORIGINAL limits on __enter__."""
        ctx = SandboxContext()
        assert ctx.original_limits == {}  # not captured yet
        with ctx:
            assert ctx.original_limits is not None
            assert isinstance(ctx.original_limits, dict)
            assert len(ctx.original_limits) > 0

    def test_original_limits_populated(self) -> None:
        """Captured dict contains at least RLIMIT_AS, RLIMIT_NOFILE, RLIMIT_NPROC."""
        ctx = SandboxContext()
        with ctx:
            keys = {resource.RLIMIT_AS, resource.RLIMIT_NOFILE, resource.RLIMIT_NPROC}
            captured = set(ctx.original_limits.keys())
            assert keys.issubset(captured)

    def test_restore_returns_limits_to_original(self) -> None:
        """After restoring, current limits must equal original."""
        ctx = SandboxContext()
        with ctx:
            # Lower the soft limit inside context (can't raise hard limit in container)
            orig_soft, orig_hard = ctx.original_limits[resource.RLIMIT_NOFILE]
            resource.setrlimit(resource.RLIMIT_NOFILE, (orig_soft - 1, orig_hard))
        # Original limits restored on __exit__
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        assert (soft, hard) == (orig_soft, orig_hard), "RLIMIT_NOFILE not restored"

    def test_restore_on_failure(self) -> None:
        """setrlimit failure during __exit__ restore must be handled gracefully."""
        ctx = SandboxContext()
        with ctx:
            pass
        # After exiting, original limits are stored
        with patch("resource.setrlimit") as mock_setrlimit:
            mock_setrlimit.side_effect = ValueError("mock failure")
            # Re-enter context and exit — restore will attempt to run inside __exit__
            try:
                with ctx:
                    pass
            except Exception:  # ruff: ignore[BLE001]
                pytest.fail("restore_on_failure should not raise")

    def test_concurrent_restore_idempotent(self) -> None:
        """Multiple context exits should be idempotent (no crash)."""
        ctx = SandboxContext()
        with ctx:
            orig_soft, orig_hard = ctx.original_limits[resource.RLIMIT_NOFILE]
            resource.setrlimit(resource.RLIMIT_NOFILE, (orig_soft - 1, orig_hard))
        with ctx:
            pass
        with ctx:
            pass
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        assert (soft, hard) == (orig_soft, orig_hard)
