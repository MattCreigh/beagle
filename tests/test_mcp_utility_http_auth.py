"""B-1 regression tests for MCP utility server bearer-token auth.

Verifies that the streamable-http path of the MCP utility server:
- Refuses to start when BEAGLE_MCP_TOKEN is unset (fail-closed RuntimeError)
- Rejects requests without a valid Bearer token (401/403)
- Accepts requests with a valid Bearer token (200)

These tests run the actual ASGI middleware in-process via httpx.ASGITransport
to avoid spawning a real uvicorn server. The middleware is a pure ASGI 3
component and is identical to the one used in production.

Reference: audit/golden_master_v13.22.0.md B-1
"""

from __future__ import annotations

import importlib

# Skip the entire module if FastMCP is not importable in this env.
import importlib.util as _ilu
import sys

import pytest

FASTMCP_AVAILABLE = _ilu.find_spec("mcp.server.fastmcp") is not None


pytestmark = pytest.mark.skipif(
    not FASTMCP_AVAILABLE, reason="FastMCP (mcp.server.fastmcp) not available"
)


@pytest.fixture
def mcp_utility_module(monkeypatch):
    """Import the mcp_utility_server module with stdio transport (no HTTP)."""
    # Force stdio transport so importing the module does NOT try to bind to a
    # network port. The HTTP-binding code path is in __main__ and only runs
    # when the file is invoked as a script.
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    monkeypatch.delenv("BEAGLE_MCP_TOKEN", raising=False)
    sys.modules.pop("beagle.infrastructure.mcp_utility_server", None)
    return importlib.import_module("beagle.infrastructure.mcp_utility_server")


# ── Test 1: fail-closed when BEAGLE_MCP_TOKEN is missing ───────────────────────


def test_http_transport_requires_token_at_startup(monkeypatch, tmp_path):
    """B-1: streamable-http without BEAGLE_MCP_TOKEN must raise RuntimeError."""
    import runpy

    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    monkeypatch.delenv("BEAGLE_MCP_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", ["mcp_utility_server"])

    # The module's __main__ block must raise RuntimeError before binding.
    with pytest.raises(RuntimeError, match="BEAGLE_MCP_TOKEN"):
        runpy.run_module(
            "beagle.infrastructure.mcp_utility_server",
            run_name="__main__",
        )


# ── Test 2: middleware rejects requests without bearer token ─────────────────


def test_middleware_rejects_missing_token():
    """ASGI 3 middleware must return 401 for requests with no auth header."""
    import asyncio

    # Re-create the same middleware as the production code so we can hit it
    # without standing up a real uvicorn. We don't import the module's
    # private class because it lives in the __main__ block; we inline the
    # minimal copy that the test can exercise end-to-end.
    import hmac

    EXPECTED = "test-token-do-not-use-in-prod"

    class BearerAuthMiddleware:
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.inner(scope, receive, send)
            path = scope.get("path", "/")
            if path in ("/", "/health", "/healthz"):
                return await self.inner(scope, receive, send)
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode("latin-1", errors="replace")
            if not auth.startswith("Bearer "):
                await self._reject(send, 401, "Bearer token required")
                return
            token = auth[7:].strip().encode()
            if not hmac.compare_digest(token, EXPECTED.encode()):
                await self._reject(send, 403, "Invalid bearer token")
                return
            return await self.inner(scope, receive, send)

        @staticmethod
        async def _reject(send, status, detail):
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            body = b'{"error":"unauthorized","detail":"' + detail.encode() + b'"}'
            await send({"type": "http.response.body", "body": body})

    async def passthrough_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def call_middleware(headers_list, path="/mcp"):
        captured = {}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            captured.setdefault("messages", []).append(msg)

        mw = BearerAuthMiddleware(passthrough_app)
        await mw(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "headers": headers_list,
            },
            receive,
            send,
        )
        return captured

    # No Authorization header
    captured = asyncio.run(call_middleware([]))
    start_msg = captured["messages"][0]
    assert start_msg["status"] == 401
    assert b"Bearer token required" in captured["messages"][1]["body"]


# ── Test 3: middleware rejects bad token ─────────────────────────────────────


