"""SP-5: tests for re-export shims (utils/tracing, config/config).

beagle-spotless-phase2, work package SP-5. Two modules are backward-compat
re-export shims that had zero direct coverage: utils/tracing (→ observability
.tracing) and config/config (→ the split config sub-modules). These assert the
shims re-export the canonical symbols.
"""

from __future__ import annotations

import importlib


def test_utils_tracing_reexports_canonical() -> None:
    """utils/tracing re-exports the observability.tracing public API."""
    from beagle import observability, utils

    utils_tracing = importlib.import_module("beagle.utils.tracing")
    obs_tracing = importlib.import_module("beagle.observability.tracing")
    for name in utils_tracing.__all__:
        assert getattr(utils_tracing, name) is getattr(obs_tracing, name), name
    # Sanity: canonical module exposes the expected symbols.
    assert obs_tracing is observability.tracing
    assert utils.tracing is not None


def test_config_config_reexports_loader() -> None:
    """config/config re-exports get_config from the loader."""
    from beagle.config import config, loader

    assert config.get_config is loader.get_config


def test_config_config_reexports_schema_types() -> None:
    """config/config re-exports the WorkflowConfig dataclass."""
    from beagle.config import config, schema

    assert config.WorkflowConfig is schema.WorkflowConfig


def test_config_config_reexports_env_overrides() -> None:
    """config/config re-exports apply_env_overrides."""
    from beagle.config import config, env_overrides

    assert config.apply_env_overrides is env_overrides.apply_env_overrides
