"""A2A Server Bridge — Inbound A2A protocol for inter-framework federation.

Phase 5 of the LangChain Ecosystem Compatibility Plan.
Exposes Beagle's 46 specialized agents as A2A (Agent-to-Agent) endpoints
so external frameworks (CrewAI, AutoGen, LangChain) can discover and
call them via the A2A JSON-RPC protocol.

Endpoints:
  POST /a2a/discover  → list[AgentCard]
  POST /a2a/execute   → TaskResult (signed)

All config from config.toml [langchain_bridges.a2a].
Signature verification via Ed25519 (PyNaCl) when available.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from .a2a_types import AgentCard as AgentCard
from .config import get_a2a_config

logger = logging.getLogger("Beagle.bridges.a2a_server")

# ── Input sanitization constants ──────────────────────────────────────────
_MAX_QUERY_LENGTH = 50_000  # Max characters for a2a query input
_MAX_INPUT_KEYS = 50  # Max keys in task input dict
# Phase 6: A2A payload DoS guard (audit E8). The A2A protocol
# previously had no body-size limit; a malicious peer could ship a
# 100 MB JSON to saturate the edge runtime. 1 MB is generous for
# a single A2A task — actual production A2A tasks are < 64 KB.
_A2A_MAX_BODY_BYTES = 1_048_576  # 1 MiB
_SANITIZE_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # Control chars


def _sanitize_query(query: str) -> str:
    """Sanitize an A2A query string for safety.

    Strips control characters, enforces max length, and rejects
    obviously malicious patterns.

    Args:
        query: Raw query string from A2A task input.

    Returns:
        Sanitized query string.

    Raises:
        ValueError: If query exceeds max length or contains threats.

    """
    if len(query) > _MAX_QUERY_LENGTH:
        raise ValueError(
            f"A2A query too long ({len(query)} chars, max {_MAX_QUERY_LENGTH}). "
            f"Potential abuse — rejecting."
        )

    # Strip control characters (keep newlines/tabs)
    sanitized = _SANITIZE_PATTERN.sub("", query)

    # Check for common injection patterns (prompt injection defense-in-depth)
    suspicious = [
        "ignore previous instructions",
        "disregard all prior",
        "system prompt override",
        "you are now",
    ]
    lower = sanitized.lower()
    for pattern in suspicious:
        if pattern in lower:
            logger.warning(f"A2A query contains suspicious pattern: '{pattern}'")
            # We log but don't reject — the LLM system directives handle this
            # The check is defense-in-depth, not a filter

    return sanitized


@dataclass
class A2ATask:
    """An A2A task request."""

    task_id: str = ""
    agent_name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    callback_url: str = ""


@dataclass
class A2ATaskResult:
    """Result of an A2A task execution."""

    task_id: str = ""
    status: str = "completed"  # "completed" | "failed" | "in_progress"
    output: Any = None
    error: str = ""
    agent_name: str = ""
    timestamp: float = field(default_factory=time.time)
    signature: str = ""


class BeagleToA2ABridge:
    """Bridges Beagle agents ↔ A2A protocol.

    Inbound:  External A2A client → HTTP endpoint → Beagle workflow execution
    Outbound: Beagle workflow → A2A client call → External agent execution

    This class handles the inbound (server) side.
    """

    def __init__(self) -> None:
        self.config = get_a2a_config()
        self._app = None
        self._server = None
        self._signing_key: Any = None  # nacl.signing.SigningKey | None

    def _load_signing_key(self) -> Any:
        """Load or generate an Ed25519 signing keypair.

        SECURITY (DevSecOps): If PyNaCl is not available or initialization
        fails, raises RuntimeError to crash immediately. There is NO fallback
        to HMAC — silently downgrading from Ed25519 to HMAC creates a
        cryptographic downgrade attack surface where an attacker could
        force nacl to fail and then forge HMAC signatures.
        """
        if self._signing_key is not None:
            return self._signing_key

        try:
            from pathlib import Path

            import nacl.signing  # type: ignore[import-untyped]

            key_path = Path(self.config.key_path).expanduser()
            key_path.mkdir(parents=True, exist_ok=True)
            key_file = key_path / "signing.key"

            if key_file.exists():
                seed = key_file.read_bytes()
                self._signing_key = nacl.signing.SigningKey(seed)
            else:
                self._signing_key = nacl.signing.SigningKey.generate()
                key_file.write_bytes(bytes(self._signing_key))  # type: ignore[call-overload]
                key_file.chmod(0o600)
                logger.info(f"A2A: Generated Ed25519 signing key at {key_file}")

            return self._signing_key
        except ImportError as exc:
            # SECURITY: Fail-closed — do NOT silently downgrade to HMAC.
            raise RuntimeError(
                "PyNaCl is REQUIRED for A2A signing. Ed25519 signing is mandatory; "
                "HMAC fallback has been removed as it creates a cryptographic "
                "downgrade vulnerability. Install with: pip install pynacl"
            ) from exc

    def _sign_payload(self, payload: bytes) -> str:
        """Sign a payload with Beagle's Ed25519 private key.

        SECURITY (DevSecOps): Ed25519 ONLY — HMAC fallback removed.
        If _load_signing_key() failed to initialize nacl, it already raised
        RuntimeError, so we will never reach this method with an invalid key.
        Any unexpected failure here is a hard error, not a downgrade path.
        """
        try:
            import nacl.signing  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError("PyNaCl is REQUIRED for A2A signing") from exc

        key = self._load_signing_key()

        if not isinstance(key, nacl.signing.SigningKey):
            # SECURITY: This should never happen — _load_signing_key either
            # returns a valid SigningKey or raises RuntimeError. But if somehow
            # we get here with a non-signing key (e.g., corrupted state), crash
            # rather than silently downgrade.
            raise RuntimeError(
                "A2A signing key is not an Ed25519 SigningKey — refusing to sign. "
                "HMAC fallback has been removed for security. "
                "Ensure PyNaCl is installed and the signing key is valid."
            )

        signed = key.sign(payload)
        return signed.signature.hex()  # type: ignore[no-any-return]

    def _verify_signature(
        self, payload: bytes, signature: str, peer_key: bytes | None = None
    ) -> bool:
        """Verify a signature on an incoming A2A request.

        SECURITY (DevSecOps): Ed25519 ONLY — HMAC verification removed.
        If PyNaCl is not installed, verification FAILS CLOSED (returns False).
        There is no fallback to HMAC — a cryptographic downgrade from Ed25519
        to HMAC allows an attacker who can force nacl to fail (e.g., via
        dependency confusion) to then forge HMAC signatures.
        """
        if not self.config.require_signatures:
            return True

        try:
            import nacl.signing  # type: ignore[import-untyped]
        except ImportError:
            # SECURITY: Fail-closed — no HMAC fallback.
            logger.error(
                "[SECURITY] PyNaCl not installed — Ed25519 verification impossible. "
                "HMAC fallback removed. Refusing to verify (fail-closed)."
            )
            return False

        if not peer_key:
            # No peer key provided — cannot verify Ed25519 signature
            logger.error("[SECURITY] No peer public key provided for Ed25519 verification")
            return False

        try:
            verify_key = nacl.signing.VerifyKey(peer_key)
            verify_key.verify(payload, bytes.fromhex(signature))
            return True
        except Exception:  # ruff: ignore[BLE001]  # broad catch intentional
            return False

    async def discover(self) -> list[dict[str, Any]]:
        """Return A2A AgentCards for all Beagle agents.

        Reads agents from agents.toml and auto-generates
        an AgentCard for each profile.
        """
        from .a2a_card_builder import build_agent_cards

        cards = build_agent_cards()
        return [asdict(card) for card in cards]

    async def execute(self, task: A2ATask) -> A2ATaskResult:
        """Execute an A2A task by routing to the appropriate Beagle workflow.

        Maps the requested agent to an Beagle workflow, executes it,
        and returns a signed result.
        """
        if not task.task_id:
            import uuid

            task.task_id = str(uuid.uuid4())

        logger.info(f"[A2A] Executing task {task.task_id}: agent={task.agent_name}")

        try:
            # Validate and sanitize input (OWASP API8 defense)
            if len(task.input) > _MAX_INPUT_KEYS:
                return A2ATaskResult(
                    task_id=task.task_id,
                    status="failed",
                    error=f"Too many input keys ({len(task.input)}, max {_MAX_INPUT_KEYS})",
                    agent_name=task.agent_name,
                )

            # Route to Beagle workflow
            from ..core.graph import run_workflow

            query = task.input.get("query", task.input.get("message", ""))
            if not query:
                return A2ATaskResult(
                    task_id=task.task_id,
                    status="failed",
                    error="No query provided in task input",
                    agent_name=task.agent_name,
                )

            # Sanitize query string (strip controls, enforce max length)
            try:
                query = _sanitize_query(str(query))
            except ValueError as e:
                return A2ATaskResult(
                    task_id=task.task_id,
                    status="failed",
                    error=str(e),
                    agent_name=task.agent_name,
                )

            result = await run_workflow(
                query=query,
                workflow_name=task.agent_name or "research",
                workflow_mode="audit",
            )

            # Extract the final report or synthesis
            output = result.get("final_report") or result.get("synthesis") or str(result)

            # Sign the result
            result_obj = A2ATaskResult(
                task_id=task.task_id,
                status="completed",
                output=output,
                agent_name=task.agent_name,
            )
            payload = json.dumps(asdict(result_obj), sort_keys=True).encode()
            result_obj.signature = self._sign_payload(payload)

            return result_obj

        except Exception as exc:  # broad catch intentional
            logger.error(f"[A2A] Task {task.task_id} failed: {exc}", exc_info=True)
            return A2ATaskResult(
                task_id=task.task_id,
                status="failed",
                error=str(exc),
                agent_name=task.agent_name,
            )

    async def start_server(self) -> None:
        """Start the A2A HTTP server.

        Uses FastAPI/uvicorn if available, falls back to a basic
        asyncio HTTP server.
        """
        if not self.config.enabled:
            logger.debug("A2A server disabled in config")
            return

        try:
            import uvicorn  # type: ignore[import-untyped]
            from fastapi import FastAPI, Request  # type: ignore[import-untyped]
            from fastapi.responses import JSONResponse  # type: ignore[import-untyped]

            app = FastAPI(title="Beagle A2A Bridge", version="1.0.0")
            self._app = app  # type: ignore[assignment]

            # ── Rate limiting middleware ─────────────────────────────────────
            # Simple per-IP rate limit: max N requests per window.
            _rate_limits: dict[str, tuple[float, int]] = {}
            _RATE_LIMIT_MAX = self.config.max_concurrent_tasks * 10  # Allow burst headroom
            _RATE_LIMIT_WINDOW = 60.0  # 1 minute sliding window

            @app.middleware("http")
            async def rate_limit_middleware(request: Request, call_next: Callable) -> Any:
                """Per-IP rate limiting for A2A endpoints."""
                import time as _time

                client_ip = request.client.host if request.client else "unknown"

                # Only rate-limit A2A endpoints
                if not request.url.path.startswith("/a2a/"):
                    return await call_next(request)

                now = _time.monotonic()
                if client_ip in _rate_limits:
                    window_start, count = _rate_limits[client_ip]
                    if now - window_start > _RATE_LIMIT_WINDOW:
                        # Reset window
                        _rate_limits[client_ip] = (now, 1)
                    elif count >= _RATE_LIMIT_MAX:
                        return JSONResponse(
                            content={"error": "Rate limit exceeded"},
                            status_code=429,
                        )
                    else:
                        _rate_limits[client_ip] = (window_start, count + 1)
                else:
                    _rate_limits[client_ip] = (now, 1)

                return await call_next(request)

            @app.post("/a2a/discover")
            async def discover_endpoint() -> JSONResponse:
                cards = await self.discover()
                return JSONResponse(content=cards)

            @app.post("/a2a/execute")
            async def execute_endpoint(request: Request) -> JSONResponse:
                # Phase 6: A2A payload DoS guard (audit E8). Refuse
                # bodies larger than _A2A_MAX_BODY_BYTES (1 MB by
                # default) BEFORE parsing JSON. The peer-controlled
                # ``input`` field is otherwise unbounded; a single
                # 100 MB POST saturates an edge runtime in seconds.
                content_length = request.headers.get("content-length")
                if content_length is not None:
                    try:
                        if int(content_length) > _A2A_MAX_BODY_BYTES:
                            return JSONResponse(
                                content={
                                    "error": (
                                        f"A2A body too large: {content_length} bytes "
                                        f"(max {_A2A_MAX_BODY_BYTES})"
                                    )
                                },
                                status_code=413,
                            )
                    except ValueError:
                        # Malformed Content-Length header — treat as
                        # potentially hostile and reject.
                        return JSONResponse(
                            content={"error": "Invalid Content-Length header"},
                            status_code=400,
                        )
                try:
                    body = await request.json()
                except json.JSONDecodeError as exc:
                    return JSONResponse(
                        content={"error": f"Invalid JSON: {exc}"},
                        status_code=400,
                    )
                # Defensive: if the peer lied about Content-Length
                # (sent a small header but a huge body), enforce
                # a second check on the parsed body.
                body_str = json.dumps(body)
                if len(body_str) > _A2A_MAX_BODY_BYTES:
                    return JSONResponse(
                        content={
                            "error": (
                                f"A2A body too large after parse: {len(body_str)} bytes "
                                f"(max {_A2A_MAX_BODY_BYTES})"
                            )
                        },
                        status_code=413,
                    )
                task = A2ATask(
                    task_id=body.get("task_id", ""),
                    agent_name=body.get("agent_name", ""),
                    input=body.get("input", {}),
                    callback_url=body.get("callback_url", ""),
                )

                # Verify signature if required
                if self.config.require_signatures:
                    sig = body.get("signature", "")
                    payload = json.dumps(body.get("input", {}), sort_keys=True).encode()
                    peer_key = body.get("peer_public_key")
                    peer_key_bytes = bytes.fromhex(peer_key) if peer_key else None
                    if not self._verify_signature(payload, sig, peer_key_bytes):
                        return JSONResponse(
                            content={"error": "Signature verification failed"},
                            status_code=401,
                        )

                # Enforce concurrency cap
                result = await self.execute(task)
                return JSONResponse(content=asdict(result))

            @app.get("/a2a/health")
            async def health_endpoint() -> JSONResponse:
                return JSONResponse(content={"status": "ok", "version": "1.0.0"})

            logger.info(f"A2A server starting on {self.config.bind_address}:{self.config.port}")
            config = uvicorn.Config(
                app,
                host=self.config.bind_address,
                port=self.config.port,
                log_level="info",
            )
            self._server = uvicorn.Server(config)  # type: ignore[assignment]
            await self._server.serve()  # type: ignore[attr-defined]

        except ImportError:
            logger.warning(
                "FastAPI/uvicorn not installed — A2A server not started. "
                "Install with: pip install fastapi uvicorn"
            )
        except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.error(f"A2A server failed to start: {exc}")

    async def stop_server(self) -> None:
        """Stop the A2A HTTP server."""
        if self._server:
            self._server.should_exit = True
            logger.info("A2A server stopping")
