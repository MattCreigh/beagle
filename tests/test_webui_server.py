"""Tests for the Beagle webui server (real Beagle-backed aiohttp dashboard).

Covers bundle serving, the live-data API adapters, and the SPA fallback.
"""

from __future__ import annotations

from typing import AsyncIterator

import pytest
import pytest_asyncio
from aiohttp import web
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
async def test_serves_index_html(client: TestClient) -> None:
    resp = await client.get("/")
    assert resp.status == 200
    body = await resp.text()
    assert "<!DOCTYPE html>" in body
    assert "id=\"root\"" in body


@pytest.mark.asyncio
async def test_serves_bundle_asset(client: TestClient) -> None:
    dist = server._dist_dir()
    js = (dist / "assets").glob("index-*.js")
    asset_names = [p.name for p in js]
    assert asset_names, "no built JS asset found in webui-prebuild/dist/assets"
    resp = await client.get(f"/assets/{asset_names[0]}")
    assert resp.status == 200
    assert resp.headers.get("Content-Type") == "application/javascript"


@pytest.mark.asyncio
async def test_system_status_returns_live_shape(client: TestClient) -> None:
    resp = await client.get("/api/system/status")
    assert resp.status == 200
    data = await resp.json()
    for key in (
        "beaconStatus",
        "sandboxEngine",
        "governanceMode",
        "contextCompactionRatio",
        "totalSpendToday",
        "budgetCeiling",
        "activeRunsCount",
        "totalHistoricalRuns",
    ):
        assert key in data, f"missing key {key}"


@pytest.mark.asyncio
async def test_workflows_endpoint(client: TestClient) -> None:
    resp = await client.get("/api/workflows")
    assert resp.status == 200
    data = await resp.json()
    # list_workflows may return [] if no metaprompts are present, but the
    # payload must still be a list (the contract the frontend expects).
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_runs_endpoint(client: TestClient) -> None:
    resp = await client.get("/api/runs")
    assert resp.status == 200
    data = await resp.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_cost_summary_endpoint(client: TestClient) -> None:
    resp = await client.get("/api/cost/summary")
    assert resp.status == 200
    data = await resp.json()
    for key in ("dailyBudgetCap", "currentDaySpend", "remainingBudget", "governancePolicy"):
        assert key in data, f"missing key {key}"


@pytest.mark.asyncio
async def test_agents_roster_endpoint(client: TestClient) -> None:
    resp = await client.get("/api/agents/roster")
    assert resp.status == 200
    data = await resp.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_rag_search_returns_list(client: TestClient) -> None:
    # RAG may not be indexed in CI; the endpoint must degrade to a valid
    # payload with a results list, never 500.
    resp = await client.get("/api/rag/search?q=autonomous")
    assert resp.status == 200
    data = await resp.json()
    assert "results" in data
    assert isinstance(data["results"], list)


@pytest.mark.asyncio
async def test_unknown_route_falls_back_to_spa(client: TestClient) -> None:
    # Any unmatched non-API path returns the SPA index (client-side routing).
    resp = await client.get("/workflows/runs")
    assert resp.status == 200
    body = await resp.text()
    assert "id=\"root\"" in body
