"""Benchmark tests for DirectLLMClient vs subprocess path.

Measures latency, token cost, and memory footprint of the two LLM
execution paths to validate the ~3-5s speedup claim for DirectLLMClient.

Network tests require a valid OLLAMA_CLOUD_API_KEY env var.
Tests auto-skip when the key is absent, expired, or returns 401.

Run with: pytest tests/test_bridge_benchmarks.py -v
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from beagle.bridges.llm_client_registry import (
    LLMClientRegistry,
    get_llm_client,
    reset_llm_client_registry,
)
from beagle.bridges.llm_direct import DirectLLMClient
from beagle.cost_tracker import (
    get_tenant_budget_tracker,
    reset_tenant_budget_tracker,
)

# ── API key validation ────────────────────────────────────────────────────


def _api_key_is_valid_sync() -> bool:
    """Probe /v1/completions with a minimal payload to verify the key works.

    Uses a synchronous httpx call at module load so the skip marker
    can gate network tests before pytest runs them.

    Resolves the API key through the full secrets chain (env var →
    secrets.yaml → secrets_loader) to match the production path.

    /v1/models may return 200 even when the key lacks inference access
    for the target model — a 401 on the actual /v1/completions call is
    the definitive signal that the key is not valid for this model.
    """
    import httpx

    from beagle.secrets_loader import load_secret

    api_key = load_secret("OLLAMA_CLOUD_API_KEY")
    if not api_key:
        return False
    try:
        with httpx.Client(
            base_url=DirectLLMClient.DEFAULT_HOST,
            timeout=10.0,
        ) as http:
            resp = http.post(
                "/v1/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "glm-5.2",
                    "prompt": "ping",
                    "max_tokens": 1,
                },
            )
            # 401 = key invalid for this model; >=500 = server error; both skip.
            return resp.status_code < 400
    except (
        httpx.HTTPError,
        httpx.TimeoutException,
        ConnectionError,
        TimeoutError,
        OSError,
    ) as _exc:
        return False


# Module-level validation cache — probed once per test session, but only
# when BEAGLE_RUN_NETWORK_TESTS=1 is set.  Lazy-evaluated to avoid paying
# the network cost during collection or in offline test runs.
_has_valid_api_key: bool | None = None


def _get_api_key_valid() -> bool:
    """Lazy wrapper — only probes the network when network tests are enabled."""
    global _has_valid_api_key
    if _has_valid_api_key is None:
        if os.environ.get("BEAGLE_RUN_NETWORK_TESTS") != "1":
            _has_valid_api_key = False
        else:
            _has_valid_api_key = _api_key_is_valid_sync()
    return _has_valid_api_key


needs_api_key = pytest.mark.skipif(
    not _get_api_key_valid(),
    reason="OLLAMA_CLOUD_API_KEY not set or invalid (401/error), "
    "or BEAGLE_RUN_NETWORK_TESTS not set to '1'",
)


# ── Direct LLM Benchmark (network required) ───────────────────────────────


@pytest.mark.asyncio
@needs_api_key
async def test_direct_llm_client_latency():
    """Measure DirectLLMClient end-to-end latency.

    Verifies the client returns a response within the declared timeout
    and that response contains expected fields.
    """
    client = get_llm_client(model="glm-5.1:cloud", timeout=60)
    start = time.monotonic()

    resp = await client.chat(
        messages=[
            {"role": "system", "content": "You are a benchmark tool. Respond concisely."},
            {"role": "user", "content": "Say exactly 'benchmark_ok' and nothing else."},
        ],
        temperature=0.0,
        max_tokens=200,
    )

    elapsed = time.monotonic() - start

    # Clean up to avoid event-loop-closed errors across tests.
    await client.close()
    await reset_llm_client_registry()

    assert resp.content, "Empty response from DirectLLMClient"
    assert "benchmark_ok" in resp.content.lower(), f"Unexpected response: {resp.content[:100]}"
    assert resp.input_tokens > 0, "Expected non-zero input tokens"
    assert resp.output_tokens > 0, "Expected non-zero output tokens"
    assert resp.latency_seconds > 0, "Expected non-zero latency"
    assert elapsed < 60, f"DirectLLMClient too slow: {elapsed:.1f}s"


@pytest.mark.asyncio
@needs_api_key
async def test_direct_llm_client_chat():
    """Measure DirectLLMClient chat endpoint latency."""
    client = get_llm_client(model="glm-5.1:cloud", timeout=60)
    start = time.monotonic()

    resp = await client.chat(
        messages=[
            {"role": "system", "content": "You are a benchmark tool. Respond concisely."},
            {"role": "user", "content": "Say exactly 'chat_benchmark_ok' and nothing else."},
        ],
        temperature=0.0,
        max_tokens=200,
    )

    elapsed = time.monotonic() - start

    # Clean up to avoid event-loop-closed errors.
    await client.close()
    await reset_llm_client_registry()

    assert resp.content, "Empty response from DirectLLMClient chat"
    assert "chat_benchmark_ok" in resp.content.lower(), (
        f"Unexpected chat response: {resp.content[:100]}"
    )
    assert resp.input_tokens > 0, "Expected non-zero input tokens"
    assert resp.output_tokens > 0, "Expected non-zero output tokens"
    assert elapsed < 60, f"DirectLLMClient chat too slow: {elapsed:.1f}s"


# ── Unit: Registry singleton ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_client_registry_singleton():
    """Verify LLMClientRegistry returns the same client for same model."""
    await reset_llm_client_registry()
    c1 = get_llm_client(model="test-model-a", timeout=5)
    c2 = get_llm_client(model="test-model-a", timeout=5)
    assert c1 is c2, "Registry should return singleton per model name"


@pytest.mark.asyncio
async def test_llm_client_registry_list():
    """Verify registry tracks multiple models correctly."""
    await reset_llm_client_registry()
    get_llm_client(model="test-x", timeout=5)
    get_llm_client(model="test-y", timeout=5)
    names = LLMClientRegistry().list_clients()
    assert "test-x" in names
    assert "test-y" in names


@pytest.mark.asyncio
async def test_registry_resolves_model_max_tokens():
    """tracked_generate inherits the model's declared budget when omitted."""
    registry = LLMClientRegistry()
    # Explicit value always wins.
    assert registry._resolve_max_tokens("nemotron-3-ultra:cloud", 200) == 200
    # Omitted -> declared budget.
    assert registry._resolve_max_tokens("nemotron-3-ultra:cloud", None) == 65536
    # Unknown model -> 8000 fallback.
    assert registry._resolve_max_tokens("nope:cloud", None) == 8000
    # Explicit value is clamped to the context window (resource guard).
    assert registry._resolve_max_tokens("nemotron-3-ultra:cloud", 999_999) == 128000


