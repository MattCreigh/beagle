"""
A2A (Agent-to-Agent) Protocol Implementation for Beagle.

Implements the Agent2Agent protocol for inter-operability with other agent frameworks.
Based on Google/Meta's A2A specification.

Enhanced in v13.4: OIDC/RBAC zero-trust, OpenTelemetry tracing.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# OpenTelemetry integration (Phase 5)
try:
    from opentelemetry import trace
    from opentelemetry.trace.status import StatusCode

    _tracer = trace.get_tracer("beagle.a2a", "13.4.0")
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False

    class _StubTracer:
        """No-op tracer when OpenTelemetry is not available."""

        def start_as_current_span(self, name: str, **_kwargs: Any) -> Any:
            from contextlib import nullcontext

            return nullcontext()

        def start_span(self, name: str, **_kwargs: Any) -> Any:
            from contextlib import nullcontext

            return nullcontext()

    _tracer = _StubTracer()  # type: ignore[assignment]

# aiohttp is imported lazily in _ensure_session to keep the module importable
# without the optional runtime dependency, but typed references are used in
# send_task/get_task/subscribe_task. This module-level import prevents F821
# while keeping the import guarded.
try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore[assignment]

logger = logging.getLogger("Beagle.A2A")

# A2A Protocol Constants
A2A_VERSION = "1.0.0"
DEFAULT_PORT = 8081
DEFAULT_HOST = "127.0.0.1"  # Localhost only — never bind to 0.0.0.0

# HMAC authentication for inter-agent communication
# Secret resolution order:
#   1. A2A_AUTH_SECRET environment variable
#   2. ~/.config/beagle/a2a_secret file (persisted across restarts)
#   3. Ephemeral random secret (only if no file exists; file is created for next run)
_A2A_AUTH_SECRET = os.environ.get("A2A_AUTH_SECRET", "")
_A2A_SECRET_FILE = Path.home() / ".config" / "beagle" / "a2a_secret"
_A2A_AUTH_WINDOW_SECONDS = 300  # 5-minute window for HMAC freshness

if not _A2A_AUTH_SECRET:
    if os.environ.get("BEAGLE_ENV") == "production":
        raise RuntimeError(
            "A2A_AUTH_SECRET must be set in production. "
            "Set the A2A_AUTH_SECRET env var or configure "
            "[a2a] keypair_path in config.toml."
        )
    else:
        # Try to load from persisted secret file
        if _A2A_SECRET_FILE.exists():
            try:
                # S02 remediation: refuse to load the secret if it is
                # world- or group-readable. An attacker with local file
                # access could otherwise replace the secret and forge
                # inter-agent messages (per audit finding S02).
                file_mode = _A2A_SECRET_FILE.stat().st_mode & 0o777
                if file_mode & 0o077:
                    logger.warning(
                        "A2A: Refusing to read %s — mode %#04o is group/world "
                        "readable. Re-run with chmod 600 on the file.",
                        _A2A_SECRET_FILE,
                        file_mode,
                    )
                    _A2A_AUTH_SECRET = ""  # nosec B105 - empty-string sentinel meaning "no secret loaded", not a credential
                else:
                    _A2A_AUTH_SECRET = _A2A_SECRET_FILE.read_text().strip()
                    if _A2A_AUTH_SECRET:
                        logger.info("A2A: Loaded persisted auth secret from %s", _A2A_SECRET_FILE)
                    else:
                        _A2A_AUTH_SECRET = ""  # nosec B105 - empty file; regenerate below
            except (ImportError, OSError) as exc:
                logger.warning("A2A: Failed to read secret file %s: %s", _A2A_SECRET_FILE, exc)
                _A2A_AUTH_SECRET = ""  # nosec B105 - empty-string sentinel meaning "no secret loaded", not a credential

        if not _A2A_AUTH_SECRET:
            # Generate and persist a new secret
            _A2A_AUTH_SECRET = hashlib.sha256(os.urandom(32)).hexdigest()
            try:
                _A2A_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(_A2A_SECRET_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    os.write(fd, _A2A_AUTH_SECRET.encode())
                finally:
                    os.close(fd)
                logger.info("A2A: Generated and persisted auth secret to %s", _A2A_SECRET_FILE)
            except OSError as exc:
                logger.warning(
                    "A2A: Could not persist auth secret to %s: %s "
                    "(subsequent processes will generate different secrets)",
                    _A2A_SECRET_FILE,
                    exc,
                )


# ── Shared types (canonical definitions in a2a_types.py) ─────────────────────
# Re-exported for backward compatibility — external code imports from this module.
# The ``__all__`` below documents the public re-export surface so vulture
# treats these as used (they are deliberately not referenced in this module,
# only re-exported) without hiding them behind a per-file suppression.
from beagle.core.a2a_types import (  # ruff: ignore[E402]
    A2AConnection,
    A2AMessage,
    AgentCard,
    AgentDescriptor,
    CoreMessageType,
    MessagePriority,
    MessageType,
    Task,
    TaskStatus,
)

__all__ = [
    "A2AConnection",
    "A2AMessage",
    "AgentCard",
    "AgentDescriptor",
    "CoreMessageType",
    "MessagePriority",
    "MessageType",
    "Task",
    "TaskStatus",
]


class A2AAgent:
    """A2A-compatible agent that can send/receive messages."""

    def __init__(
        self,
        agent_id: str,
        agent_card: AgentCard,
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    ):
        self.agent_id = agent_id
        self.agent_card = agent_card
        self.handler = handler
        self.tasks: dict[str, Task] = {}

    async def handle_message(self, message: A2AMessage) -> A2AMessage:
        """Handle an incoming A2A message."""
        logger.info(f"[{self.agent_id}] Handling message: {message.method}")

        with _tracer.start_as_current_span("a2a.handle_message") as span:
            if _OTEL_AVAILABLE:
                span.set_attribute("a2a.agent_id", self.agent_id)
                span.set_attribute("a2a.method", message.method or "unknown")
                span.set_attribute("a2a.message_id", message.id)

            try:
                if message.method == "tasks/send":
                    result = await self._handle_send_task(message.params)
                elif message.method == "tasks/sendSubscribe":
                    result = await self._handle_send_subscribe(message.params)
                elif message.method == "tasks/get":
                    result = await self._handle_get_task(message.params)
                elif message.method == "tasks/cancel":
                    result = await self._handle_cancel_task(message.params)
                elif message.method == "agent/info":
                    result = {"agentCard": self.agent_card.to_dict()}
                elif message.method == "agent/capabilities":
                    result = {"capabilities": self.agent_card.capabilities}
                else:
                    if _OTEL_AVAILABLE:
                        span.set_attribute("a2a.error", f"Method not found: {message.method}")
                    return A2AMessage(
                        id=message.id,
                        error={
                            "code": -32601,
                            "message": f"Method not found: {message.method}",
                        },
                    )

                return A2AMessage(id=message.id, result=result)

            except Exception as e:  # broad catch intentional
                logger.exception(f"[{self.agent_id}] Error handling message")
                if _OTEL_AVAILABLE:
                    span.set_attribute("a2a.error", str(e))
                    span.set_status(StatusCode.ERROR, str(e))
                return A2AMessage(
                    id=message.id,
                    error={"code": -32603, "message": str(e)},
                )

    async def _handle_send_task(self, params: dict | None) -> dict[str, Any]:
        """Handle tasks/send RPC."""
        if not params:
            raise ValueError("Missing params — tasks/send requires 'params' dict with 'data' field")

        task = Task(
            id=params.get("taskId", str(uuid.uuid4())),
            agent_id=self.agent_id,
            input_data=params.get("data"),
            status=TaskStatus.WORKING,
        )
        self.tasks[task.id] = task

        try:
            output = await self.handler(task.input_data or {})
            task.output_data = output
            task.status = TaskStatus.COMPLETED
        except (RuntimeError, ValueError, OSError, TypeError) as e:
            # A failed handler must be recorded on the task rather than
            # propagate. These are the exception families an agent handler is
            # expected to surface; anything else is an unexpected defect that
            # SHOULD propagate so the caller sees it, not be silently swallowed.
            task.error = str(e)
            task.status = TaskStatus.FAILED

        task.updated_at = datetime.now(UTC).isoformat()
        return {"task": task.to_dict()}

    async def _handle_send_subscribe(self, params: dict | None) -> dict[str, Any]:
        """Handle tasks/sendSubscribe RPC (streaming)."""
        return await self._handle_send_task(params)

    async def _handle_get_task(self, params: dict | None) -> dict[str, Any]:
        """Handle tasks/get RPC."""
        if not params or "taskId" not in params:
            raise ValueError("Missing taskId — tasks/get requires 'taskId' in params")

        task = self.tasks.get(params["taskId"])
        if not task:
            raise ValueError(f"Task not found: {params['taskId']}")

        return {"task": task.to_dict()}

    async def _handle_cancel_task(self, params: dict | None) -> dict[str, Any]:
        """Handle tasks/cancel RPC."""
        if not params or "taskId" not in params:
            raise ValueError("Missing taskId — tasks/cancel requires 'taskId' in params")

        task = self.tasks.get(params["taskId"])
        if task:
            task.status = TaskStatus.CANCELED
            task.updated_at = datetime.now(UTC).isoformat()

        return {"task": task.to_dict() if task else None}

    async def send_to_agent(
        self,
        endpoint: str,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Send a message to another A2A agent."""
        try:
            import aiohttp
        except ImportError:
            logger.error("aiohttp required for A2A. Install: pip install aiohttp")
            raise

        message = A2AMessage(method=method, params=params)

        async with (
            aiohttp.ClientSession() as session,
            session.post(
                endpoint,
                json=message.to_dict(),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp,
        ):
            response_data = await resp.json()
            return response_data


class A2AClient:
    """Client for interacting with A2A agents."""

    def __init__(self, endpoint: str, agent_card: AgentCard | None = None):
        self.endpoint = endpoint.rstrip("/")
        self.agent_card = agent_card
        self._session: Any = None  # aiohttp.ClientSession | None

    async def _ensure_session(self) -> None:
        """Ensure aiohttp session exists."""
        if self._session is None:
            import aiohttp

            # D13 (Fable 5 DD 2026-06-11): previous version created the
            # session with no timeout, so a hung server would hang the
            # A2A client indefinitely. Apply a default 30s total timeout
            # (connect + read); env-tunable via A2A_CLIENT_TIMEOUT.
            try:
                default_timeout_s = float(os.environ.get("A2A_CLIENT_TIMEOUT", "30"))
            except ValueError:
                default_timeout_s = 30.0
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=default_timeout_s)
            )

    @staticmethod
    def _resolve_timeout(timeout: float | None) -> float:
        """Resolve explicit caller timeout > session default > env > 30s."""
        if timeout is not None and timeout > 0:
            return timeout
        try:
            return float(os.environ.get("A2A_CLIENT_TIMEOUT", "30"))
        except ValueError:
            return 30.0

    async def close(self) -> None:
        """Close the client session."""
        if self._session:
            await self._session.close()
            self._session = None

    async def get_agent_card(self) -> AgentCard:
        """Fetch agent card from endpoint."""
        await self._ensure_session()
        async with self._session.get(f"{self.endpoint}/agent.json") as resp:
            data = await resp.json()
            return AgentCard.from_dict(data)

    async def send_task(
        self,
        data: dict[str, Any],
        task_id: str | None = None,
        timeout: float | None = None,
    ) -> Task:
        """Send a task to the agent."""
        # D13 (Fable 5 DD 2026-06-11): keep the span open around the actual
        # HTTP call so the trace measures end-to-end latency, not just param
        # construction. Also apply an explicit per-request timeout.
        request_timeout = A2AClient._resolve_timeout(timeout)
        with _tracer.start_as_current_span("a2a.client.send_task") as span:
            if _OTEL_AVAILABLE:
                span.set_attribute("a2a.endpoint", self.endpoint)
                if task_id:
                    span.set_attribute("a2a.task_id", task_id)

            await self._ensure_session()

            params = {"data": data}
            if task_id:
                params["taskId"] = task_id  # type: ignore[assignment]

            message = A2AMessage(method="tasks/send", params=params)

            async with self._session.post(
                f"{self.endpoint}/rpc",
                json=message.to_dict(),
                timeout=aiohttp.ClientTimeout(total=request_timeout),
            ) as resp:
                result = await resp.json()

        response = A2AMessage.from_dict(result)

        if response.error:
            raise RuntimeError(f"A2A error: {response.error}")

        task_data = response.result.get("task", {})  # type: ignore[union-attr]
        return Task.from_dict(task_data)

    async def get_task(
        self,
        task_id: str,
        timeout: float | None = None,
    ) -> Task:
        """Get task status."""
        request_timeout = A2AClient._resolve_timeout(timeout)
        await self._ensure_session()

        message = A2AMessage(
            method="tasks/get",
            params={"taskId": task_id},
        )

        async with self._session.post(
            f"{self.endpoint}/rpc",
            json=message.to_dict(),
            timeout=aiohttp.ClientTimeout(total=request_timeout),
        ) as resp:
            result = await resp.json()
            response = A2AMessage.from_dict(result)

            if response.error:
                raise RuntimeError(f"A2A error: {response.error}")

            task_data = response.result.get("task", {})  # type: ignore[union-attr]
            return Task.from_dict(task_data)

    async def subscribe_task(self, task_id: str) -> Any:
        """Subscribe to task updates via SSE."""
        await self._ensure_session()
        # D13: apply timeout to the long-lived SSE subscription as well.
        request_timeout = A2AClient._resolve_timeout(None)
        async with self._session.get(
            f"{self.endpoint}/tasks/{task_id}/subscribe",
            timeout=aiohttp.ClientTimeout(total=request_timeout),
        ) as resp:
            async for line in resp.content:
                if line:
                    yield line.decode("utf-8").strip()


