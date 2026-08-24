"""F6 regression — model chain cross-validated against runtime allowlist.

The v13.21 audit flagged that ``[goose].fallback_chain`` (read by
``style_guides/version_resolver.py``) and ``[models.allowed]`` (read by
``config/allowlist.py``) are *two* config readers for the model space.
They serve different purposes — the chain is routing intent (which
model to try next on failure), the allowlist is the security perimeter
(which model strings are legal at all) — so the duality is intentional.
The defect was that a chain entry NOT in the allowlist would pass
``get_model_fallback_chain`` silently and only fail at the LLM call
boundary (after the network round-trip, with a 404 from Ollama Cloud).

The fix has two parts:

1. ``config/allowlist.py`` exposes ``validate_against_allowlist(models,
   on_violation=...)`` with three modes (``raise``, ``warn``, ``filter``).
2. ``style_guides/version_resolver.get_model_fallback_chain`` calls it
   on every invocation, raising ``ModelNotAllowedError`` on the first
   violator.

This file pins both the unit-level behaviour of
``validate_against_allowlist`` and the integration that
``get_model_fallback_chain`` rejects a tampered chain.
"""

from __future__ import annotations

import pytest

# ── Unit tests for validate_against_allowlist ─────────────────────────────


def test_validate_against_allowlist_empty_input():
    """Empty input returns empty (no work to do, no error)."""
    from beagle.config.allowlist import validate_against_allowlist

    assert validate_against_allowlist([]) == []
    assert validate_against_allowlist([], on_violation="warn") == []
    assert validate_against_allowlist([], on_violation="filter") == []


def test_validate_against_allowlist_all_in_allowlist():
    """All-valid chain returns the input unchanged."""
    from beagle.config.allowlist import validate_against_allowlist

    chain = ["minimax-m3", "gemma4:31b"]
    assert validate_against_allowlist(chain) == chain
    assert validate_against_allowlist(chain, on_violation="warn") == chain
    assert validate_against_allowlist(chain, on_violation="filter") == chain


def test_validate_against_allowlist_raise_on_violator():
    """A violator raises ModelNotAllowedError naming the first offender."""
    from beagle.config.allowlist import (
        ModelNotAllowedError,
        validate_against_allowlist,
    )

    with pytest.raises(ModelNotAllowedError) as excinfo:
        validate_against_allowlist(["minimax-m3", "hacker-gpt-99:cloud", "gemma4:31b"])
    # The first violator is named in the exception.
    assert excinfo.value.model == "hacker-gpt-99:cloud"
    # The full allowlist is in the exception so the operator can fix
    # config.toml without a second tool call.
    assert "minimax-m3" in excinfo.value.allowed
    assert "gemma4:31b" in excinfo.value.allowed


def test_validate_against_allowlist_warn_keeps_input():
    """'warn' mode returns the input unchanged and logs."""
    from beagle.config.allowlist import validate_against_allowlist

    chain = ["minimax-m3", "not-in-allowlist:cloud"]
    result = validate_against_allowlist(chain, on_violation="warn")
    assert result == chain  # unchanged
    # The violator is still in the result; 'warn' is observational, not
    # corrective.
    assert "not-in-allowlist:cloud" in result


def test_validate_against_allowlist_filter_drops_violators():
    """'filter' mode returns only the models that pass."""
    from beagle.config.allowlist import validate_against_allowlist

    chain = ["minimax-m3", "hacker-gpt:cloud", "gemma4:31b"]
    result = validate_against_allowlist(chain, on_violation="filter")
    assert "minimax-m3" in result
    assert "gemma4:31b" in result
    assert "hacker-gpt:cloud" not in result


def test_validate_against_allowlist_rejects_bad_mode():
    """An unknown on_violation value raises ValueError (loud, not silent)."""
    from beagle.config.allowlist import validate_against_allowlist

    with pytest.raises(ValueError, match="on_violation must be one of"):
        validate_against_allowlist(["x"], on_violation="explode")


# ── Integration: get_model_fallback_chain enforces the invariant ──────────


