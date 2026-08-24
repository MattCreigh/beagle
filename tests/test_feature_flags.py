"""Tests for feature flags."""

from __future__ import annotations

from beagle.feature_flags import FeatureFlag, FeatureFlags


def test_feature_flags_defaults():
    ff = FeatureFlags({})
    assert ff.is_enabled(FeatureFlag.ENABLE_GRAPH_RAG) is False
    assert ff.is_enabled(FeatureFlag.ENABLE_GENAI_METRICS) is False


def test_feature_flags_enable():
    ff = FeatureFlags({})
    assert ff.is_enabled(FeatureFlag.ENABLE_GRAPH_RAG) is False
    ff.enable(FeatureFlag.ENABLE_GRAPH_RAG)
    assert ff.is_enabled(FeatureFlag.ENABLE_GRAPH_RAG) is True
    assert ff.is_enabled(FeatureFlag.ENABLE_GENAI_METRICS) is False


def test_feature_flags_config():
    ff = FeatureFlags({"enable_graph_rag": True, "enable_multi_tenancy": True})
    assert ff.is_enabled(FeatureFlag.ENABLE_GRAPH_RAG) is True
    assert ff.is_enabled(FeatureFlag.ENABLE_MULTI_TENANCY) is True
    assert ff.is_enabled(FeatureFlag.ENABLE_GENAI_METRICS) is False