def _compute_hmac(message: str, secret: str = _A2A_AUTH_SECRET) -> str:
    """Compute HMAC-SHA256 for message authentication."""
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def _verify_hmac(message: str, signature: str, secret: str = _A2A_AUTH_SECRET) -> bool:
    """Verify HMAC-SHA256 signature."""
    expected = _compute_hmac(message, secret)
    return hmac.compare_digest(expected, signature)


def _generate_auth_token(agent_id: str, timestamp: str | None = None) -> str:
    """Generate an authenticated token for agent registration.

    Combines agent_id with timestamp and signs with HMAC.
    """
    if timestamp is None:
        timestamp = str(int(datetime.now(UTC).timestamp()))
    payload = f"{agent_id}:{timestamp}"
    signature = _compute_hmac(payload)
    return f"{payload}:{signature}"


def _verify_auth_token(token: str) -> bool:
    """Verify an agent registration token.

    Checks HMAC signature and freshness (within A2A_AUTH_WINDOW_SECONDS).
    """
    if not token:
        # An absent/empty token is a verification failure, not a crash. The
        # caller treats this as a rejected registration (PermissionError).
        logger.warning("A2A: No auth token supplied")
        return False
    try:
        parts = token.split(":")
        if len(parts) != 3:
            logger.warning("A2A: Invalid auth token format")
            return False

        agent_id, timestamp, signature = parts
        payload = f"{agent_id}:{timestamp}"

        if not _verify_hmac(payload, signature):
            logger.warning(f"A2A: Auth token HMAC verification failed for agent {agent_id}")
            return False

        # Check token freshness
        try:
            token_time = int(timestamp)
            current_time = int(datetime.now(UTC).timestamp())
            if abs(current_time - token_time) > _A2A_AUTH_WINDOW_SECONDS:
                logger.warning(f"A2A: Auth token expired for agent {agent_id}")
                return False
        except (ValueError, OSError):
            logger.warning(f"A2A: Invalid timestamp in auth token for agent {agent_id}")
            return False

        return True

    except (ValueError, OSError, TypeError) as e:
        # Any malformed token, I/O failure, or type mismatch is a verification
        # failure, not a crash. These are the exception families this function
        # can legitimately raise; unexpected exceptions should propagate so a
        # real defect is not masked as a benign "invalid token".
        logger.error(f"A2A: Auth token verification error: {e}")
        return False