def test_get_model_fallback_chain_passes_when_allowlisted(tmp_path, monkeypatch):
    """The shipped config.toml chain is a subset of the allowlist."""
    from beagle.config import allowlist
    from beagle.style_guides import version_resolver

    # Both the allowlist and the version_resolver must read from the
    # tmp config; clear the allowlist cache to force a re-read.
    monkeypatch.setattr(allowlist, "_ALLOWED_CACHE", None)
    # v1.0.0: allowlist resolves its config through the shared
    # _config_path.find_config_toml (bound as _find_config_toml at import),
    # not a module-level _CONFIG_PATH constant — patching the old name
    # raised AttributeError.
    monkeypatch.setattr(allowlist, "_find_config_toml", lambda: tmp_path / "config.toml")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[models.allowed]\n"
        '"minimax-m3:cloud" = true\n'
        '"gemma4:31b-cloud" = true\n'
        "[goose]\n"
        'fallback_chain = ["minimax-m3:cloud", "gemma4:31b-cloud"]\n'
    )
    monkeypatch.setattr(version_resolver, "_resolve_repo_root", lambda *_: tmp_path)
    chain = version_resolver.get_model_fallback_chain()
    assert chain == ["minimax-m3:cloud", "gemma4:31b-cloud"]


def test_get_model_fallback_chain_rejects_chain_with_non_allowlisted_model(tmp_path, monkeypatch):
    """A chain entry not in the allowlist is a config error, not a warning.

    Without the F6 fix, this would have returned the chain unchanged and
    failed later at the LLM call boundary (Ollama Cloud 404). With the
    fix, the error is raised at the first call site that resolves the
    chain — typically the model_resolver at workflow startup.
    """
    from beagle.config import allowlist
    from beagle.config.allowlist import ModelNotAllowedError
    from beagle.style_guides import version_resolver

    # Both the allowlist reader and the version_resolver reader use
    # hard-coded paths. The version_resolver goes through
    # ``_resolve_repo_root`` (which we monkeypatch to tmp_path); the
    # allowlist uses its own ``_CONFIG_PATH`` attribute (a Path
    # constructed at module import time), so we monkeypatch that too.
    # Clear the allowlist cache to force a re-read from the new path.
    monkeypatch.setattr(allowlist, "_ALLOWED_CACHE", None)
    # v1.0.0: allowlist resolves its config through the shared
    # _config_path.find_config_toml (bound as _find_config_toml at import),
    # not a module-level _CONFIG_PATH constant — patching the old name
    # raised AttributeError.
    monkeypatch.setattr(allowlist, "_find_config_toml", lambda: tmp_path / "config.toml")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[models.allowed]\n"
        '"minimax-m3:cloud" = true\n'
        # NOTE: gemma4 is NOT in the allowlist
        "[goose]\n"
        'fallback_chain = ["minimax-m3:cloud", "gemma4:31b-cloud"]\n'
    )
    monkeypatch.setattr(version_resolver, "_resolve_repo_root", lambda *_: tmp_path)
    with pytest.raises(ModelNotAllowedError) as excinfo:
        version_resolver.get_model_fallback_chain()
    assert excinfo.value.model == "gemma4:31b-cloud"


def test_get_model_fallback_chain_works_against_shipped_config():
    """End-to-end: the shipped config.toml's chain passes the validator.

    This is the F6 acceptance test. If the shipped chain ever drifts out
    of the shipped allowlist, this test fails loudly at CI time instead
    of at the first LLM call in production.
    """
    from beagle.style_guides import version_resolver

    chain = version_resolver.get_model_fallback_chain()
    # Every entry of the chain must be in the shipped allowlist.
    from beagle.config.allowlist import allowed_models

    allowed = allowed_models()
    for model in chain:
        assert model in allowed, (
            f"Chain entry {model!r} not in shipped allowlist {sorted(allowed)!r}. "
            f"This is the F6 invariant — see tests/test_f6_chain_allowlist_consistency.py."
        )