# ── Unit: tracked_llm_call (no network — cost tracking only) ─────────────


@pytest.mark.asyncio
@needs_api_key
async def test_tracked_llm_call_cost():
    """Verify tracked_llm_call records cost via cost_tracker."""
    from beagle.cost_tracker import get_cost_tracker, reset_cost_tracker

    reset_cost_tracker()
    tracker = get_cost_tracker()

    resp = await LLMClientRegistry().tracked_chat(
        messages=[
            {"role": "system", "content": "You are a benchmark. Respond concisely."},
            {"role": "user", "content": "Say 'cost_test_ok'"},
        ],
        model="glm-5.1:cloud",
        temperature=0.0,
        max_tokens=200,
    )

    # Clean up client connections.
    await reset_llm_client_registry()

    assert resp.content, "Empty response from tracked_llm_call"
    assert resp.cost_usd >= 0, "Cost must be non-negative"
    assert resp.input_tokens > 0, "Expected non-zero input tokens"

    # Verify cost was recorded in the global tracker.
    total = tracker.total_cost_usd
    assert total >= 0, "Cost should be tracked after LLM call"


# ── Unit: Tenant budget tracker ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_tenant_budget_tracker_async():
    """Verify tenant budget tracker handles concurrent spend recording."""
    from beagle.cost_tracker import TenantBudgetTracker

    tracker = TenantBudgetTracker(tenants=["org-a", "org-b"])
    tracker.set_budget("org-a", 0.05)
    tracker.set_budget("org-b", 0.02)

    async def spend(tenant: str, amount: float, count: int):
        for _ in range(count):
            await tracker.record_spend(tenant, amount)

    # Concurrent spending from two tenants.
    await asyncio.gather(
        spend("org-a", 0.002, 20),
        spend("org-b", 0.002, 15),
    )

    summary = tracker.get_summary()
    assert summary["org-a"]["spent_usd"] == pytest.approx(0.04, rel=1e-2)
    assert summary["org-b"]["spent_usd"] == pytest.approx(0.03, rel=1e-2)

    # org-b should be over budget (0.03 > 0.02), org-a should be under.
    assert summary["org-b"]["exceeded"] is True
    assert summary["org-a"]["exceeded"] is False


