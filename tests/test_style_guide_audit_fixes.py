"""Tests for the style-guide subsystem fixes (B1, B2, B4) and the
Layered-Tom (E3) hydration pipeline (B10).

These tests pin down the regression that the audit fixed:
  - B1: StyleGuideLoader.get_by_stem
  - B2: version_resolver no longer hard-codes a default
  - B4: config.allowlist enforcement
  - B10: render_with_placeholders emits placeholders; hydrate resolves them
"""

from __future__ import annotations

import pytest

# ── B1: StyleGuideLoader.get_by_stem ────────────────────────────────────────


def test_loader_get_by_stem_returns_loaded_guide():
    """B1 regression: get_by_stem must look up by filename stem, not meta.name."""
    from beagle.style_guides.loader import StyleGuideLoader

    loader = StyleGuideLoader()
    guide = loader.get_by_stem("04_lang_python")
    assert guide is not None, "get_by_stem('04_lang_python') returned None"
    meta = guide.get("meta", {})
    assert meta.get("name") == "Python Domain Engineering"


def test_loader_get_by_stem_missing_returns_none():
    """get_by_stem returns None for non-existent stems (no exception)."""
    from beagle.style_guides.loader import StyleGuideLoader

    loader = StyleGuideLoader()
    assert loader.get_by_stem("definitely_not_a_real_guide") is None


def test_loader_get_by_stem_works_for_all_canonical_guides():
    """Every canonical guide has a working get_by_stem lookup."""
    from beagle.style_guides.loader import StyleGuideLoader

    loader = StyleGuideLoader()
    # v14.0: the doctrine guides were renumbered 0X_*.toml in the canonical
    # config root; the legacy canonical stems (beagle_environment, ...) were
    # archived to guides/_archive/. The current top-level SSOT is the
    # renumbered 0X_* set + the surviving unnumbered guides.
    for stem in (
        "01_core_doctrine",
        "02_host_runtime",
        "03_tool_registry",
        "04_lang_python",
        "05_lang_systems",
        "06_domain_frontend",
        "07_domain_devops",
        "08_security_baseline",
        "09_agentic_doctrine_spec",
        "beagle_core_directives",
        "local_tool_inventory",
        "run_to_completion",
    ):
        g = loader.get_by_stem(stem)
        assert g is not None, f"get_by_stem({stem!r}) returned None"
        assert "meta" in g


# ── B2: version_resolver no hard-coded default ─────────────────────────────


def test_version_resolver_raises_without_fallback_chain(tmp_path, monkeypatch):
    """B2 regression: get_model_fallback_chain must raise if config has no chain."""
    from beagle.style_guides import version_resolver

    # Point at a tmp config.toml that has no [goose].fallback_chain
    cfg = tmp_path / "config.toml"
    cfg.write_text("[goose]\nprovider = 'ollama_cloud'\n")
    monkeypatch.setattr(version_resolver, "_resolve_repo_root", lambda *_: tmp_path)
    with pytest.raises(RuntimeError, match="fallback_chain is required"):
        version_resolver.get_model_fallback_chain()


def test_version_resolver_returns_chain_from_config(tmp_path, monkeypatch):
    """When config.toml has [goose].fallback_chain, return it as-is."""
    from beagle.style_guides import version_resolver

    cfg = tmp_path / "config.toml"
    cfg.write_text('[goose]\nfallback_chain = ["minimax-m3", "gemma4:31b"]\n')
    monkeypatch.setattr(version_resolver, "_resolve_repo_root", lambda *_: tmp_path)
    chain = version_resolver.get_model_fallback_chain()
    assert chain == ["minimax-m3", "gemma4:31b"]


def test_version_resolver_primary_is_chain_first(tmp_path, monkeypatch):
    """get_primary_model returns chain[0]."""
    from beagle.style_guides import version_resolver

    cfg = tmp_path / "config.toml"
    cfg.write_text('[goose]\nfallback_chain = ["minimax-m3", "gemma4:31b"]\n')
    monkeypatch.setattr(version_resolver, "_resolve_repo_root", lambda *_: tmp_path)
    assert version_resolver.get_primary_model() == "minimax-m3"


# ── B4: config.allowlist enforcement ───────────────────────────────────────


def test_allowlist_loads_from_config():
    """The allowlist is loaded from config.toml [models.allowed]."""
    from beagle.config.allowlist import allowed_models, reload_allowlist

    reload_allowlist()  # ensure fresh read
    models = allowed_models()
    assert "minimax-m3" in models
    assert "glm-5.2" in models
    assert "deepseek-v4-pro:0813-cloud" in models
    assert "deepseek-v4-flash:0731-cloud" in models
    assert "kimi-k2.6" in models
    assert "gemma4:31b" in models
    assert "nemotron-3-ultra" in models


def test_allowlist_validate_model_accepts_allowed():
    """validate_model returns the model for an allowlisted string."""
    from beagle.config.allowlist import validate_model

    assert validate_model("minimax-m3") == "minimax-m3"


