"""JWT validation helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any


def validate_jwt(token: str, secret: str, algorithm: str = "HS256") -> dict[str, Any]:
    """Validate and decode a JWT token.

    Returns the payload dict or raises an exception.
    """
    import jwt as pyjwt

    # v13.17.1: Enforce require-exp to reject tokens with missing/expired exp claim.
    # This prevents replay and cross-environment token abuse.
    try:
        payload: dict[str, Any] = pyjwt.decode(
            token,
            secret,
            algorithms=[algorithm],
            options={"require": ["exp", "iat"], "verify_exp": True},
        )
        return payload
    except pyjwt.MissingRequiredClaimError as e:
        raise ValueError(f"JWT missing required claim: {e}") from e
    except pyjwt.ExpiredSignatureError as e:
        raise ValueError("JWT token has expired") from e
    except pyjwt.InvalidTokenError as e:
        raise ValueError(f"JWT validation failed: {e}") from e


def create_jwt(
    claims: dict[str, Any],
    secret: str,
    algorithm: str = "HS256",
    ttl_seconds: int = 3600,
) -> str:
    """Create a JWT with standard exp/iat claims."""
    import jwt as pyjwt

    now = datetime.now(UTC)
    payload = {
        **claims,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    encoded: str = pyjwt.encode(payload, secret, algorithm=algorithm)
    return encoded