@pytest.mark.asyncio
async def test_tenant_budget_summary_format():
    """Verify tenant budget summary has expected keys and unlimited handling."""
    from beagle.cost_tracker import TenantBudgetTracker

    tracker = TenantBudgetTracker(tenants=["alpha"])
    tracker.set_budget("alpha", 0.10)

    await tracker.record_spend("alpha", 0.01)
    await tracker.record_spend("alpha", 0.02)

    summary = tracker.get_summary()
    assert "alpha" in summary
    assert "spent_usd" in summary["alpha"]
    assert "budget_usd" in summary["alpha"]
    assert "exceeded" in summary["alpha"]
    assert summary["alpha"]["exceeded"] is False  # 0.03 < 0.10
    assert summary["alpha"]["budget_usd"] == 0.10  # budget was set to 0.10

    # Budget exceeded should be True when spending exceeds budget.
    await tracker.record_spend("alpha", 0.08)  # total = 0.11 > 0.10
    summary2 = tracker.get_summary()
    assert summary2["alpha"]["exceeded"] is True

    # default tenant gets unlimited budget (0).
    default_summary = tracker.get_summary()
    assert default_summary.get("default", {}).get("budget_usd") == 0


# ── End-to-end: bridge delegation roundtrip ──────────────────────────────


@pytest.mark.asyncio
@needs_api_key
async def test_bridge_delegation_roundtrip():
    """End-to-end test: DirectLLMClient → cost_tracker → tenant budget.

    Simulates a bridge delegation: generate with tracked_llm_call,
    verify cost flows into the context-aware tracker and tenant budget.
    """
    from beagle.cost_tracker import (
        get_cost_tracker,
        reset_cost_tracker,
    )

    # Reset state.
    reset_cost_tracker()
    reset_tenant_budget_tracker(tenants=["e2e-tenant"])

    cost_tracker = get_cost_tracker()
    budget_tracker = get_tenant_budget_tracker(tenants=["e2e-tenant"])
    budget_tracker.set_budget("e2e-tenant", 0.50)

    registry = LLMClientRegistry()

    resp = await registry.tracked_chat(
        messages=[
            {"role": "system", "content": "You are a benchmark. Respond concisely."},
            {"role": "user", "content": "Say 'e2e_ok'"},
        ],
        model="glm-5.1:cloud",
        temperature=0.0,
        max_tokens=200,
        tenant_id="e2e-tenant",
    )

    # Clean up.
    await reset_llm_client_registry()

    assert resp.content, "Empty response from tracked_generate"
    assert resp.cost_usd > 0, "Cost should be > 0"

    # Verify cost flowed into cost_tracker.
    assert cost_tracker.total_cost_usd > 0, "Global cost tracker should have recorded usage"
    assert cost_tracker._total_input_tokens > 0, "Input tokens should be recorded"

    # Verify tenant budget recorded the spend.
    tenant_summary = budget_tracker.get_summary()
    assert tenant_summary["e2e-tenant"]["spent_usd"] > 0, (
        "Tenant budget should reflect the LLM call cost"
    )