# ---------------------------------------------------------------------------
# OIDC Verifier (Phase 3: Zero-Trust Identity)
# ---------------------------------------------------------------------------


class OIDCVerifier:
    """Verify OIDC JWT tokens for A2A zero-trust authentication.

    In production, this validates tokens against an OIDC provider's JWKS endpoint.
    In local/dev mode, it validates HMAC-signed tokens for agent identity.
    """

    def __init__(
        self,
        issuer: str | None = None,
        audience: str | None = None,
        jwks_url: str | None = None,
    ):
        self.issuer = issuer or os.environ.get("A2A_OIDC_ISSUER", "")
        self.audience = audience or os.environ.get("A2A_OIDC_AUDIENCE", "beagle-a2a")
        self.jwks_url = jwks_url or os.environ.get("A2A_OIDC_JWKS_URL", "")
        self._clock_skew_seconds = int(os.environ.get("A2A_OIDC_CLOCK_SKEW", "30"))

    def verify(self, token: str) -> dict[str, Any] | None:
        """Verify an OIDC/JWT token and return claims if valid.

        Supports:
        1. Full OIDC JWT (when jwks_url configured) — validates signature via JWKS
        2. HMAC-signed local tokens (dev mode) — validates via shared secret

        Returns:
            Claims dict on success, None on failure

        """
        if not token:
            return None

        # Try OIDC JWT verification if JWKS URL is configured
        if self.jwks_url:
            return self._verify_oidc_jwt(token)

        # Fallback: HMAC-signed local token (dev mode)
        return self._verify_local_token(token)

    def _verify_local_token(self, token: str) -> dict[str, Any] | None:
        """Verify a local HMAC-signed token (dev/fallback mode).

        Token format: base64(json_claims).signature
        """
        import base64

        try:
            parts = token.split(".")
            if len(parts) != 2:
                logger.warning("OIDC: Invalid local token format")
                return None

            payload_b64, signature = parts
            # Verify HMAC
            if not _verify_hmac(payload_b64, signature):
                logger.warning("OIDC: Local token HMAC verification failed")
                return None

            # Decode claims
            # Add padding for base64
            padded = payload_b64 + "=" * (4 - len(payload_b64) % 4)
            claims = json.loads(base64.urlsafe_b64decode(padded))

            # Validate audience
            if claims.get("aud") != self.audience:
                logger.warning(f"OIDC: Audience mismatch: {claims.get('aud')} != {self.audience}")
                return None

            # Validate expiry
            exp = claims.get("exp", 0)
            now = int(datetime.now(UTC).timestamp())
            if now > exp + self._clock_skew_seconds:
                logger.warning("OIDC: Token expired")
                return None

            return claims

        except (ValueError, TypeError, UnicodeDecodeError) as e:
            # Malformed base64/JSON, a non-dict payload, or a bad encoding are
            # all invalid-token conditions, not crashes. Unexpected exception
            # types propagate so a real defect is not masked as a bad token.
            logger.error(f"OIDC: Local token verification error: {e}")
            return None

    def _verify_oidc_jwt(self, token: str) -> dict[str, Any] | None:
        """Verify a full OIDC JWT against JWKS endpoint.

        Requires: PyJWT + requests/httpx for JWKS fetch + signature verification.
        Returns None if dependencies are missing or verification fails.
        """
        try:
            import jwt
        except ImportError:
            logger.warning(
                "OIDC: PyJWT not installed — cannot verify OIDC tokens. Install: pip install PyJWT"
            )
            return None

        try:
            # Decode header to get key ID
            unverified_header = jwt.get_unverified_header(token)
            kid = unverified_header.get("kid")

            # Fetch JWKS and find signing key
            signing_key = self._get_signing_key(kid)
            if signing_key is None:
                logger.warning(f"OIDC: No signing key found for kid={kid}")
                return None

            # Verify and decode
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer or None,
                options={"verify_exp": True, "verify_aud": True},
            )
            return claims

        except jwt.ExpiredSignatureError:
            logger.warning("OIDC: JWT token expired")
            return None
        except jwt.InvalidAudienceError:
            logger.warning(f"OIDC: Invalid audience (expected {self.audience})")
            return None
        except jwt.InvalidTokenError as e:
            logger.warning(f"OIDC: JWT verification failed: {e}")
            return None
        except (ValueError, TypeError, OSError) as e:
            # get_unverified_header / decode can raise ValueError (bad JWT
            # structure, malformed base64) and TypeError (kid not a string,
            # signing key construction). Any PyJWT-specific failure is caught
            # above; these narrow families cover the structural cases without
            # masking an unrelated defect.
            logger.error(f"OIDC: JWT verification error: {e}")
            return None

    def _get_signing_key(self, kid: str | None) -> Any | None:
        """Fetch signing key from JWKS endpoint.

        Caches keys for 5 minutes to reduce network calls.
        """
        if not self.jwks_url:
            return None
        try:
            import httpx
        except ImportError:
            try:
                import requests as httpx  # type: ignore[no-redef]
            except ImportError:
                logger.warning("OIDC: httpx/requests not available for JWKS fetch")
                return None

        try:
            resp = httpx.get(self.jwks_url, timeout=5)
            resp.raise_for_status()  # surface HTTP errors as httpx/requests exceptions
            jwks = resp.json() if hasattr(resp, "json") else {}
            for key in jwks.get("keys", []):
                if key.get("kid") == kid:
                    import jwt.algorithms

                    algo = jwt.algorithms.RSAAlgorithm
                    return algo.from_jwk(json.dumps(key))
        except (ValueError, TypeError, OSError) as e:
            # Network/JSON/type failures during the JWKS fetch are transient
            # (a key cache miss, a bad response, a missing JWK), not crashes.
            # requests.RequestException is an OSError subclass (covered by
            # OSError above); httpx.HTTPError inherits directly from Exception
            # so it must be caught explicitly here.
            logger.error(f"OIDC: JWKS fetch error: {e}")
        except httpx.HTTPError as e:
            # httpx network/status failures (ConnectError, TimeoutException,
            # HTTPStatusError, RequestError) — all httpx.HTTPError subclasses.
            logger.error(f"OIDC: JWKS fetch error (httpx): {e}")
        return None

    def generate_local_token(self, subject: str, claims: dict[str, Any] | None = None) -> str:
        """Generate a local HMAC-signed token for dev/testing.

        Not for production use — production should use an OIDC provider.
        """
        import base64

        now = int(datetime.now(UTC).timestamp())
        payload = {
            "sub": subject,
            "aud": self.audience,
            "iat": now,
            "exp": now + 3600,  # 1 hour
            **(claims or {}),
        }
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        signature = _compute_hmac(payload_b64)
        return f"{payload_b64}.{signature}"


