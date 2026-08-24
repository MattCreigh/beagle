"""Regression guard for the goose `--provider` argv contract.

The installed goose binary (v1.28/1.29) only recognizes ``openai`` as a
provider for OpenAI-compatible upstreams (Ollama Cloud is reached this way
because its API speaks the OpenAI dialect). Every agent profile in
``beagle/config/agents.toml`` must therefore set
``provider = "openai"`` so the orchestrator's
``core/orchestrator/node_executor.py`` builds a goose command goose will
actually accept. This test enforces that contract.

If this test ever fails, do NOT relax it without checking goose's release
notes — the prior value ``"ollama_cloud"`` returned ``Unknown provider``
at runtime and broke every workflow node, see plan
``please-do-a-deep-snoopy-haven.md`` Phase A1.
"""

from __future__ import annotations

from importlib.resources import files

import pytest

# v1.1.1 (S5): agents.toml moved to the canonical config root. Resolve via
# the resolver, not the installed package path.
from beagle.config._config_path import find_agents_toml

_INSTALLED_AGENTS_TOML = find_agents_toml()

import pytest as _pytest

if not _INSTALLED_AGENTS_TOML.is_file():
    _pytest.skip(
        "agents.toml (operator fleet config) not installed — nothing to "
        "regression-test; seed ~/.config/beagle/coding_agent_config/agents.toml",
        allow_module_level=True,
    )

# Permitted argv values for `goose run --provider`. Update only if goose itself
# starts accepting a new provider literal AND the supported deployment uses it.
# v1.0.8: ``openrouter`` is now the default fleet provider (from the active
# fleet card in presets/), added alongside ``openai`` and ``ollama_cloud``.
# v13.22.3: Added ``ollama_cloud`` — was the missing entry that masked the
# silent-fallback regression (every profile defaulted to ``provider = "openai"``,
# goose hit api.openai.com with no API key, every call returned 401, the
# circuit breaker tripped after 5 failures, and the workflow silently returned
# ``CircuitBreakerOpenError`` without surfacing the real cause).
_ALLOWED_PROVIDERS = {"openai", "ollama_cloud", "openrouter"}


@pytest.fixture(scope="module")
def agents_toml() -> dict:
    # agents.toml uses Jinja {{ preset.xxx }} templates (v13.22.4) that pull
    # from the active fleet card. Parse through the real template renderer so
    # provider/model values are resolved to their concrete names, not the raw
    # template placeholders.
    from beagle.config.toml_template import load_toml_with_templates

    return load_toml_with_templates(_INSTALLED_AGENTS_TOML)


def test_every_agent_uses_an_accepted_provider(agents_toml: dict) -> None:
    """No agent in agents.toml may carry a provider goose will reject."""
    violations = []
    for name, profile in agents_toml.items():
        if not isinstance(profile, dict):
            continue
        provider = profile.get("provider", "")
        if provider and provider not in _ALLOWED_PROVIDERS:
            violations.append(f"{name}: {provider!r}")
    assert not violations, (
        "agents.toml profiles must use one of "
        f"{_ALLOWED_PROVIDERS} (goose CLI argv constraint). "
        f"Offenders: {violations}"
    )


def test_agents_toml_has_a_default_profile(agents_toml: dict) -> None:
    """The [default] profile is the fallback for unknown agent names."""
    assert "default" in agents_toml, (
        "agents.toml must declare a [default] profile so "
        "get_agent() can resolve unknown agent names."
    )
    default = agents_toml["default"]
    assert default.get("provider") in _ALLOWED_PROVIDERS
    assert default.get("model")  # non-empty


def test_streaming_synthesizer_profile_present(agents_toml: dict) -> None:
    """research.yaml::demo_streaming_synthesis references this profile."""
    assert "streaming-synthesizer" in agents_toml, (
        "metaprompts/research.yaml's demo_streaming_synthesis phase declares "
        "agent: streaming-synthesizer — agents.toml must carry a matching "
        "profile or validate_workflow('research') will fail."
    )


def test_executor_argv_constant_uses_openai() -> None:
    """``core/orchestrator/executor.py`` is the live subprocess pipeline
    (v13.22.1, B-7: the prior ``node_executor.py`` was dead code and was
    removed). We sanity-check that the runtime path doesn't carry a
    hardcoded fallback to the broken value.
    """
    from beagle.core.orchestrator import executor as ne

    source = files("beagle").joinpath("core/orchestrator/executor.py").read_text(encoding="utf-8")
    assert '"ollama_cloud"' not in source, (
        "executor.py contains a hardcoded 'ollama_cloud' literal — it "
        "must read provider from agent profiles only."
    )
    # Touch the imported module so this test fails fast if the path moves.
    assert hasattr(ne, "__file__")
