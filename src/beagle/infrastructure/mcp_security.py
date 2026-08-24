"""MCP Transport Security — TokenVerifier middleware and transport hardening.

SECURITY: All MCP servers default to stdio transport only.
HTTP/SSE transport is blocked unless explicitly enabled in config.toml
with mandatory Bearer token authentication.

This module provides:
1. TokenVerifier middleware for HTTP transport authentication
2. Transport enforcement utilities (block HTTP unless explicitly enabled)
3. CORS policy configuration (strict, no wildcards)

Usage:
    from beagle.infrastructure.mcp_security import (
        TokenVerifier,
        enforce_transport_security,
        configure_cors,
    )

Config (config.toml):
    [mcp_auth]
    enabled = true              # Enable auth checks (default: true)
    tokens = ["beagle-..."]       # Bearer tokens for HTTP transport
    require_https = true        # Require HTTPS for HTTP transport
    bind_address = "127.0.0.1"  # Bind to loopback only (default)

    [mcp_cors]
    allowed_origins = []        # No wildcards allowed
    allowed_methods = ["GET", "POST"]
    allowed_headers = ["Authorization", "Content-Type"]
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("Beagle.mcp_security")

# Thread lock for auth failure tracking
_auth_failures_lock = threading.Lock()

# ═══════════════════════════════════════════════════════════════════════════════
# Transport Security Constants
# ═══════════════════════════════════════════════════════════════════════════════

ALLOWED_TRANSPORTS = frozenset({"stdio"})
HTTP_TRANSPORTS = frozenset({"http", "sse", "streamable-http"})

# Rate limiting for auth failures
_MAX_AUTH_FAILURES = 10
_AUTH_FAILURE_WINDOW = 300  # 5 minutes
_MAX_FAILED_ATTEMPTS_ENTRIES = 10000  # Cap total entries to prevent unbounded memory growth (DoS)
_auth_failures: dict[str, list[float]] = {}


# ═══════════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class MCPAuthConfig:
    """Configuration for MCP server authentication.

    SECURITY: HTTP transport requires explicit opt-in AND valid tokens.
    No default fallback to insecure mode.
    """

    enabled: bool = True
    tokens: list[str] = field(default_factory=list)
    require_https: bool = True
    bind_address: str = "127.0.0.1"  # Loopback only


@dataclass
class MCPCORSConfig:
    """CORS configuration for MCP HTTP transport.

    SECURITY: No wildcard origins allowed. Must explicitly list allowed origins.
    """

    allowed_origins: list[str] = field(default_factory=list)  # EMPTY = deny all
    allowed_methods: list[str] = field(default_factory=lambda: ["GET", "POST"])
    allowed_headers: list[str] = field(default_factory=lambda: ["Authorization", "Content-Type"])
    allow_credentials: bool = False
    max_age: int = 300  # 5 minutes


# ═══════════════════════════════════════════════════════════════════════════════
# TokenVerifier
# ═══════════════════════════════════════════════════════════════════════════════


class TokenVerifier:
    """Bearer token verification middleware for MCP HTTP transport.

    Auth tokens are compared using constant-time comparison to prevent
    timing attacks. Tokens can be provided via:
    1. Environment variable BEAGLE_MCP_TOKEN
    2. config.toml [mcp_auth].tokens list
    3. Programmatically via add_token()

    Token rotation is supported via rotate_token() (replaces an old
    token with a new one) and revoke_token() (removes a specific token).
    Tokens have a configurable TTL (default: 3600s / 1 hour) enforced
    on verify(); expired tokens are automatically evicted.

    SECURITY: If HTTP transport is enabled and NO tokens are configured,
    the server WILL NOT START. This prevents accidental deployment of
    unauthenticated HTTP endpoints.
    """

    # Default token TTL in seconds (1 hour). Set to 0 for no expiry.
    DEFAULT_TOKEN_TTL = 3600

    def __init__(self, config: MCPAuthConfig | None = None) -> None:
        self._config = config or MCPAuthConfig()
        self._tokens: dict[str, float] = {}  # token_hash -> created_at
        self._failed_attempts: dict[str, list[float]] = {}
        self._auth_lock = threading.Lock()
        self._token_ttl = self._get_token_ttl()

        # Load tokens from environment
        env_token = os.environ.get("BEAGLE_MCP_TOKEN")
        if env_token:
            self.add_token(env_token)

        # Load tokens from config
        for token in self._config.tokens:
            self.add_token(token)

    @staticmethod
    def _get_token_ttl() -> float:
        """Get token TTL from BEAGLE_MCP_TOKEN_TTL env var or default."""
        try:
            return float(
                os.environ.get("BEAGLE_MCP_TOKEN_TTL", str(TokenVerifier.DEFAULT_TOKEN_TTL))
            )
        except (ValueError, TypeError):
            return TokenVerifier.DEFAULT_TOKEN_TTL

    def add_token(self, token: str) -> None:
        """Register an authentication token.

        Tokens are stored as SHA-256 hashes. All mutations to self._tokens
        must hold self._auth_lock to preserve the class invariant that
        verify/add/rotate/revoke/_evict never observe a torn dict.
        """
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self._auth_lock:
            self._tokens[token_hash] = time.monotonic()
        logger.info(f"[TokenVerifier] Registered token hash: {token_hash[:8]}...")

    def verify(self, authorization_header: str | None) -> bool:
        """Verify a Bearer token from an Authorization header.

        Uses constant-time comparison to prevent timing attacks.

        Args:
            authorization_header: The Authorization header value, e.g., "Bearer <token>"

        Returns:
            True if the token is valid, False otherwise.

        """
        if not self._config.enabled:
            logger.warning("[TokenVerifier] Auth is DISABLED — allowing all requests")
            return True

        if not authorization_header:
            logger.warning("[TokenVerifier] Missing Authorization header")
            return False

        # Extract Bearer token
        parts = authorization_header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            logger.warning("[TokenVerifier] Invalid Authorization header format")
            return False

        token = parts[1].strip()
        token_hash = hashlib.sha256(token.encode()).hexdigest()

        with self._auth_lock:
            # Evict expired tokens first (if TTL is configured)
            self._evict_expired_tokens()

            # Constant-time comparison against all registered tokens
            expired_count = 0
            for registered_hash, created_at in list(self._tokens.items()):
                if self._token_ttl > 0 and (time.monotonic() - created_at) >= self._token_ttl:
                    # Token expired — skip it
                    expired_count += 1
                    continue
                if hmac.compare_digest(token_hash, registered_hash):
                    logger.debug("[TokenVerifier] Token verified successfully")
                    return True

            if expired_count:
                logger.info(
                    f"[TokenVerifier] {expired_count} expired token(s) skipped during verify"
                )

            # Track failed attempts for rate limiting
            self._record_failure("unknown")

        logger.warning("[TokenVerifier] Invalid token attempt from unknown client")
        return False

    def _evict_stale_entries(self) -> None:
        """Evict expired and excess entries from _failed_attempts to prevent unbounded growth."""
        now = time.monotonic()
        # Remove expired timestamps from each client
        for cid in list(self._failed_attempts.keys()):
            self._failed_attempts[cid] = [
                t for t in self._failed_attempts[cid] if now - t < _AUTH_FAILURE_WINDOW
            ]
            if not self._failed_attempts[cid]:
                del self._failed_attempts[cid]
        # Cap total entries (FIFO eviction of oldest clients)
        if len(self._failed_attempts) > _MAX_FAILED_ATTEMPTS_ENTRIES:
            # Sort by oldest entry and remove the oldest clients
            sorted_clients = sorted(
                self._failed_attempts.keys(),
                key=lambda k: min(self._failed_attempts[k]),
            )
            for cid in sorted_clients[: len(sorted_clients) - _MAX_FAILED_ATTEMPTS_ENTRIES]:
                del self._failed_attempts[cid]

    def check_rate_limit(self, client_id: str) -> bool:
        """Check if a client is rate-limited due to too many auth failures.

        Args:
            client_id: Client identifier (IP address or similar).

        Returns:
            True if the client is allowed, False if rate-limited.

        """
        with self._auth_lock:
            now = time.monotonic()
            failures = self._failed_attempts.get(client_id, [])

            # Clean old failures
            failures = [t for t in failures if now - t < _AUTH_FAILURE_WINDOW]
            self._failed_attempts[client_id] = failures

            if len(failures) >= _MAX_AUTH_FAILURES:
                logger.warning(
                    f"[TokenVerifier] Rate limiting client {client_id}: "
                    f"{len(failures)} failures in {_AUTH_FAILURE_WINDOW}s"
                )
                return False

            return True

    def _record_failure(self, client_id: str) -> None:
        """Record an authentication failure for rate limiting.

        Must be called with self._auth_lock held, following the same convention
        as _evict_expired_tokens and _evict_stale_entries.

        <invariant>
        This method must NOT acquire self._auth_lock. It previously did, while
        its only caller — verify() — was already inside `with self._auth_lock`.
        threading.Lock is not reentrant, so every rejected token deadlocked the
        calling thread permanently: an unauthenticated request was enough to
        wedge the MCP auth path, and the thread never returned to be timed out
        or retried.

        The lock stays non-reentrant on purpose. Switching it to an RLock would
        have unwedged this call site and silently permitted the next accidental
        re-entry; keeping it strict means the next one deadlocks in a test
        rather than in production.
        </invariant>

        Args:
            client_id: Client identifier (IP address or similar).

        """
        now = time.monotonic()
        if client_id not in self._failed_attempts:
            self._failed_attempts[client_id] = []
        self._failed_attempts[client_id].append(now)
        # Periodically evict stale entries to prevent unbounded memory growth
        self._evict_stale_entries()

    @property
    def has_tokens(self) -> bool:
        """Check if any tokens are registered."""
        return len(self._tokens) > 0

    def rotate_token(self, old_token: str, new_token: str) -> bool:
        """Replace an old token with a new one (rotation).

        The old token is revoked and the new token is registered.
        This is the standard pattern for zero-downtime secret rotation:
        1. Add new token (both are valid during transition)
        2. Update clients to use new token
        3. Revoke old token

        For atomic rotation in a single call, use this method.

        Args:
            old_token: The token to revoke.
            new_token: The replacement token to register.

        Returns:
            True if the old token was found and rotated, False otherwise.

        """
        old_hash = hashlib.sha256(old_token.encode()).hexdigest()
        with self._auth_lock:
            if old_hash not in self._tokens:
                return False
            del self._tokens[old_hash]
            new_hash = hashlib.sha256(new_token.encode()).hexdigest()
            self._tokens[new_hash] = time.monotonic()
        logger.info("[TokenVerifier] Token rotated successfully")
        return True

    def revoke_token(self, token: str) -> bool:
        """Revoke a specific authentication token.

        Args:
            token: The token to revoke.

        Returns:
            True if the token was found and revoked, False otherwise.

        """
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self._auth_lock:
            if token_hash in self._tokens:
                del self._tokens[token_hash]
                logger.info("[TokenVerifier] Token revoked")
                return True
        return False

    def _evict_expired_tokens(self) -> None:
        """Remove tokens that have exceeded their TTL.

        Must be called with self._auth_lock held.
        """
        if self._token_ttl <= 0:
            return  # No TTL configured — tokens never expire
        now = time.monotonic()
        expired = [h for h, t in self._tokens.items() if (now - t) >= self._token_ttl]
        for h in expired:
            del self._tokens[h]
        if expired:
            logger.info(f"[TokenVerifier] Evicted {len(expired)} expired token(s)")


# ═══════════════════════════════════════════════════════════════════════════════
# Transport Enforcement
# ═══════════════════════════════════════════════════════════════════════════════


def enforce_transport_security(transport: str, auth_config: MCPAuthConfig | None = None) -> str:
    """Enforce transport security policy for MCP servers.

    SECURITY RULES:
    1. stdio transport is ALWAYS allowed (local process communication only)
    2. HTTP/SSE transports are BLOCKED unless:
       a. Explicitly enabled in config.toml [mcp_auth]
       b. Valid Bearer tokens are configured
       c. Binding to 127.0.0.1 (loopback)
    3. No unauthenticated HTTP endpoints are permitted

    Args:
        transport: Requested transport ("stdio", "http", "sse", "streamable-http")
        auth_config: Authentication configuration (uses defaults if None)

    Returns:
        The validated transport string (always "stdio" unless auth is configured)

    Raises:
        RuntimeError: If HTTP transport is requested without proper auth configuration.

    """
    config = auth_config or MCPAuthConfig()
    transport_lower = transport.lower().strip()

    # stdio is always allowed
    if transport_lower in ALLOWED_TRANSPORTS:
        return transport_lower

    # HTTP/SSE requires explicit auth configuration
    if transport_lower in HTTP_TRANSPORTS:
        if not config.enabled:
            raise RuntimeError(
                f"SECURITY: HTTP transport '{transport}' requested but auth is DISABLED. "
                f"HTTP transport requires [mcp_auth] enabled = true in config.toml. "
                f"Use stdio transport for local process communication."
            )

        if not config.tokens and not os.environ.get("BEAGLE_MCP_TOKEN"):
            raise RuntimeError(
                f"SECURITY: HTTP transport '{transport}' requested but NO authentication tokens "
                f"are configured. Set [mcp_auth].tokens in config.toml or the BEAGLE_MCP_TOKEN "
                f"environment variable. Unauthenticated HTTP endpoints are NOT permitted."
            )

        import ipaddress as _ipaddr

        _is_loopback = False
        if config.bind_address == "localhost":
            _is_loopback = True
        else:
            try:
                _is_loopback = _ipaddr.ip_address(config.bind_address.strip("[]")).is_loopback
            except ValueError:
                _is_loopback = False
        if not _is_loopback:
            raise RuntimeError(
                f"SECURITY: MCP HTTP transport cannot bind to {config.bind_address}. "
                f"Only loopback addresses are permitted. Change bind_address in config.toml."
            )

        logger.info(
            f"[Security] MCP HTTP transport '{transport}' authorized with "
            f"{len(config.tokens)} token(s), binding to {config.bind_address}"
        )
        return transport_lower

    raise RuntimeError(
        f"SECURITY: Unknown transport '{transport}'. "
        f"Allowed transports: {', '.join(sorted(ALLOWED_TRANSPORTS | HTTP_TRANSPORTS))}"
    )


def generate_token() -> str:
    """Generate a cryptographically secure Bearer token.

    Returns:
        A 32-byte hex token prefixed with 'beagle-'.

    """
    return f"beagle-{secrets.token_hex(32)}"


# ═══════════════════════════════════════════════════════════════════════════════
# CORS Configuration
# ═══════════════════════════════════════════════════════════════════════════════


def configure_cors(cors_config: MCPCORSConfig | None = None) -> dict[str, Any]:
    """Generate CORS configuration for MCP HTTP transport.

    SECURITY: Wildcard origins ("*") are ALWAYS rejected.
    If no origins are specified, CORS is effectively disabled (deny all).

    Args:
        cors_config: CORS configuration (uses strict defaults if None)

    Returns:
        Dictionary suitable for FastAPI/Starlette CORS middleware configuration.

    """
    config = cors_config or MCPCORSConfig()

    # SECURITY: Reject wildcard origins
    if "*" in config.allowed_origins:
        raise RuntimeError(
            "SECURITY: Wildcard CORS origin ('*') is not permitted. "
            "Specify explicit origins in [mcp_cors].allowed_origins."
        )

    return {
        "allow_origins": config.allowed_origins,  # EMPTY = deny all
        "allow_methods": config.allowed_methods,
        "allow_headers": config.allowed_headers,
        "allow_credentials": config.allow_credentials,
        "max_age": config.max_age,
    }


def load_auth_config_from_env() -> MCPAuthConfig:
    """Load MCP auth configuration from environment variables.

    Environment variables:
        BEAGLE_MCP_AUTH_ENABLED: Enable/disable auth checks (default: true)
        BEAGLE_MCP_TOKEN: Bearer token for HTTP transport
        BEAGLE_MCP_REQUIRE_HTTPS: Require HTTPS for HTTP transport (default: true)
        BEAGLE_MCP_BIND_ADDRESS: Bind address for HTTP transport (default: 127.0.0.1)

    Returns:
        MCPAuthConfig with values from environment.

    """
    tokens = []
    env_token = os.environ.get("BEAGLE_MCP_TOKEN")
    if env_token:
        tokens.append(env_token)

    return MCPAuthConfig(
        enabled=os.environ.get("BEAGLE_MCP_AUTH_ENABLED", "true").lower() in ("true", "1", "yes"),
        tokens=tokens,
        require_https=os.environ.get("BEAGLE_MCP_REQUIRE_HTTPS", "true").lower()
        in ("true", "1", "yes"),
        bind_address=os.environ.get("BEAGLE_MCP_BIND_ADDRESS", "127.0.0.1"),
    )


__all__ = [
    "ALLOWED_TRANSPORTS",
    "MCPAuthConfig",
    "MCPCORSConfig",
    "TokenVerifier",
    "configure_cors",
    "enforce_transport_security",
    "generate_token",
    "load_auth_config_from_env",
]