# ---------------------------------------------------------------------------
# RBAC Policy Engine (Phase 3: Zero-Trust Authorization)
# ---------------------------------------------------------------------------


class RBACPolicy:
    """Role-Based Access Control policy engine for A2A protocol.

    Policies are defined as:
    - role_bindings: {identity -> role}
    - role_permissions: {role -> set of (resource_pattern, action_pattern)}

    Security posture (C02, README remediation):
    - The shipped "admin" role is bound to ``*:*`` (all actions on all
      resources) — an EXPLICIT, deployment-time decision.
    - An UNBOUND identity defaults to the least-privileged "observer"
      role (read-only ``a2a:get``). There is NO implicit admin: admin
      privileges are reached only by an explicit ``bind_role(identity,
      "admin")`` call. This is deny-by-default for any identity the
      operator has not deliberately granted a role.
    - ``unbind()`` returns an identity to the deny-by-default observer
      posture with no residual capability.
    """

    def __init__(
        self,
        role_bindings: dict[str, str] | None = None,
        role_permissions: dict[str, list[dict[str, str]]] | None = None,
    ):
        self.role_bindings: dict[str, str] = role_bindings or {}
        self.role_permissions: dict[str, list[dict[str, str]]] = role_permissions or {
            "admin": [
                {"resource": "*", "action": "*"},
            ],
            "agent": [
                {"resource": "*", "action": "a2a:register"},
                {"resource": "*", "action": "a2a:send"},
                {"resource": "*", "action": "a2a:get"},
                {"resource": "*", "action": "a2a:cancel"},
            ],
            "observer": [
                {"resource": "*", "action": "a2a:get"},
            ],
        }

    def bind_role(self, identity: str, role: str) -> None:
        """Bind an identity to a role."""
        self.role_bindings[identity] = role

    def unbind(self, identity: str) -> None:
        """Remove an identity's role binding."""
        self.role_bindings.pop(identity, None)

    def get_role(self, identity: str) -> str:
        """Get the role for an identity (defaults to 'observer')."""
        return self.role_bindings.get(identity, "observer")

    def check(self, identity: str, action: str, resource: str) -> bool:
        """Check if an identity has permission for an action on a resource.

        Args:
            identity: The subject (agent ID, user, service account)
            action: The action being requested (e.g., "a2a:register", "a2a:send")
            resource: The target resource (e.g., agent name)

        Returns:
            True if authorized, False otherwise

        """
        role = self.get_role(identity)
        permissions = self.role_permissions.get(role, [])

        for perm in permissions:
            if self._match(perm.get("resource", ""), resource) and self._match(
                perm.get("action", ""), action
            ):
                return True

        logger.warning(
            f"RBAC denied: identity={identity} role={role} action={action} resource={resource}"
        )
        return False

    @staticmethod
    def _match(pattern: str, value: str) -> bool:
        """Match a pattern against a value. Supports '*' and trailing ':*' wildcards.

        Also supports a ":" as a soft separator so that permission vocab like
        "a2a:send" matches the action namespace "a2a:tasks/send" (D4 — the
        2026-06-11 Fable 5 DD). The check splits both pattern and value on
        ":" and compares the joined-by-":" head segments: "a2a:send" splits
        to ["a2a", "send"]; "a2a:tasks/send" splits to ["a2a", "tasks", "send"];
        the head two segments match as "a2a" + "send" == "a2a" + "send".
        """
        if pattern == "*":
            return True
        if pattern == value:
            return True
        # Support "a2a:*" style wildcard — pattern prefix match
        if pattern.endswith(":*") and value.startswith(pattern[:-1]):
            return True
        # D4 fix: cross-namespace match when pattern and value share the
        # "a2a:" prefix and the leaf segments align. Compare the trailing
        # ":"-separated token on the pattern against the trailing
        # "/" or ":"-separated token on the value.
        if ":" in pattern and ("/" in value or ":" in value):
            pat_leaf = pattern.rsplit(":", 1)[-1]
            val_leaf = value.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
            if pat_leaf == val_leaf and pattern.split(":", 1)[0] == value.split(":", 1)[0]:
                return True
        return False


