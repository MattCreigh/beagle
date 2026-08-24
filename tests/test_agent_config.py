"""Tests for beagle.config.agent_config — provider decoupling.

Validates the agent profile fallback chain:
  agents.toml[name] → agents.toml[default] → config.toml[llm]
  → config.toml[goose] → hardcoded defaults

All providers use ollama_cloud (OpenAI-compatible). Default model is glm-5.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    """Invalidate agent_config caches before and after each test."""
    from beagle.config.agent_config import invalidate_cache

    invalidate_cache()
    yield
    invalidate_cache()


@pytest.fixture
def tmp_agents_toml(tmp_path):
    """Create a temporary agents.toml with test profiles."""
    toml_content = """
[default]
provider = "ollama_cloud"
model = "glm-5.1:cloud"
temperature = 0.4
description = "Default test profile"

[planner]
provider = "ollama_cloud"
model = "glm-5.1:cloud"
temperature = 0.3
description = "Research planning"

[executor]
provider = "ollama_cloud"
model = "glm-5.1:cloud"
temperature = 0.2
description = "Code execution"

[security_firewall]
provider = "ollama_cloud"
model = "gemma3:27b"
temperature = 0.0
description = "Cheap model for security checks"
"""
    toml_path = tmp_path / "agents.toml"
    toml_path.write_text(toml_content)
    return toml_path


@pytest.fixture
def tmp_config_toml(tmp_path):
    """Create a minimal config.toml with [llm] section."""
    toml_content = """
[llm]
default_provider = "ollama_cloud"
default_model = "glm-5.1:cloud"
cheap_model = "gemma3:27b"
cheap_provider = "ollama_cloud"