def test_allowlist_validate_model_rejects_not_allowed():
    """validate_model raises ModelNotAllowedError for a non-allowlisted model."""
    from beagle.config.allowlist import ModelNotAllowedError, validate_model

    with pytest.raises(ModelNotAllowedError) as excinfo:
        validate_model("hacker-gpt-99:cloud")
    assert "hacker-gpt-99:cloud" in str(excinfo.value)
    assert "minimax-m3" in str(excinfo.value)


def test_allowlist_validate_model_rejects_empty():
    """validate_model raises ValueError for an empty/non-string input."""
    from beagle.config.allowlist import validate_model

    with pytest.raises(ValueError):
        validate_model("")
    with pytest.raises(ValueError):
        validate_model(None)  # type: ignore[arg-type]


def test_model_resolver_validate_at_boundary(monkeypatch):
    """model_resolver.resolve_model validates every returned string."""
    from beagle.config import model_resolver

    # Force the env-var path: an env var with a non-allowlisted value
    # must raise, not return the string.
    from beagle.config.allowlist import ModelNotAllowedError

    monkeypatch.setenv("GOOSE_MODEL", "hacker-gpt-99:cloud")
    with pytest.raises(ModelNotAllowedError):
        model_resolver.resolve_model()


def test_model_resolver_returns_allowlisted_default(monkeypatch):
    """With no env var, resolve_model returns a model in the allowlist."""
    from beagle.config import model_resolver

    monkeypatch.delenv("GOOSE_MODEL", raising=False)
    monkeypatch.delenv("GOOSE_DEFAULT_MODEL", raising=False)
    model = model_resolver.resolve_model()
    from beagle.config.allowlist import allowed_models

    assert model in allowed_models(), f"resolve_model() returned {model!r}, not in allowlist"


# ── B10: Layered-Tom (E3) hydration pipeline ──────────────────────────────


def test_render_with_placeholders_emits_hydrator_block():
    """render_with_placeholders emits a <hydrator> block with the declared queries."""
    from beagle.style_guides.render import GooseTopOfMindRenderer

    renderer = GooseTopOfMindRenderer()
    xml, queries = renderer.render_with_placeholders()
    assert "<hydrator>" in xml
    assert "</hydrator>" in xml
    assert len(queries) >= 1, "beagle_environment.toml declares 2 rag_queries + 1 chat_query"
    # The block must appear before </beagle_top_of_mind>
    assert xml.find("<hydrator>") < xml.find("</beagle_top_of_mind>")


def test_render_with_placeholders_queries_match_toml():
    """The emitted queries match the [meta] declarations in the TOML guides."""
    from beagle.style_guides.render import GooseTopOfMindRenderer

    renderer = GooseTopOfMindRenderer()
    _xml, queries = renderer.render_with_placeholders()

    # beagle_environment.toml declares 2 rag + 1 chat = 3 queries
    rag_queries = [q for q in queries if q["source"] == "rag"]
    chat_queries = [q for q in queries if q["source"] == "chat"]
    assert len(rag_queries) >= 2, f"expected ≥2 rag queries, got {len(rag_queries)}"
    assert len(chat_queries) >= 1, f"expected ≥1 chat query, got {len(chat_queries)}"
    for q in queries:
        assert q["id"]
        assert q["query"]
        assert q["guide"]