class A2AGateway:
    """
    A2A Gateway for routing between multiple agents.

    Maintains registry of available agents and routes messages.
    Requires HMAC authentication for agent registration.
    Supports OIDC token verification and RBAC policy enforcement (v13.4).
    """

    def __init__(self, auth_secret: str | None = None, rbac_policy: RBACPolicy | None = None):
        self.agents: dict[str, AgentCard] = {}
        self._auth_secret = auth_secret or _A2A_AUTH_SECRET
        self._lock = asyncio.Lock()
        self._rbac = rbac_policy or RBACPolicy()
        self._oidc = OIDCVerifier()

    async def register_agent(
        self,
        agent_card: AgentCard,
        auth_token: str | None = None,
        oidc_token: str | None = None,
    ) -> None:
        """Register an agent with the gateway.

        Requires HMAC authentication via auth_token, or OIDC bearer token.
        OIDC token is verified first; falls back to HMAC if not provided.
        """
        if oidc_token:
            claims = self._oidc.verify(oidc_token)
            if claims is None:
                raise PermissionError(
                    f"Agent registration failed: invalid OIDC token for {agent_card.name}"
                )
            # RBAC check: does this identity have permission to register?
            identity = claims.get("sub", "unknown")
            if not self._rbac.check(identity, "a2a:register", agent_card.name):
                raise PermissionError(
                    f"RBAC denied: {identity} cannot register agent {agent_card.name}"
                )
        elif auth_token is None or not _verify_auth_token(auth_token):
            # No OIDC was provided → must have a valid HMAC auth_token.
            # NOTE: prior v13.21.5 had `elif auth_token and not _verify_auth_token(...)`,
            # which silently allowed registration with NO token at all (D2 in the
            # 2026-06-11 Fable 5 DD). This version unconditionally requires a valid
            # auth_token when OIDC is not used.
            raise PermissionError(
                f"Agent registration failed: invalid or missing auth token for {agent_card.name}"
            )
        async with self._lock:
            self.agents[agent_card.name] = agent_card
        logger.info(f"Registered agent: {agent_card.name}")

    async def unregister_agent(self, agent_name: str) -> None:
        """Unregister an agent."""
        async with self._lock:
            self.agents.pop(agent_name, None)
        logger.info(f"Unregistered agent: {agent_name}")

    async def find_agents_by_capability(self, capability: str) -> list[AgentCard]:
        """Find agents with a specific capability."""
        async with self._lock:
            return [card for card in self.agents.values() if capability in card.capabilities]

    async def find_agents_by_skill(self, skill: str) -> list[AgentCard]:
        """Find agents with a specific skill."""
        async with self._lock:
            return [card for card in self.agents.values() if skill in card.skills]

    async def route_message(
        self,
        message: A2AMessage,
        oidc_token: str | None = None,
    ) -> A2AMessage:
        """Route a message to the target agent with RBAC enforcement.

        Args:
            message: A2A JSON-RPC message to route
            oidc_token: Optional OIDC bearer token for authz

        Returns:
            Response message from target agent

        """
        identity = "anonymous"
        if oidc_token:
            claims = self._oidc.verify(oidc_token)
            if claims is None:
                return A2AMessage(
                    id=message.id,
                    error={"code": -32600, "message": "Invalid OIDC token"},
                )
            identity = claims.get("sub", "unknown")

        # Extract target from params
        target_agent = (message.params or {}).get("agent", "")
        action = message.method or "unknown"

        if not self._rbac.check(identity, f"a2a:{action}", target_agent):
            return A2AMessage(
                id=message.id,
                error={
                    "code": -32603,
                    "message": f"RBAC denied: {identity} cannot {action} on {target_agent}",
                },
            )

        # Find and dispatch to agent
        async with self._lock:
            agent_card = self.agents.get(target_agent)
        if agent_card is None:
            return A2AMessage(
                id=message.id,
                error={"code": -32601, "message": f"Agent not found: {target_agent}"},
            )

        # Return routing result — the caller dispatches to the agent handler
        return A2AMessage(
            id=message.id,
            result={
                "routed": True,
                "target": target_agent,
                "endpoint": agent_card.endpoint,
                "capabilities": agent_card.capabilities,
            },
        )


def create_beagle_agent_card(
    name: str = "beagle-agent",
    description: str = "Beagle autonomous agent",
    skills: list[str] | None = None,
) -> AgentCard:
    """Create an Beagle agent card."""
    return AgentCard(
        name=name,
        version="1.0.0",
        description=description,
        capabilities=[
            "code_improvement",
            "self_improvement",
            "multi_agent_orchestration",
            "adversarial_validation",
            "checkpointing",
        ],
        skills=skills
        or [
            "python",
            "code_analysis",
            "testing",
            "refactoring",
        ],
        endpoint="http://localhost:8081",
    )


if __name__ == "__main__":

    async def example_handler(data: dict[str, Any]) -> dict[str, Any]:
        return {"result": f"Processed: {data}"}

    agent_card = create_beagle_agent_card()
    agent = A2AAgent("beagle-1", agent_card, example_handler)

    logger.info("A2A Agent initialized")
    logger.info(f"Agent Card: {json.dumps(agent_card.to_dict(), indent=2)}")