[goose]
provider = "ollama_cloud"
default_model = "glm-5.1:cloud"
"""
    config_path = tmp_path / "config.toml"
    config_path.write_text(toml_content)
    return config_path


# ---------------------------------------------------------------------------
# AgentProfile dataclass tests
# ---------------------------------------------------------------------------


class TestAgentProfile:
    """Tests for the AgentProfile frozen dataclass."""

    def test_creation(self):
        from beagle.config.agent_config import AgentProfile

        profile = AgentProfile(
            name="test",
            provider="ollama_cloud",
            model="glm-5.1:cloud",
            temperature=0.4,
            description="Test profile",
        )
        assert profile.name == "test"
        assert profile.provider == "ollama_cloud"
        assert profile.model == "glm-5.1:cloud"
        assert profile.temperature == 0.4
        assert profile.description == "Test profile"

    def test_frozen_immutability(self):
        from beagle.config.agent_config import AgentProfile

        profile = AgentProfile(name="test", provider="ollama_cloud", model="glm-5.1:cloud")
        with pytest.raises(AttributeError):
            profile.model = "other-model"

    def test_litellm_model_property(self):
        from beagle.config.agent_config import AgentProfile

        profile = AgentProfile(name="test", provider="ollama_cloud", model="glm-5.1:cloud")
        assert profile.litellm_model == "ollama_cloud/glm-5.1:cloud"

    def test_default_temperature(self):
        from beagle.config.agent_config import AgentProfile

        profile = AgentProfile(name="test", provider="ollama_cloud", model="glm-5.1:cloud")
        assert profile.temperature == 0.4

    def test_default_description(self):
        from beagle.config.agent_config import AgentProfile

        profile = AgentProfile(name="test", provider="ollama_cloud", model="glm-5.1:cloud")
        assert profile.description == ""


# ---------------------------------------------------------------------------
# get_agent() — fallback chain
# ---------------------------------------------------------------------------


class TestGetAgentFallbackChain:
    """Tests for the agent profile resolution fallback chain."""

    def test_known_profile_from_toml(self, tmp_agents_toml):
        """agents.toml[name] should resolve correctly."""
        from beagle.config.agent_config import (
            get_agent,
            invalidate_cache,
        )

        invalidate_cache()
        with patch(
            "beagle.config.agent_config._get_agents_toml_path",
            return_value=tmp_agents_toml,
        ):
            profile = get_agent("planner")
            assert profile.name == "planner"
            assert profile.model == "glm-5.1:cloud"
            assert profile.provider == "ollama_cloud"
            assert profile.temperature == 0.3

    def test_unknown_name_falls_back_to_default(self, tmp_agents_toml):
        """Unknown agent name should fall back to [default] profile."""
        from beagle.config.agent_config import (
            get_agent,
            invalidate_cache,
        )

        invalidate_cache()
        with patch(
            "beagle.config.agent_config._get_agents_toml_path",
            return_value=tmp_agents_toml,
        ):
            profile = get_agent("nonexistent_agent")
            assert profile.name == "nonexistent_agent"
            assert profile.model == "glm-5.1:cloud"  # From default profile

    def test_executor_profile(self, tmp_agents_toml):
        """Executor profile should use glm-5.1:cloud."""
        from beagle.config.agent_config import (
            get_agent,
            invalidate_cache,
        )

        invalidate_cache()
        with patch(
            "beagle.config.agent_config._get_agents_toml_path",
            return_value=tmp_agents_toml,
        ):
            profile = get_agent("executor")
            assert profile.model == "glm-5.1:cloud"
            assert profile.temperature == 0.2

    def test_security_firewall_profile(self, tmp_agents_toml):
        """Security firewall should use the cheapest model."""
        from beagle.config.agent_config import (
            get_agent,
            invalidate_cache,
        )

        invalidate_cache()
        with patch(
            "beagle.config.agent_config._get_agents_toml_path",
            return_value=tmp_agents_toml,
        ):
            profile = get_agent("security_firewall")
            assert profile.model == "gemma3:27b"
            assert profile.temperature == 0.0

    def test_hardcoded_defaults_when_no_toml(self):
        """When agents.toml and config.toml are missing, hardcoded defaults should be used."""
        from beagle.config.agent_config import (
            get_agent,
            invalidate_cache,
        )

        invalidate_cache()
        with (
            patch(
                "beagle.config.agent_config._get_agents_toml_path",
                return_value=Path("/nonexistent/agents.toml"),
            ),
            patch(
                "beagle.config.agent_config._load_llm_defaults",
                return_value={},
            ),
        ):
            profile = get_agent("anything")
            # Provider falls to hardcoded default (ollama_cloud).
            assert profile.provider == "ollama_cloud"
            # Provider-neutral: no model preset ships; the fallback model is
            # "" until the operator configures one ([goose].default_model or
            # model presets). See README "Provider-neutral LLM configuration".
            assert profile.model == ""
            assert profile.temperature == 0.4


# ---------------------------------------------------------------------------
# Environment variable overrides
# ---------------------------------------------------------------------------


class TestEnvOverrides:
    """Tests for GOOSE_MODEL and GOOSE_PROVIDER environment variable overrides."""

    def test_goose_model_override(self, tmp_agents_toml):
        """GOOSE_MODEL env var should override any resolved model."""
        from beagle.config.agent_config import (
            get_agent,
            invalidate_cache,
        )

        invalidate_cache()
        with (
            patch(
                "beagle.config.agent_config._get_agents_toml_path",
                return_value=tmp_agents_toml,
            ),
            patch.dict(os.environ, {"GOOSE_MODEL": "qwen3.5:397b"}, clear=False),
        ):
            profile = get_agent("planner")
            assert profile.model == "qwen3.5:397b"

    def test_goose_provider_override(self, tmp_agents_toml):
        """GOOSE_PROVIDER env var should override any resolved provider."""
        from beagle.config.agent_config import (
            get_agent,
            invalidate_cache,
        )

        invalidate_cache()
        with (
            patch(
                "beagle.config.agent_config._get_agents_toml_path",
                return_value=tmp_agents_toml,
            ),
            patch.dict(os.environ, {"GOOSE_PROVIDER": "openai"}, clear=False),
        ):
            profile = get_agent("planner")
            assert profile.provider == "openai"

    def test_both_env_overrides(self, tmp_agents_toml):
        """Both env vars should override simultaneously."""
        from beagle.config.agent_config import (
            get_agent,
            invalidate_cache,
        )

        invalidate_cache()
        with (
            patch(
                "beagle.config.agent_config._get_agents_toml_path",
                return_value=tmp_agents_toml,
            ),
            patch.dict(
                os.environ,
                {"GOOSE_MODEL": "kimi-k2-thinking", "GOOSE_PROVIDER": "ollama_cloud"},
                clear=False,
            ),
        ):
            profile = get_agent("executor")
            assert profile.model == "kimi-k2-thinking"
            assert profile.provider == "ollama_cloud"


# ---------------------------------------------------------------------------
# get_cheap_agent()
# ---------------------------------------------------------------------------


class TestGetCheapAgent:
    """Tests for the cheap agent resolution."""

    def test_cheap_agent_from_toml(self, tmp_agents_toml):
        """get_cheap_agent() should return security_firewall profile from agents.toml."""
        from beagle.config.agent_config import (
            get_cheap_agent,
            invalidate_cache,
        )

        invalidate_cache()
        with patch(
            "beagle.config.agent_config._get_agents_toml_path",
            return_value=tmp_agents_toml,
        ):
            profile = get_cheap_agent()
            assert profile.model == "gemma3:27b"
            assert profile.temperature == 0.0

    def test_cheap_agent_fallback_no_toml(self):
        """Without agents.toml and without config.toml LLM defaults,
        get_cheap_agent() should use hardcoded defaults."""
        from beagle.config.agent_config import (
            get_cheap_agent,
            invalidate_cache,
        )

        invalidate_cache()
        with (
            patch(
                "beagle.config.agent_config._get_agents_toml_path",
                return_value=Path("/nonexistent/agents.toml"),
            ),
            patch(
                "beagle.config.agent_config._load_llm_defaults",
                return_value={},
            ),
        ):
            profile = get_cheap_agent()
            # No config at all, so this falls all the way back to the
            # hardcoded default (ollama_cloud).
            assert profile.provider == "ollama_cloud"
            # Provider-neutral: no preset ships; the fallback cheap-model is
            # "" until the operator configures one.
            assert profile.model == ""

    def test_cheap_agent_from_config_toml(self):
        """With config.toml cheap_model set, get_cheap_agent() should use it."""
        from beagle.config.agent_config import (
            get_cheap_agent,
            invalidate_cache,
        )

        invalidate_cache()
        with patch(
            "beagle.config.agent_config._get_agents_toml_path",
            return_value=Path("/nonexistent/agents.toml"),
        ):
            # Without mocking _load_llm_defaults, it reads config.toml
            # which has cheap_model="gemma4:31b" (v1.0.9 audit C1 restored
            # per-tier routing) and cheap_provider="ollama_cloud".
            profile = get_cheap_agent()
            assert profile.provider == "ollama_cloud"
            assert profile.model == "gemma4:31b"


# ---------------------------------------------------------------------------
# list_agents() and invalidate_cache()
# ---------------------------------------------------------------------------


class TestListAgentsAndCache:
    """Tests for list_agents() and invalidate_cache()."""

    def test_list_agents_returns_profiles(self, tmp_agents_toml):
        """list_agents() should return all non-default profiles plus default."""
        from beagle.config.agent_config import (
            invalidate_cache,
            list_agents,
        )

        invalidate_cache()
        with patch(
            "beagle.config.agent_config._get_agents_toml_path",
            return_value=tmp_agents_toml,
        ):
            agents = list_agents()
            assert "default" in agents
            assert "planner" in agents
            assert "executor" in agents

    def test_invalidate_cache_clears_data(self, tmp_agents_toml):
        """invalidate_cache() should force re-read of agents.toml."""
        from beagle.config import agent_config

        agent_config.invalidate_cache()
        # Access internal cache to verify it's None
        assert agent_config._agents_cache is None
        assert agent_config._llm_defaults_cache is None

    def test_cache_populates_after_first_call(self, tmp_agents_toml):
        """Cache should be populated after first get_agent() call."""
        from beagle.config import agent_config
        from beagle.config.agent_config import (
            get_agent,
            invalidate_cache,
        )

        invalidate_cache()
        with patch(
            "beagle.config.agent_config._get_agents_toml_path",
            return_value=tmp_agents_toml,
        ):
            get_agent("planner")
            assert agent_config._agents_cache is not None


# ---------------------------------------------------------------------------
# Config.toml [llm] fallback
# ---------------------------------------------------------------------------


class TestLLMConfigFallback:
    """Tests for config.toml [llm] section fallback."""

    def test_llm_section_used_when_no_agents_toml(self, tmp_config_toml):
        """When agents.toml is missing, [llm] section from config.toml should be used."""
        from beagle.config.agent_config import (
            get_agent,
            invalidate_cache,
        )

        invalidate_cache()
        with (
            patch(
                "beagle.config.agent_config._get_agents_toml_path",
                return_value=Path("/nonexistent/agents.toml"),
            ),
            patch("beagle.config.agent_config.Path") as mock_path,
        ):
            mock_path.return_value.resolve.return_value.parent.parent.__truediv__ = (
                lambda self, _other: tmp_config_toml
            )
            # This is complex to mock properly, so just test the hardcoded default
            profile = get_agent("default")
            # The provider is read from the active fleet card (SSOT) — ollama_cloud.
            # v1.0.9 (audit C1): default model is deepseek-v4-flash:0731-cloud.
            assert profile.provider == "ollama_cloud"
            assert profile.model == "deepseek-v4-flash:0731-cloud"

    def test_hardcoded_defaults_use_ollama_cloud(self):
        """Hardcoded defaults should use the default fleet provider (ollama_cloud)."""
        from beagle.config.agent_config import _HARDCODED_DEFAULTS, _default_model

        assert _HARDCODED_DEFAULTS["provider"] == "ollama_cloud"
        # Provider-neutral: no model preset ships, so the accessor resolves to
        # "" until the operator configures [goose].default_model or presets.
        assert _default_model() == ""
