"""v13.15.4: regression coverage for the Beagle Core Directives style guide.

The directives migrated from .goosehints v9.0 → v10.0 must remain present in
the style guide so they keep auto-injecting via the injector on every file
edit. This test guards against accidental removal of load-bearing rules.
"""

from __future__ import annotations

import pytest

from beagle.style_guides.injector import ContextInjector
from beagle.style_guides.loader import StyleGuideLoader


@pytest.fixture(scope="module")
def loader() -> StyleGuideLoader:
    return StyleGuideLoader()


def test_beagle_core_directives_guide_present(loader: StyleGuideLoader):
    assert "Beagle Core Directives" in loader.available, (
        "Beagle Core Directives guide missing — migration from .goosehints v9.0 broken"
    )


def test_beagle_core_directives_is_universal(loader: StyleGuideLoader):
    """The guide must apply to every file extension (wildcard).

    If applies_to is narrowed, the orchestrator's core behavioural rules
    will only fire on a subset of edits — exactly the regression the
    migration intended to prevent.
    """
    guide = loader.get("Beagle Core Directives")
    assert guide is not None
    assert guide["meta"]["applies_to"] == ["*"]


def test_load_bearing_directives_present(loader: StyleGuideLoader):
    """Every directive originally in .goosehints v9.0 core_directives + forbidden
    blocks must appear (by keyword) in the migrated guide. If you remove one of
    these from the TOML without updating this list, you're degrading the
    orchestrator's contract.
    """
    guide = loader.get("Beagle Core Directives")
    patterns_text = " ".join(guide["architecture"]["patterns"])
    forbidden_text = " ".join(guide["anti_patterns"]["forbidden"])
    formatting_text = " ".join(str(v) for v in guide.get("formatting", {}).values())
    formatting_keys = " ".join(guide.get("formatting", {}).keys())
    combined = (
        patterns_text + " " + forbidden_text + " " + formatting_text + " " + formatting_keys
    ).lower()

    required_keywords = [
        "system-2",  # System-2 Deliberation directive
        "xml substrate",  # XML Substrate directive
        "ace delta",  # ACE Delta Updates directive
        "cvcp",  # CVCP / Adversarial Review directive
        "context folding",  # Context Folding directive
        "autonomous resume",  # Autonomous Resume directive
        "l-v-v-v-a-r-c",  # File mutation cycle
        "edge_token",  # Edge Token Discipline (key name in formatting)
        "datetime.now",  # Datetime convention
        "uuid.uuid4",  # UUID convention
        "path.relative_to",  # Filesystem containment
        "additionalproperties",  # MCP schema convention
        "final_answer",  # Output-closure contract (added in v13.15.1)
        "fix features",  # Don't-degrade rule (from feedback_fix_features memory)
    ]
    missing = [k for k in required_keywords if k not in combined]
    assert not missing, (
        f"Beagle Core Directives missing load-bearing keywords: {missing}. "
        "These directives were in .goosehints v9.0 and must remain in the migration."
    )


def test_guide_injects_on_arbitrary_extension():
    """Wildcard applies_to means injection on .py, .yaml, .rs, anything."""
    injector = ContextInjector()
    for ext in [".py", ".yaml", ".toml", ".md", ".rs", ".go", ".cpp"]:
        out = injector.inject(ext)
        assert "Beagle Core Directives" in out, (
            f"Core directives did not inject on {ext} — wildcard match broken"
        )


def test_goosehints_is_stub_only():
    """v13.15.6: .goosehints is a pointer stub + optional auto-regenerated
    session-start XML block from style_guides/guides/beagle_environment.toml.

    Allowed shape:
        <goose_beagle_pointer version="...">...</goose_beagle_pointer>
        [optional] <beagle_session_start>...auto-generated...</beagle_session_start>

    Forbidden: behavioural rules written directly into the body (those
    belong in the TOML style guides and reach goose via tom or
    beagle_session_bootstrap, never as hand-edited hints content).
    """
    from pathlib import Path

    hints_path = Path(__file__).resolve().parents[1] / ".goosehints"
    if not hints_path.exists():
        pytest.skip(".goosehints not at expected path")
    text = hints_path.read_text().lower()

    # Must contain the pointer tag
    assert "goose_beagle_pointer" in text, (
        ".goosehints missing <goose_beagle_pointer> — expected v11.0+ stub"
    )
    assert "goose_moim_message_file" in text, (
        ".goosehints must reference the GOOSE_MOIM_MESSAGE_FILE env var"
    )

    # Strip the auto-generated session-start XML block from the body before
    # the migrated-tag tripwire fires.  Environment tags (env_vars,
    # mcp_servers, etc.) are legitimately re-emitted by `beagle render-hints`
    # from beagle_environment.toml — those occurrences are not regressions.
    body = text
    ss_start = body.find("<beagle_session_start")
    ss_end = body.find("</beagle_session_start>")
    if ss_start >= 0 and ss_end > ss_start:
        body = body[:ss_start] + body[ss_end + len("</beagle_session_start>") :]

    # After stripping the auto-generated block, the residue must not contain
    # any of the old v9.0/v10.0 hand-edited sections.
    forbidden_top_level_sections = [
        "<core_directives>",
        "<cognitive_loop>",
        "<output_schema_protocol>",
        "<delegation_protocol>",
        "<goose_beagle_system_prompt",  # the v9.0 root
        "<project_contract>",  # v10.0 project contract section
        "<session_startup_pointer>",
    ]
    remaining = [t for t in forbidden_top_level_sections if t in body]
    assert not remaining, (
        ".goosehints body (outside the auto-generated <beagle_session_start> "
        f"block) still contains migrated sections: {remaining}"
    )

    # Behavioural phrases must NOT appear in the stub body
    behavioural_phrases = [
        "system-2 deliberation",
        "ace delta updates",
        "cognitive loop phase",
    ]
    duplicated = [p for p in behavioural_phrases if p in body]
    assert not duplicated, f".goosehints stub re-introduced behavioural phrases: {duplicated}"


def test_beagle_core_directives_in_top_of_mind_render():
    """v13.15.6: the rendered Top-of-Mind XML must contain the load-bearing
    keywords from beagle_core_directives.toml. This replaces the old
    test_guide_injects_on_arbitrary_extension which tested the now-irrelevant
    per-file-edit injection path for behavioural directives.
    """
    from beagle.style_guides.render import GooseTopOfMindRenderer

    renderer = GooseTopOfMindRenderer()
    output = renderer.render()
    assert output, "Top-of-Mind renderer produced empty output"
    lowered = output.lower()

    required_keywords = [
        "system-2",
        "xml substrate",
        "ace delta",
        "cvcp",
        "context folding",
        "autonomous resume",
        "l-v-v-v-a-r-c",
        "edge_token",
        "datetime.now",
        "uuid.uuid4",
        "check_and_fold_context",  # the exact rule whose absence caused the 245k/128k blowup
        "additionalproperties",
        "final_answer",
        "fix features",
    ]
    missing = [k for k in required_keywords if k not in lowered]
    assert not missing, f"Top-of-Mind renderer missing load-bearing keywords: {missing}"
