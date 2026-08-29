"""Tests for the D-02 / D-14 webui security fixes.

The defect was that ``build_app()`` registered eight routes and no
authentication middleware, so ``POST /api/workflows/{id}/execute`` reached
``run_workflow()`` with a caller-supplied goal and budget. ``float("inf")``
was accepted as a budget, and the default bind was 0.0.0.0.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from beagle.frontends.webui import server


@pytest_asyncio.fixture
async def client() -> AsyncIterator[TestClient]:
    app = server.build_app()
    test_server = TestServer(app)
    async with test_server:
        client = TestClient(test_server)
        yield client


@pytest.mark.asyncio
async def test_unauthenticated_request_is_401(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """D-02: with a token configured, a request without it is rejected."""
    monkeypatch.setattr(server, "_webui_token", lambda: "secret-token")
    resp = await client.get("/api/system/status")
    assert resp.status == 401
    body = await resp.text()
    assert "Unauthorized" in body


@pytest.mark.asyncio
async def test_authenticated_request_is_200(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """D-02: the correct bearer token passes the middleware."""
    monkeypatch.setattr(server, "_webui_token", lambda: "secret-token")
    resp = await client.get(
        "/api/system/status",
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status == 200


@pytest.mark.asyncio
async def test_wrong_token_is_401(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    """D-02: a wrong token is rejected, compared with hmac.compare_digest."""
    monkeypatch.setattr(server, "_webui_token", lambda: "secret-token")
    resp = await client.get(
        "/api/system/status",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status == 401


def test_non_loopback_bind_without_token_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D-02: main() must refuse to start on 0.0.0.0 with no token."""
    monkeypatch.setattr(server, "_webui_token", lambda: "")
    monkeypatch.setenv("BEAGLE_WEBUI_HOST", "0.0.0.0")
    # _bundle_dir() would start the server; stub it so main() exits at the
    # refuse-to-start check before any listener opens.
    monkeypatch.setattr(server, "_bundle_dir", lambda: server.Path("."))
    rc = server.main()
    assert rc == 1
    captured = capsys.readouterr()
    assert "refusing to bind" in captured.err
    assert "BEAGLE_WEBUI_TOKEN" in captured.err


def test_non_loopback_bind_with_token_does_not_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-02: with a token set, a non-loopback bind is allowed to proceed."""
    monkeypatch.setattr(server, "_webui_token", lambda: "t")
    monkeypatch.setenv("BEAGLE_WEBUI_HOST", "0.0.0.0")
    monkeypatch.setattr(server, "_bundle_dir", lambda: server.Path("."))
    # web.run_app blocks; stub it so main() returns 0 past the refusal check.
    monkeypatch.setattr(server.web, "run_app", lambda *_a, **_k: None)
    assert server.main() == 0


def test_loopback_bind_without_token_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-02: loopback binds are the safe default and need no token."""
    monkeypatch.setattr(server, "_webui_token", lambda: "")
    monkeypatch.setenv("BEAGLE_WEBUI_HOST", "127.0.0.1")
    monkeypatch.setattr(server, "_bundle_dir", lambda: server.Path("."))
    monkeypatch.setattr(server.web, "run_app", lambda *_a, **_k: None)
    assert server.main() == 0


@pytest.mark.asyncio
async def test_budget_inf_returns_400(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    """D-14: float("inf") is not a budget — 400, not 500."""
    monkeypatch.setattr(server, "_webui_token", lambda: "t")
    resp = await client.post(
        "/api/workflows/test/execute",
        headers={"Authorization": "Bearer t"},
        json={"goal": "x", "budgetLimitUsd": "inf"},
    )
    assert resp.status == 400
    body = await resp.json()
    assert "finite" in body["error"].lower()


@pytest.mark.asyncio
async def test_budget_nan_returns_400(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    """D-14: NaN is rejected like infinity."""
    monkeypatch.setattr(server, "_webui_token", lambda: "t")
    resp = await client.post(
        "/api/workflows/test/execute",
        headers={"Authorization": "Bearer t"},
        json={"goal": "x", "budgetLimitUsd": "nan"},
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_budget_negative_returns_400(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """D-14: a negative budget is refused, not silently spent."""
    monkeypatch.setattr(server, "_webui_token", lambda: "t")
    resp = await client.post(
        "/api/workflows/test/execute",
        headers={"Authorization": "Bearer t"},
        json={"goal": "x", "budgetLimitUsd": -5.0},
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_valid_budget_passes_auth(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """D-14: a finite non-negative budget is accepted (201 from the handler)."""
    monkeypatch.setattr(server, "_webui_token", lambda: "t")
    resp = await client.post(
        "/api/workflows/test/execute",
        headers={"Authorization": "Bearer t"},
        json={"goal": "x", "budgetLimitUsd": 25.0},
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["budgetLimitUsd"] == 25.0
