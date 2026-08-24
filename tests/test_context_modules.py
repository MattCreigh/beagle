"""Tests for beagle.context modules — import verification and basic instantiation."""

from __future__ import annotations

# ── Import verification for context submodules ──────────────────────────────


class TestContextImports:
    """Verify all context submodules can be imported."""

    def test_import_context_package(self):
        import beagle.context as ctx

        assert ctx is not None

    def test_import_context_window(self):
        from beagle.context.context_window import (
            ContextMetrics,
            ContextWindowManager,
        )

        assert ContextWindowManager is not None
        assert ContextMetrics is not None

    def test_import_session_model(self):
        from beagle.context.session_model import RoutedMatch, RuntimeSession

        assert RuntimeSession is not None
        assert RoutedMatch is not None

    def test_import_prompt_cache(self):
        from beagle.context.prompt_cache import (
            PromptCache,
            PromptMetadata,
            StaticPromptPart,
        )

        assert PromptCache is not None
        assert StaticPromptPart is not None
        assert PromptMetadata is not None

    def test_import_auto_hydration(self):
        from beagle.context.auto_hydration import (
            AutoHydrationConfig,
            HydrationResult,
        )

        assert AutoHydrationConfig is not None
        assert HydrationResult is not None

    def test_import_compressed_store(self):
        from beagle.context.compressed_store import (
            CompressedStore,
        )

        assert CompressedStore is not None

    def test_import_context_optimizer(self):
        from beagle.context.context_optimizer import ContextOptimizer, ContextPlan

        assert ContextOptimizer is not None
        assert ContextPlan is not None

    def test_import_context_integration(self):
        from beagle.context.context_integration import (
            ContextIntegration,
        )

        assert ContextIntegration is not None


# ── Basic instantiation ─────────────────────────────────────────────────────


class TestContextInstantiation:
    """Quick instantiation tests for key context objects."""

    def test_context_metrics_defaults(self):
        from beagle.context.context_window import ContextMetrics

        metrics = ContextMetrics()
        assert metrics.total_tokens == 0
        assert metrics.context_utilization == 0.0

    def test_prompt_cache_creation(self):
        from beagle.context.prompt_cache import PromptCache

        cache = PromptCache()
        assert cache is not None

    def test_runtime_session_creation(self):
        from beagle.context.session_model import RuntimeSession

        session = RuntimeSession(workflow_id="test-wf", query="test query")
        assert session.workflow_id == "test-wf"
        assert session.query == "test query"
        assert session.history == []
        assert session.routed_matches == []

    def test_routed_match_creation(self):
        from beagle.context.session_model import RoutedMatch

        match = RoutedMatch(kind="tool", name="test_tool", score=0.95)
        assert match.kind == "tool"
        assert match.name == "test_tool"
        assert match.score == 0.95

    def test_session_add_event(self):
        from beagle.context.session_model import RuntimeSession
        from beagle.events.events import WorkflowStarted

        session = RuntimeSession(workflow_id="w1", query="q")
        event = WorkflowStarted(workflow_id="w1")
        session.add_event(event)
        assert len(session.stream_events) == 1

    def test_session_add_match(self):
        from beagle.context.session_model import RoutedMatch, RuntimeSession

        session = RuntimeSession(workflow_id="w1", query="q")
        match = RoutedMatch(kind="command", name="run", score=0.8)
        session.add_match(match)
        assert len(session.routed_matches) == 1

    def test_session_to_dict(self):
        from beagle.context.session_model import RuntimeSession

        session = RuntimeSession(workflow_id="w1", query="q")
        d = session.to_dict()
        assert "workflow_id" in d
        assert d["workflow_id"] == "w1"

    def test_auto_hydration_config_defaults(self):
        from beagle.context.auto_hydration import AutoHydrationConfig

        config = AutoHydrationConfig()
        assert config is not None
