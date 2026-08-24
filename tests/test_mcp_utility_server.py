"""Tests for the consolidated Beagle Utility MCP Server.

Covers:
- Rate limiting (_check_mcp_rate_limit)
- Correlation ID helpers (set_correlation_id, get_correlation_id)
- Metrics collection (record_metric, get_metrics_summary)
- Workflow tools: list_available_workflows, route_query_to_workflow,
  validate_workflow_file, estimate_workflow_cost
- Workflow tools: run_beagle_workflow, get_agent_recipe, list_agents,
  get_metrics, health_check
- Web Search tools: web_search, arxiv_search, web_research
- Code tools integration (smoke test — full tests in test_code_tools.py)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beagle.infrastructure.mcp_common import (
    _metrics,
    get_correlation_id,
    get_metrics_summary,
    record_metric,
    set_correlation_id,
)
from beagle.infrastructure.mcp_utility_server import (
    estimate_workflow_cost,
    get_agent_recipe,
    get_metrics,
    health_check,
    list_agents,
    list_available_workflows,
    route_query_to_workflow,
    validate_workflow_file,
)

# ── Rate Limiter Tests ────────────────────────────────────────────────────────


class TestRateLimiter:
    """Tests for the shared RateLimiter used by the utility server."""

    @pytest.mark.asyncio
    async def test_rate_limiter_allows_calls_under_limit(self):
        """Calls under the limit should not raise."""
        from beagle.utils.mcp_rate_limit import RateLimiter

        limiter = RateLimiter()
        for _ in range(5):
            await limiter.check()  # Should not raise

    @pytest.mark.asyncio
    async def test_rate_limiter_blocks_calls_over_limit(self):
        """Calls exceeding the limit should raise RateLimitExceeded."""
        from beagle.utils.mcp_rate_limit import RateLimiter

        limiter = RateLimiter(max_calls=10, window_seconds=60.0)
        for _ in range(10):
            await limiter.check()
        with pytest.raises(RuntimeError, match="MCP rate limit exceeded"):
            await limiter.check()

    @pytest.mark.asyncio
    async def test_rate_limiter_window_expiry(self):
        """Old timestamps outside the window should be evicted."""
        from beagle.utils.mcp_rate_limit import RateLimiter

        limiter = RateLimiter(max_calls=60, window_seconds=0.3)
        for _ in range(60):
            await limiter.check()
        # Budget exhausted
        with pytest.raises(RuntimeError):
            await limiter.check()
        # Let the window slide
        import asyncio

        await asyncio.sleep(0.4)
        # Now should pass again
        await limiter.check()


# ── Correlation ID Tests ──────────────────────────────────────────────────────


class TestCorrelationId:
    """Tests for correlation ID helpers."""

    def test_set_correlation_id_returns_string(self):
        """set_correlation_id should return a non-empty string."""
        cid = set_correlation_id()
        assert isinstance(cid, str)
        assert len(cid) == 36  # uuid4

    def test_get_correlation_id_matches_set(self):
        """get_correlation_id should return the last set ID."""
        cid = set_correlation_id()
        assert get_correlation_id() == cid

    def test_correlation_ids_are_unique(self):
        """Each call to set_correlation_id should produce a unique ID."""
        ids = {set_correlation_id() for _ in range(20)}
        assert len(ids) == 20


# ── Metrics Collection Tests ─────────────────────────────────────────────────


class TestMetricsCollection:
    """Tests for record_metric and get_metrics_summary."""

    def setup_method(self):
        """Reset metrics state before each test."""
        _metrics["requests"]["total"] = 0
        _metrics["requests"]["success"] = 0
        _metrics["requests"]["error"] = 0
        _metrics["durations"].clear()

    def test_record_metric_success(self):
        """record_metric with success=True should increment success counter."""
        record_metric("test_tool", 0.5, success=True)
        assert _metrics["requests"]["total"] == 1
        assert _metrics["requests"]["success"] == 1
        assert _metrics["requests"]["error"] == 0

    def test_record_metric_failure(self):
        """record_metric with success=False should increment error counter."""
        record_metric("test_tool", 0.1, success=False)
        assert _metrics["requests"]["total"] == 1
        assert _metrics["requests"]["error"] == 1
        assert _metrics["requests"]["success"] == 0

    def test_record_metric_stores_durations(self):
        """record_metric should store durations for later summary."""
        record_metric("tool_a", 0.1)
        record_metric("tool_a", 0.3)
        record_metric("tool_b", 0.5)

        summary = get_metrics_summary()
        assert "tool_a" in summary["durations"]
        assert "tool_b" in summary["durations"]
        assert summary["durations"]["tool_a"]["count"] == 2

    def test_get_metrics_summary_structure(self):
        """get_metrics_summary should return a dict with requests and durations."""
        record_metric("test", 1.0)
        summary = get_metrics_summary()
        assert "requests" in summary
        assert "durations" in summary
        assert "total" in summary["requests"]
        assert "success" in summary["requests"]
        assert "error" in summary["requests"]


# ── Workflow Tool Tests ──────────────────────────────────────────────────────


class TestListAvailableWorkflows:
    """Tests for list_available_workflows."""

    @pytest.mark.asyncio
    @patch("beagle.infrastructure.mcp_utility_server.list_workflows")
    async def test_returns_workflow_list(self, mock_list):
        """Should return a JSON array of workflows."""
        mock_list.return_value = [
            {"name": "research", "phases": 5, "description": "Research workflow"},
        ]
        result = await list_available_workflows()
        parsed = json.loads(result)
        assert isinstance(parsed, list)
        assert parsed[0]["name"] == "research"

    @pytest.mark.asyncio
    @patch("beagle.infrastructure.mcp_utility_server.list_workflows")
    async def test_records_metrics_on_success(self, mock_list):
        """Should record metrics on successful call."""
        mock_list.return_value = []
        _metrics["requests"]["total"] = 0
        _metrics["durations"].clear()
        await list_available_workflows()
        assert _metrics["requests"]["success"] >= 1
        assert "list_available_workflows" in _metrics["durations"]

    @pytest.mark.asyncio
    @patch("beagle.infrastructure.mcp_utility_server.list_workflows")
    async def test_records_metrics_on_failure(self, mock_list):
        """Should record metrics on failed call."""
        mock_list.side_effect = RuntimeError("boom")
        _metrics["requests"]["total"] = 0
        _metrics["durations"].clear()
        with pytest.raises(RuntimeError):
            await list_available_workflows()
        assert _metrics["requests"]["error"] >= 1


class TestRouteQueryToWorkflow:
    """Tests for route_query_to_workflow."""

    @pytest.mark.asyncio
    @patch("beagle.infrastructure.mcp_utility_server.route_query")
    async def test_returns_routing_result(self, mock_route):
        """Should return workflow recommendation with confidence."""
        mock_result = MagicMock()
        mock_result.workflow = "security"
        mock_result.confidence = 0.92
        mock_result.reasoning = "Security keywords detected"
        mock_result.alternatives = [("audit", 0.6), ("research", 0.3)]
        mock_route.return_value = mock_result

        result = await route_query_to_workflow("Check for SQL injection vulnerabilities")
        parsed = json.loads(result)
        assert parsed["workflow"] == "security"
        assert parsed["confidence"] == 0.92
        assert len(parsed["alternatives"]) == 2


class TestValidateWorkflowFile:
    """Tests for validate_workflow_file."""

    @pytest.mark.asyncio
    @patch("beagle.infrastructure.mcp_utility_server.validate_workflow")
    async def test_valid_workflow(self, mock_validate):
        """Should return valid=true for a workflow with no errors."""
        mock_validate.return_value = []
        result = await validate_workflow_file("research.yaml")
        parsed = json.loads(result)
        assert parsed["valid"] is True
        assert parsed["errors"] == []

    @pytest.mark.asyncio
    @patch("beagle.infrastructure.mcp_utility_server.validate_workflow")
    async def test_invalid_workflow(self, mock_validate):
        """Should return valid=false with error details."""
        mock_validate.return_value = ["Missing required field: phases"]
        result = await validate_workflow_file("broken.yaml")
        parsed = json.loads(result)
        assert parsed["valid"] is False
        assert len(parsed["errors"]) == 1


class TestEstimateWorkflowCost:
    """Tests for estimate_workflow_cost."""

    @pytest.mark.asyncio
    @patch("beagle.infrastructure.mcp_utility_server._get_pricing")
    @patch("beagle.infrastructure.mcp_utility_server.get_config")
    @patch("beagle.infrastructure.mcp_utility_server.list_workflows")
    @patch("beagle.infrastructure.mcp_utility_server.estimate_tokens_agnostic")
    async def test_returns_cost_estimate(self, mock_tokens, mock_list, mock_config, mock_pricing):
        """Should return a cost estimate with expected fields."""
        mock_tokens.return_value = 500
        mock_list.return_value = [{"name": "research", "phases": 5}]
        mock_cfg = MagicMock()
        mock_cfg.goose.default_model = "glm-5.1:cloud"
        mock_config.return_value = mock_cfg
        mock_pricing.return_value = {"input": 3.0, "output": 15.0}

        result = await estimate_workflow_cost("Test query", workflow_name="research")
        parsed = json.loads(result)
        assert "estimated_cost_usd" in parsed
        assert "estimated_total_tokens" in parsed
        assert parsed["phases"] == 5
        assert parsed["model"] == "glm-5.1:cloud"


# ── Agent Recipe Tests ────────────────────────────────────────────────────────


class TestGetAgentRecipe:
    """Tests for get_agent_recipe."""

    @pytest.mark.asyncio
    async def test_rejects_invalid_agent_name(self):
        """Should reject agent names with path traversal characters."""
        result = await get_agent_recipe("../../../etc/passwd")
        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert parsed["code"] == "INVALID_INPUT"

    @pytest.mark.asyncio
    async def test_rejects_long_agent_name(self):
        """Should reject agent names longer than 64 chars."""
        result = await get_agent_recipe("a" * 65)
        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert parsed["code"] == "INVALID_INPUT"

    @pytest.mark.asyncio
    @patch("beagle.infrastructure.tools._impl.get_workspace_root")
    async def test_rejects_path_traversal_via_symlink(self, mock_workspace):
        """Should reject resolved paths outside recipes/ directory."""
        mock_workspace.return_value = Path("/workspace")
        # The regex validation will catch this first
        result = await get_agent_recipe("../../etc/passwd")
        parsed = json.loads(result)
        assert parsed["status"] == "error"


# ── List Agents Tests ─────────────────────────────────────────────────────────


class TestListAgents:
    """Tests for list_agents."""

    @pytest.mark.asyncio
    @patch("beagle.infrastructure.mcp_utility_server._list_agents_impl")
    async def test_returns_agent_list(self, mock_impl):
        """Should return a JSON array of agents."""
        mock_impl.return_value = json.dumps(
            [{"name": "research-planner", "description": "Plans research"}]
        )
        result = await list_agents()
        parsed = json.loads(result)
        assert isinstance(parsed, list)
        assert parsed[0]["name"] == "research-planner"


# ── Observability Tool Tests ──────────────────────────────────────────────────


class TestGetMetrics:
    """Tests for get_metrics tool."""

    @pytest.mark.asyncio
    async def test_returns_metrics_summary(self):
        """Should return a JSON metrics summary."""
        _metrics["requests"]["total"] = 10
        _metrics["requests"]["success"] = 9
        _metrics["requests"]["error"] = 1
        result = await get_metrics()
        parsed = json.loads(result)
        assert "requests" in parsed
        assert parsed["requests"]["total"] >= 10


class TestHealthCheck:
    """Tests for health_check tool."""

    @pytest.mark.asyncio
    @patch("beagle.infrastructure.mcp_utility_server.list_routable_workflows")
    @patch("beagle.infrastructure.mcp_utility_server.list_workflows")
    @patch("beagle.infrastructure.mcp_utility_server.get_config")
    async def test_healthy_response(self, mock_config, mock_list_wf, mock_list_routes):
        """Should return healthy status when all checks pass."""
        mock_cfg = MagicMock()
        mock_cfg.goose.provider = "openai"
        mock_cfg.goose.default_model = "glm-5.1:cloud"
        mock_config.return_value = mock_cfg
        mock_list_wf.return_value = [{"name": "research"}]
        mock_list_routes.return_value = [{"name": "research"}]

        result = await health_check()
        parsed = json.loads(result)
        assert parsed["status"] in ("healthy", "degraded")
        assert "checks" in parsed

    @pytest.mark.asyncio
    @patch("beagle.infrastructure.mcp_utility_server.get_config")
    async def test_degraded_on_config_failure(self, mock_config):
        """Should return degraded status when config fails."""
        mock_config.side_effect = RuntimeError("Config not found")

        result = await health_check()
        parsed = json.loads(result)
        assert parsed["status"] == "degraded"
        assert parsed["checks"]["config"]["status"] == "error"


# ── Integration Smoke Tests ──────────────────────────────────────────────────


class TestUtilityServerSmoke:
    """Smoke tests verifying the consolidated server imports and structure."""

    def test_mcp_instance_exists(self):
        """The FastMCP instance should be importable."""
        from beagle.infrastructure.mcp_utility_server import mcp

        assert mcp is not None

    def test_all_code_tools_importable(self):
        """All code tool functions should be importable."""
        from beagle.infrastructure.mcp_utility_server import (
            code_context,
            code_search,
            file_discovery,
        )

        assert callable(code_search)
        assert callable(file_discovery)
        assert callable(code_context)

    def test_all_web_tools_importable(self):
        """All web search tool functions should be importable."""
        from beagle.infrastructure.mcp_utility_server import (
            arxiv_search,
            web_research,
            web_search,
        )

        assert callable(web_search)
        assert callable(arxiv_search)
        assert callable(web_research)

    def test_all_workflow_tools_importable(self):
        """All workflow tool functions should be importable."""
        from beagle.infrastructure.mcp_utility_server import (
            estimate_workflow_cost,
            get_agent_recipe,
            get_metrics,
            health_check,
            list_agents,
            list_available_workflows,
            route_query_to_workflow,
            run_beagle_workflow,
            validate_workflow_file,
        )

        assert callable(list_available_workflows)
        assert callable(route_query_to_workflow)
        assert callable(validate_workflow_file)
        assert callable(estimate_workflow_cost)
        assert callable(run_beagle_workflow)
        assert callable(get_agent_recipe)
        assert callable(list_agents)
        assert callable(get_metrics)
        assert callable(health_check)

    def test_all_context_tools_importable(self):
        """All context management tool functions should be importable."""
        from beagle.infrastructure.mcp_utility_server import (
            check_and_fold_context,
            report_context_usage,
        )

        assert callable(report_context_usage)
        assert callable(check_and_fold_context)

    @pytest.mark.asyncio
    async def test_report_context_usage_writes_report(self):
        """report_context_usage should write context_report.json and return status."""
        import json

        from beagle.infrastructure.mcp_utility_server import (
            report_context_usage,
        )

        result = await report_context_usage(0.59, used_tokens=75520, max_tokens=128000)
        parsed = json.loads(result)
        assert parsed["status"] == "ok"
        assert parsed["percentage"] == 0.59
        assert parsed["recommendation"] in ("continue", "approaching_threshold", "compact_now")

    @pytest.mark.asyncio
    async def test_report_context_usage_clamps_percentage(self):
        """report_context_usage should clamp percentage to [0.0, 1.0]."""
        import json

        from beagle.infrastructure.mcp_utility_server import (
            report_context_usage,
        )

        result = await report_context_usage(1.5)
        parsed = json.loads(result)
        assert parsed["percentage"] == 1.0

    @pytest.mark.asyncio
    async def test_check_and_fold_context_below_threshold(self):
        """check_and_fold_context below threshold should return action=continue."""
        import json

        from beagle.infrastructure.mcp_utility_server import (
            check_and_fold_context,
        )

        result = await check_and_fold_context(0.30, used_tokens=38400, max_tokens=128000)
        parsed = json.loads(result)
        assert parsed["status"] == "ok"
        assert parsed["action"] == "continue"

    @pytest.mark.asyncio
    @patch("beagle.context.context_integration.get_context_integration")
    async def test_check_and_fold_context_at_threshold(self, mock_get_integration):
        """check_and_fold_context at threshold should return action=compact_now."""
        import json

        from beagle.infrastructure.mcp_utility_server import (
            check_and_fold_context,
        )

        mock_integration = MagicMock()
        mock_integration.enhanced_context_fold = AsyncMock()
        mock_integration.get_stats.return_value = {"integration": {"turbo_fold_id": "abc123"}}
        mock_get_integration.return_value = mock_integration

        result = await check_and_fold_context(
            0.72, used_tokens=92160, max_tokens=128000, query="test task"
        )
        parsed = json.loads(result)
        assert parsed["status"] == "ok"
        assert parsed["action"] == "compact_now"
        assert "rehydration_prompt" in parsed
        assert len(parsed["rehydration_prompt"]) > 0

    def test_infrastructure_package_exports_utility(self):
        """The infrastructure package should export mcp_utility_server."""
        import beagle.infrastructure as infra

        # Lazy import should work
        module = infra.mcp_utility_server
        assert module is not None
        assert hasattr(module, "mcp")


# ── Anti-Stub Structural Tests ────────────────────────────────────────────────
# v13.16: Added after Phase 5 stub-gutting incident.
# These tests verify that key MCP tool functions have REAL implementations,
# not structurally-correct stubs (return {} / [] / None / "").
# Static gates (lint, banned, import) cannot detect "function does nothing."


class TestAntiStub:
    """Verify critical tool functions are NOT hollow stubs."""

    def _get_func_lines(self, func) -> int:
        """Get the number of source lines in a function body."""
        import inspect

        return len(inspect.getsource(func).split("\n"))

    def _get_func_bytecode_len(self, func) -> int:
        """Get bytecode instruction count (catches return-constant stubs)."""
        return len(func.__code__.co_code)

    def test_run_beagle_workflow_is_not_stub(self):
        """run_beagle_workflow must have a real body. A stub returning {} is < 10 bytecode ops."""
        from beagle.infrastructure.mcp_utility_server import (
            run_beagle_workflow,
        )

        ops = self._get_func_bytecode_len(run_beagle_workflow)
        lines = self._get_func_lines(run_beagle_workflow)
        assert ops > 50, (
            f"run_beagle_workflow bytecode={ops} (suspiciously small — "
            f"likely a stub returning {{}}; expected > 50)"
        )
        assert lines > 20, (
            f"run_beagle_workflow source lines={lines} (suspiciously short — "
            f"likely a stub; expected > 20)"
        )

    def test_beagle_session_bootstrap_is_not_stub(self):
        """beagle_session_bootstrap must have a real body."""
        from beagle.infrastructure.mcp_utility_server import (
            beagle_session_bootstrap,
        )

        ops = self._get_func_bytecode_len(beagle_session_bootstrap)
        assert ops > 20, (
            f"beagle_session_bootstrap bytecode={ops} (likely a stub returning {{}}; expected > 20)"
        )

    def test_code_search_is_not_stub(self):
        """code_search must have a real body."""
        from beagle.infrastructure.mcp_utility_server import (
            code_search,
        )

        ops = self._get_func_bytecode_len(code_search)
        assert ops > 20, f"code_search bytecode={ops} (likely a stub returning []; expected > 20)"

    def test_web_search_is_not_stub(self):
        """web_search must have a real body."""
        from beagle.infrastructure.mcp_utility_server import (
            web_search,
        )

        ops = self._get_func_bytecode_len(web_search)
        assert ops > 20, f"web_search bytecode={ops} (likely a stub returning []; expected > 20)"

    def test_list_agents_is_not_stub(self):
        """list_agents must have a real body."""
        from beagle.infrastructure.mcp_utility_server import (
            list_agents,
        )

        ops = self._get_func_bytecode_len(list_agents)
        assert ops > 10, f"list_agents bytecode={ops} (likely a stub returning []; expected > 10)"

    def test_list_available_workflows_is_not_stub(self):
        """list_available_workflows must have a real body."""
        from beagle.infrastructure.mcp_utility_server import (
            list_available_workflows,
        )

        ops = self._get_func_bytecode_len(list_available_workflows)
        assert ops > 10, (
            f"list_available_workflows bytecode={ops} (likely a stub returning []; expected > 10)"
        )

    def test_check_and_fold_context_is_not_stub(self):
        """check_and_fold_context must have a real body."""
        from beagle.infrastructure.mcp_utility_server import (
            check_and_fold_context,
        )

        ops = self._get_func_bytecode_len(check_and_fold_context)
        assert ops > 20, f"check_and_fold_context bytecode={ops} (likely a stub; expected > 20)"

    def test_get_agent_recipe_is_not_stub(self):
        """get_agent_recipe must have a real body."""
        from beagle.infrastructure.mcp_utility_server import (
            get_agent_recipe,
        )

        ops = self._get_func_bytecode_len(get_agent_recipe)
        assert ops > 20, f"get_agent_recipe bytecode={ops} (likely a stub; expected > 20)"


class TestArxivAtomParsingRegression:
    """BGL-001 regression — arxiv_search must not raise on self-closing elements.

    Copy A (mcp_utility_server.py) dereferenced ``.text`` on an element that
    could be ``None`` (a self-closing ``<title/>`` or ``<id/>``), raising an
    unhandled AttributeError. The surviving implementation (tools/_impl.py)
    guards each dereference with an ``is not None`` check. This test feeds an
    Atom entry with self-closing elements and asserts the function returns
    JSON rather than raising.
    """

    ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001v1</id>
    <title/>
    <summary>An abstract.</summary>
    <published>2026-01-01T00:00:00Z</published>
  </entry>
</feed>"""

    @pytest.mark.asyncio
    async def test_self_closing_title_and_id_return_json(self):
        """A self-closing <title/> and <id/> must not raise AttributeError."""
        from beagle.infrastructure.tools._impl import arxiv_search

        class _FakeResponse:
            text = self.ATOM

            def raise_for_status(self) -> None:
                return None

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def get(self, *_args, **_kwargs):
                return _FakeResponse()

        with patch("httpx.AsyncClient", return_value=_FakeClient()):
            result = await arxiv_search("test query", max_results=3)

        payload = json.loads(result)
        assert "results" in payload
        assert "count" in payload
        # The entry with a self-closing title/id must still be parsed (or
        # skipped without raising) — the key contract is that we return JSON.
        assert isinstance(payload["results"], list)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
