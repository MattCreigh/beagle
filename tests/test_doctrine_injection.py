"""v13.15.7 — Beagle doctrine delivery via render_structured + inject_into_directive.

These tests cover the two new Beagle-native delivery channels that replace
goose's tom-extension monopoly on doctrine delivery:

  1. render_structured() — returns the rendered doctrine as a JSON-friendly
     dict, served by beagle_session_bootstrap so every MCP client sees it.
  2. inject_into_directive() — prepends a compact form of the doctrine to
     any system_directive passed to execute_goose_with_fallback, so
     Beagle-spawned subagents inherit the contract without depending on tom.
"""

from __future__ import annotations

import json

import pytest

from beagle.style_guides.render import GooseTopOfMindRenderer


@pytest.fixture(scope="module")
def renderer() -> GooseTopOfMindRenderer:
    return GooseTopOfMindRenderer()


# ── render_structured ────────────────────────────────────────────────────


def test_render_structured_returns_dict_with_expected_keys(renderer):
    bundle = renderer.render_structured()
    assert isinstance(bundle, dict)
    assert bundle.get("version") == "1.0"
    assert bundle.get("domain") == "universal"
    assert isinstance(bundle.get("guides"), list)
    assert len(bundle["guides"]) >= 1


def test_render_structured_each_guide_has_name(renderer):
    bundle = renderer.render_structured()
    for guide in bundle["guides"]:
        assert "name" in guide
        assert isinstance(guide["name"], str)
        assert guide["name"]


def test_render_structured_includes_beagle_core_directives(renderer):
    bundle = renderer.render_structured()
    names = [g["name"] for g in bundle["guides"]]
    assert "Beagle Core Directives" in names, (
        f"Core Directives must be in the universal bundle, got {names}"
    )


def test_render_structured_is_json_serialisable_under_cap(renderer):
    bundle = renderer.render_structured()
    serialised = json.dumps(bundle, ensure_ascii=False)
    assert len(serialised) <= renderer.DOCTRINE_BUNDLE_MAX_BYTES, (
        f"Bundle serialised at {len(serialised)} bytes, exceeds "
        f"{renderer.DOCTRINE_BUNDLE_MAX_BYTES} cap"
    )


def test_render_structured_preserves_load_bearing_keywords(renderer):
    """Core Directives must include the routing protocol + Context Folding rule."""
    bundle = renderer.render_structured()
    core = next(
        (g for g in bundle["guides"] if g["name"] == "Beagle Core Directives"),
        None,
    )
    assert core is not None
    serialised = json.dumps(core, ensure_ascii=False)
    for keyword in ("CRITICAL_ROUTING_PROTOCOL", "Context Folding", "check_and_fold_context"):
        assert keyword in serialised, (
            f"Load-bearing keyword {keyword!r} missing from rendered Core Directives"
        )


# ── inject_into_directive ────────────────────────────────────────────────


def test_inject_into_directive_preserves_original_tail(renderer):
    original = "You are an audit subagent. Find security issues."
    out = renderer.inject_into_directive(original)
    assert out.endswith(original), "Original directive must remain intact at the tail"


def test_inject_into_directive_contains_critical_sections(renderer):
    out = renderer.inject_into_directive("X")
    # v13.21.3: Subagents are LEAF EXECUTORS. The subagent inject path
    # now emits the EXECUTOR_PROTOCOL (their role), NOT the controller's
    # CRITICAL_ROUTING_PROTOCOL. The master goose session still gets the
    # controller's protocol via the tom extension. This is the F2 fix.
    assert "EXECUTOR_PROTOCOL" in out, "Executor protocol must survive cap"
    assert "Forbidden" in out, "Forbidden anti-patterns must survive cap"
    # Patterns are droppable but at least one should fit
    assert "Patterns" in out or "patterns" in out.lower()


def test_inject_into_directive_stays_under_cap(renderer):
    original = "X"
    out = renderer.inject_into_directive(original)
    # The doctrine portion (everything before original tail) must be ≤ 16 KB
    # (DOCTRINE_DIRECTIVE_MAX_BYTES was raised from 8 KB → 16 KB in v13.22.3)
    doctrine_portion = out[: -len(original)].rstrip("\n")
    assert len(doctrine_portion) <= renderer.DOCTRINE_DIRECTIVE_MAX_BYTES, (
        f"Doctrine portion is {len(doctrine_portion)} bytes, exceeds "
        f"{renderer.DOCTRINE_DIRECTIVE_MAX_BYTES} cap (16 KB, v13.22.3)"
    )


def test_inject_into_directive_is_idempotent(renderer):
    """Calling twice must produce the same output — header check prevents
    double-injection across recipe nesting."""
    original = "Some subagent prompt."
    once = renderer.inject_into_directive(original)
    twice = renderer.inject_into_directive(once)
    assert once == twice, "inject_into_directive must be idempotent"


# ── config flag ──────────────────────────────────────────────────────────


def test_config_flag_default_is_true():
    """GooseConfig.doctrine_inject_into_subagents must default to True so
    the subagent injection happens out of the box."""
    from beagle.config.schema import GooseConfig

    cfg = GooseConfig()
    assert cfg.doctrine_inject_into_subagents is True, (
        "Doctrine injection must default ON — disabling is the explicit choice"
    )


def test_config_flag_can_be_disabled():
    """Flag must accept False so tests / quarantine environments can
    bypass injection."""
    from beagle.config.schema import GooseConfig

    cfg = GooseConfig(doctrine_inject_into_subagents=False)
    assert cfg.doctrine_inject_into_subagents is False


# ── render_markdown (v13.15.8 — Phase C) ─────────────────────────────────


def test_render_markdown_emits_doctrine_header(renderer):
    md = renderer.render_markdown()
    assert "<!-- AUTO-GENERATED by `beagle render-docs`. Do not edit. -->" in md
    assert "# Beagle Doctrine" in md
    assert "Domain: **universal**" in md


def test_render_markdown_includes_routing_protocol_table(renderer):
    md = renderer.render_markdown()
    assert "### CRITICAL_ROUTING_PROTOCOL" in md
    assert "| Field | Value |" in md, "Routing protocol must render as a Markdown table"
    assert "| `contract` |" in md


def test_render_markdown_includes_patterns_and_forbidden(renderer):
    md = renderer.render_markdown()
    assert "### Patterns" in md
    assert "### Forbidden" in md
    # Spot-check a load-bearing pattern survives the markdown render
    assert "Context Folding" in md
    assert "check_and_fold_context" in md


def test_render_docs_writes_file(tmp_path, renderer):
    """render_docs writes the markdown to disk atomically."""
    target = tmp_path / "DOCTRINE.md"
    out = renderer.render_docs(path=target)
    assert out == target
    assert target.exists()
    content = target.read_text()
    assert content.startswith("<!-- AUTO-GENERATED")
    assert "# Beagle Doctrine" in content
