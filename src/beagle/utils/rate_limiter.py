# Rate limiting — thin re-export shim.
#
# DEPRECATED: This monolithic module has been consolidated into
# ``beagle.utils.rate_limiter/`` (package).
# All classes and functions are re-exported here for backward compatibility.
#
# v13.20.10 (R5.1): Designated removal version: v13.22 (Q3 2026).
# After v13.22, callers should import directly from
# `beagle.utils.rate_limiter` (the package). This
# shim is preserved for one minor cycle to give downstream forks
# time to migrate. Closes audit C3.

# Re-export everything from the package
from beagle.utils.rate_limiter import (  # type: ignore[attr-defined]
    RateLimitConfig,
    RateLimiter,
    TokenBucket,
    WorkflowRateLimiter,
    get_rate_limiter,
    get_rate_limiter_async,
    reset_rate_limiter,
    reset_rate_limiter_async,
)

__all__ = [
    "RateLimitConfig",
    "RateLimiter",
    "TokenBucket",
    "WorkflowRateLimiter",
    "get_rate_limiter",
    "get_rate_limiter_async",
    "reset_rate_limiter",
    "reset_rate_limiter_async",
]