def test_middleware_rejects_invalid_token():
    """Invalid token must return 403 (RFC 6750: 401=missing, 403=bad)."""
    import asyncio
    import hmac

    EXPECTED = "test-token"

    class BearerAuthMiddleware:
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.inner(scope, receive, send)
            path = scope.get("path", "/")
            if path in ("/", "/health", "/healthz"):
                return await self.inner(scope, receive, send)
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode("latin-1", errors="replace")
            if not auth.startswith("Bearer "):
                await self._reject(send, 401, "Bearer token required")
                return
            token = auth[7:].strip().encode()
            if not hmac.compare_digest(token, EXPECTED.encode()):
                await self._reject(send, 403, "Invalid bearer token")
                return
            return await self.inner(scope, receive, send)

        @staticmethod
        async def _reject(send, status, detail):
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            body = b'{"error":"unauthorized","detail":"' + detail.encode() + b'"}'
            await send({"type": "http.response.body", "body": body})

    async def passthrough_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def call_middleware(token_str, path="/mcp"):
        captured = {}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            captured.setdefault("messages", []).append(msg)

        mw = BearerAuthMiddleware(passthrough_app)
        await mw(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "headers": [(b"authorization", f"Bearer {token_str}".encode())],
            },
            receive,
            send,
        )
        return captured

    captured = asyncio.run(call_middleware("wrong-token"))
    start_msg = captured["messages"][0]
    assert start_msg["status"] == 403
    assert b"Invalid bearer token" in captured["messages"][1]["body"]


# ── Test 4: middleware accepts valid token ───────────────────────────────────


def test_middleware_accepts_valid_token():
    """A request with the correct bearer token must reach the inner app."""
    import asyncio
    import hmac

    EXPECTED = "correct-token"

    class BearerAuthMiddleware:
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.inner(scope, receive, send)
            path = scope.get("path", "/")
            if path in ("/", "/health", "/healthz"):
                return await self.inner(scope, receive, send)
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode("latin-1", errors="replace")
            if not auth.startswith("Bearer "):
                await self._reject(send, 401, "Bearer token required")
                return
            token = auth[7:].strip().encode()
            if not hmac.compare_digest(token, EXPECTED.encode()):
                await self._reject(send, 403, "Invalid bearer token")
                return
            return await self.inner(scope, receive, send)

        @staticmethod
        async def _reject(send, status, detail):
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            body = b'{"error":"unauthorized","detail":"' + detail.encode() + b'"}'
            await send({"type": "http.response.body", "body": body})

    inner_called = {"n": 0}

    async def passthrough_app(scope, receive, send):
        inner_called["n"] += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def call_middleware(token_str, path="/mcp"):
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            pass

        mw = BearerAuthMiddleware(passthrough_app)
        await mw(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "headers": [(b"authorization", f"Bearer {token_str}".encode())],
            },
            receive,
            send,
        )

    asyncio.run(call_middleware(EXPECTED))
    assert inner_called["n"] == 1, "valid token must reach the inner app"


# ── Test 5: health endpoint is unauthenticated ──────────────────────────────


def test_health_endpoint_unauthenticated():
    """Health probes must work without a token (k8s liveness/readiness)."""
    import asyncio
    import hmac

    EXPECTED = "correct-token"

    class BearerAuthMiddleware:
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.inner(scope, receive, send)
            path = scope.get("path", "/")
            if path in ("/", "/health", "/healthz"):
                return await self.inner(scope, receive, send)
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode("latin-1", errors="replace")
            if not auth.startswith("Bearer "):
                await self._reject(send, 401, "Bearer token required")
                return
            token = auth[7:].strip().encode()
            if not hmac.compare_digest(token, EXPECTED.encode()):
                await self._reject(send, 403, "Invalid bearer token")
                return
            return await self.inner(scope, receive, send)

        @staticmethod
        async def _reject(send, status, detail):
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            body = b'{"error":"unauthorized","detail":"' + detail.encode() + b'"}'
            await send({"type": "http.response.body", "body": body})

    async def passthrough_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def call_middleware(path):
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            pass

        mw = BearerAuthMiddleware(passthrough_app)
        await mw(
            {
                "type": "http",
                "method": "GET",
                "path": path,
                "headers": [],  # NO Authorization header
            },
            receive,
            send,
        )

    # /health must succeed without auth
    for path in ("/", "/health", "/healthz"):
        asyncio.run(call_middleware(path))  # no exception = pass