def test_hydrator_resolves_placeholders_synchronously(monkeypatch):
    """hydrate() resolves placeholders via the mock MCP servers."""
    from beagle.style_guides import tom_hydrator
    from beagle.style_guides.render import GooseTopOfMindRenderer

    # Mock the MCP server calls so the test is hermetic.
    # The hydrator's _resolve_one awaits the result of asyncio.to_thread,
    # so the mocks can be sync — asyncio.to_thread will run them in a
    # worker thread and return the result to the event loop.
    def fake_rag(query, max_hops=1, top_k=3):
        return {
            "semantic_anchors": [
                {"file_path": f"fake_{query}.py", "score": 0.95},
            ],
            "structural_relations": [
                {"from": "A", "to": "B", "relation": "CALLS"},
            ],
        }

    def fake_chat(query, limit=10):
        return [
            {"role": "user", "content": f"fake chat for {query[:30]}"},
            {"role": "assistant", "content": "fake response"},
        ]

    # Patch the lazy imports inside tom_hydrator.
    #
    # v13.21.3 (F4 fix): the import path changed from the
    # non-importable ``beagleragserver`` module name to the real
    # in-process path ``beagle.infrastructure.mcp_rag_server.rag_search``.
    # The test seam is now the same module the production code uses;
    # we monkeypatch the function attribute on the module so the
    # hydrator's lazy ``from ... import rag_search`` resolves to our
    # fake. The chat side goes through the new
    # ``_chatrecall_adapter`` module.
    import beagle.infrastructure.mcp_rag_server as _rag_mod
    import beagle.style_guides._chatrecall_adapter as _chat_mod

    monkeypatch.setattr(
        _rag_mod,
        "rag_search",
        staticmethod(lambda query, max_hops=1, top_k=3: fake_rag(query, max_hops, top_k)),
    )
    monkeypatch.setattr(
        _chat_mod, "chatrecall", staticmethod(lambda query, limit=10: fake_chat(query, limit))
    )

    renderer = GooseTopOfMindRenderer()
    xml, queries = renderer.render_with_placeholders()
    hydrated = tom_hydrator.hydrate(xml, queries)
    # v13.21.3 (F5 fix): the opening tag now carries a ``hydrated_at``
    # attribute so ``render_canonical`` can age-check the block. Use a
    # regex that matches the opening tag with any attributes (the
    # timestamp varies per test run).
    import re as _re_attr

    assert _re_attr.search(r"<hydrated\s[^>]*>", hydrated) is not None
    assert "<rag_result " in hydrated
    assert "<chat_result " in hydrated
    # Per-result cap enforced
    assert "fake chat" in hydrated
    # The HYDRATOR_BLOCK_MAX_BYTES cap is on the hydration BLOCK (the
    # <hydrated>...</hydrated> contents), not the whole rendered XML —
    # the rest of the Top-of-Mind artefact (style guides, system
    # identity) is bounded separately by the 25 KB doctrine cap.
    import re as _re

    # v13.21.3 (F5 fix): the opening tag now carries attributes
    # (hydrated_at, source); use a regex that matches any attribute
    # set rather than the bare ``<hydrated>`` literal.
    m = _re.search(r"<hydrated\b[^>]*>(.*?)</hydrated>", hydrated, _re.DOTALL)
    assert m is not None, "hydrated output is missing <hydrated> block"
    block_size = len(m.group(1))
    assert block_size <= tom_hydrator.HYDRATOR_BLOCK_MAX_BYTES, (
        f"hydration block is {block_size} bytes, cap is {tom_hydrator.HYDRATOR_BLOCK_MAX_BYTES}"
    )


def test_hydrator_per_result_cap_enforced(monkeypatch):
    """A 2 KB result is truncated to HYDRATOR_PER_RESULT_MAX_BYTES."""
    from beagle.style_guides import tom_hydrator

    def huge_rag(query, max_hops=1, top_k=3):
        return {
            "semantic_anchors": [
                {"file_path": "/x" * 5000, "score": 0.95},
            ],
            "structural_relations": [],
        }

    # v13.21.3 (F4 fix): patch the real import path, not the
    # non-importable ``beagleragserver`` module name.
    import beagle.infrastructure.mcp_rag_server as _rag_mod
    import beagle.style_guides._chatrecall_adapter as _chat_mod

    monkeypatch.setattr(
        _rag_mod,
        "rag_search",
        staticmethod(lambda query, max_hops=1, top_k=3: huge_rag(query, max_hops, top_k)),
    )
    monkeypatch.setattr(_chat_mod, "chatrecall", staticmethod(lambda query, limit=10: []))

    xml = '<beagle_top_of_mind>\n<hydrator>\n<rag id="rag_0" query="q" guide="g"/>\n</hydrator>\n</beagle_top_of_mind>'
    queries = [{"id": "rag_0", "source": "rag", "query": "q", "guide": "g"}]
    out = tom_hydrator.hydrate(xml, queries)
    # Per-result cap of 1024 + 24-byte truncation marker
    assert "<!-- truncated -->" in out
    assert out.count("<rag_result ") == 1


def test_hydrator_handles_failed_query_gracefully(monkeypatch):
    """A failing MCP query produces an empty result, not an exception."""
    from beagle.style_guides import tom_hydrator

    def broken_chat(query, limit=10):
        raise RuntimeError("MCP server down")

    def empty_rag(query, max_hops=1, top_k=3):
        return {"semantic_anchors": [], "structural_relations": []}

    # v13.21.3 (F4 fix): patch the real import path.
    import beagle.infrastructure.mcp_rag_server as _rag_mod
    import beagle.style_guides._chatrecall_adapter as _chat_mod

    monkeypatch.setattr(
        _rag_mod,
        "rag_search",
        staticmethod(lambda query, max_hops=1, top_k=3: empty_rag(query, max_hops, top_k)),
    )
    monkeypatch.setattr(
        _chat_mod, "chatrecall", staticmethod(lambda query, limit=10: broken_chat(query, limit))
    )

    xml = '<beagle_top_of_mind>\n<hydrator>\n<chat id="chat_0" query="q" guide="g"/>\n</hydrator>\n</beagle_top_of_mind>'
    queries = [{"id": "chat_0", "source": "chat", "query": "q", "guide": "g"}]
    out = tom_hydrator.hydrate(xml, queries)
    assert "<chat_result " in out
    # The content is empty but the block is still there (placeholder for
    # the watchdog to detect degraded mode)
    assert '<chat_result id="chat_0"' in out
