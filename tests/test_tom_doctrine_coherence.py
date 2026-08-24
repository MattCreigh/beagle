"""v13.21.3: Top-of-Mind doctrine coherence tests.

Beagle v13.21 — TOM is the sole source of delegation doctrine. This module
pins the invariant mechanically so a future re-hardcoding of behavioural
directives in the renderer is rejected at test time, not discovered live.

Findings pinned:
  F1 (HIGH) — A hardcoded "<mandate>Execute work DIRECTLY ... Delegation
              OPTIONAL</mandate>" was previously embedded in
              style_guides/render.py alongside the TOML's
              "DELEGATION IS ALWAYS CORRECT", producing a self-contradictory
              Top-of-Mind every turn.
  F2 (HIGH) — The master session and the subagent got DIFFERENT doctrine;
              the master heard the (contradictory) execution mandate, the
              subagents heard the controller's routing protocol. The split
              was inverted for their roles.

This file enforces:
  1. test_renderer_has_no_hardcoded_delegation_mandate
       - Source-level anti-drift guard on style_guides/render.py. Fails the
         moment anyone re-hardcodes a delegation/execution policy literal.
  2. test_tom_delegation_doctrine_single_sourced
       - The rendered ToM must contain the TOML stance and MUST NOT contain
         the contradictory stance.
  3. test_tom_still_well_formed
       - The rendered ToM is well-formed XML and still carries the
         license_to_deviate + beagle_system_identity elements.
  4. test_subagent_doctrine_is_executor_role
       - The subagent inject path emits the EXECUTOR_PROTOCOL, NOT the
         controller's CRITICAL_ROUTING_PROTOCOL ("DELEGATION IS ALWAYS
         CORRECT"), and stays under the 16 KB cap
        (DOCTRINE_DIRECTIVE_MAX_BYTES = 16_000, v13.22.3).
"""

from __future__ import annotations

import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from beagle.config._config_path import find_guides_dir
from beagle.style_guides.render import GooseTopOfMindRenderer

# ── Constants — single source of truth for the doctrine strings ────────
# The TOML is the SSOT; the renderer must not embed the contradictory text.

CONTROLLER_DOCTRINE = "DELEGATION IS ALWAYS CORRECT"
CONTRADICTORY_DOCTRINE = "Delegation to run_beagle_workflow is OPTIONAL"
EXECUTOR_DOCTRINE = "Do NOT call run_beagle_workflow"
HARDCODED_EXECUTE_MANDATE = "Execute work DIRECTLY"

# Project layout (tests live at <repo>/tests/, source at <repo>/...)
REPO_ROOT = Path(__file__).resolve().parent.parent
# Both paths gained the `beagle` package segment in the src-layout
# restructure (7a721ab). They were not updated here, so the two renderer
# anti-drift guards below asserted against a file that does not exist.
RENDERER_PATH = REPO_ROOT / "src" / "beagle" / "style_guides" / "render.py"
# v1.1.1 (S6): guides moved to /home/Beagle_Config/style_guides/guides.
CORE_TOML_PATH = find_guides_dir() / "beagle_core_directives.toml"


@pytest.fixture(scope="module")
def renderer() -> GooseTopOfMindRenderer:
    return GooseTopOfMindRenderer()


# ── 1. Anti-drift guard on the renderer source ─────────────────────────


def test_renderer_has_no_hardcoded_delegation_mandate():
    """The renderer must not contain a hardcoded delegation/execution
    mandate literal. Behaviour is owned by the style-guide TOML (SSOT);
    code may only RENDER doctrine from TOML, never assert its own mandate.

    On pre-A1 code, this test FAILS with a clear message identifying the
    drift. On post-A1 code, the hardcoded "<mandate>Execute work
    DIRECTLY…</mandate>" line is gone — replaced by a neutral XML
    comment — and the test passes.
    """
    assert RENDERER_PATH.is_file(), f"Renderer not found at {RENDERER_PATH}"
    src = RENDERER_PATH.read_text(encoding="utf-8")

    assert HARDCODED_EXECUTE_MANDATE not in src, (
        f"style_guides/render.py contains the hardcoded literal "
        f"'{HARDCODED_EXECUTE_MANDATE}'. Behaviour is owned by the "
        f"style-guide TOML (the SSOT). Remove the hardcoded mandate and "
        f"let the [CRITICAL_ROUTING_PROTOCOL] table in "
        f"beagle_core_directives.toml be the sole source of delegation "
        f"doctrine. See tests/test_tom_doctrine_coherence.py and the "
        f"style-guide anti-pattern 'Never hardcode behavioural, routing, "
        f"or delegation directives in renderer/Python code'."
    )
    assert CONTRADICTORY_DOCTRINE not in src, (
        f"style_guides/render.py contains the contradictory literal "
        f"'{CONTRADICTORY_DOCTRINE}'. The renderer must not assert a "
        f"delegation policy that opposes the TOML's CRITICAL_ROUTING_PROTOCOL."
    )


# ── 1b. Anti-drift guard: no hardcoded disposition / auto-delete logic ──


# Literals that, if present in the renderer, would indicate a hardcoded
# attempt to bypass the PRESERVED_FILE_DISPOSITION rule (which requires
# end-of-run user approval before any final disposition of moved-aside
# files). The renderer is a docstring/comment surface, not an executor
# of disposition — it must not contain a unilateral-delete or
# "ship-the-stale-file-anyway" command.
RENDERER_DISPOSITION_BYPASS_LITERALS = (
    "os.unlink(*.superseded",
    "auto-delete preserved",
    "ship with stale .bak",
    "rm -rf audit/",
    "rm -rf replays/",
    "rm -rf embedding_cache/",
)


def test_renderer_has_no_hardcoded_disposition_bypass():
    """The renderer must not contain a hardcoded disposition / delete
    literal. The PRESERVED_FILE_DISPOSITION rule (see
    beagle_core_directives.toml) requires the agent to surface a
    <pending_disposition> block in the final report and to await
    explicit user approval before any final disposition of moved-aside
    files. A hardcoded 'os.unlink(...)' or 'rm -rf audit/' literal in
    the renderer would bypass that rule.

    This is the renderer-side anti-drift guard paired with
    test_core_directives_toml_has_preserved_file_disposition_rule's
    doctrine-level prevention.
    """
    assert RENDERER_PATH.is_file(), f"Renderer not found at {RENDERER_PATH}"
    src = RENDERER_PATH.read_text(encoding="utf-8")

    for literal in RENDERER_DISPOSITION_BYPASS_LITERALS:
        assert literal not in src, (
            f"style_guides/render.py contains the disposition-bypass "
            f"literal {literal!r}. The PRESERVED_FILE_DISPOSITION rule "
            f"requires end-of-run user approval before any final "
            f"disposition of preserved-aside files. Remove the "
            f"hardcoded delete literal — let the agent surface a "
            f"<pending_disposition> block in the final report and "
            f"await explicit user approval."
        )


# ── 2. Rendered ToM is single-sourced and contradiction-free ───────────


def test_tom_delegation_doctrine_single_sourced(
    renderer: GooseTopOfMindRenderer,
):
    """The rendered Top-of-Mind must carry the TOML's delegation stance
    ('DELEGATION IS ALWAYS CORRECT') and MUST NOT carry the
    contradictory hardcoded stance ('Delegation to run_beagle_workflow is
    OPTIONAL'). The TOML is the SSOT — a single delegation doctrine
    must reach the model.
    """
    output = renderer.render()
    assert output, "Renderer produced empty output"

    assert CONTROLLER_DOCTRINE in output, (
        f"Rendered ToM is missing the TOML's delegation stance "
        f"'{CONTROLLER_DOCTRINE}'. Verify the [CRITICAL_ROUTING_PROTOCOL] "
        f"section in beagle_core_directives.toml still renders into the ToM."
    )
    assert CONTRADICTORY_DOCTRINE not in output, (
        f"Rendered ToM contains the contradictory literal "
        f"'{CONTRADICTORY_DOCTRINE}'. This is the F1 defect: a "
        f"hardcoded mandate in render.py that opposes the TOML. "
        f"Delete the hardcoded <mandate> line from style_guides/render.py."
    )


# ── 3. Rendered ToM is well-formed and the required elements survive ──


def test_tom_still_well_formed(renderer: GooseTopOfMindRenderer):
    """The rendered ToM must still be well-formed XML and still carry the
    license_to_deviate and beagle_system_identity elements — removing the
    hardcoded mandate must not break the document shape.
    """
    output = renderer.render()
    assert output, "Renderer produced empty output"

    # Must parse as XML.
    try:
        ET.fromstring(output)
    except ET.ParseError as e:
        pytest.fail(f"Rendered ToM is invalid XML: {e}\n{output[:2000]}")

    # Required elements still present.
    assert "<license_to_deviate" in output, (
        "Rendered ToM is missing the <license_to_deviate> element."
    )
    assert "<beagle_system_identity" in output, (
        "Rendered ToM is missing the <beagle_system_identity> element."
    )


# ── 4. Subagent doctrine is the EXECUTOR role, not the controller's ──


def test_subagent_doctrine_is_executor_role(
    renderer: GooseTopOfMindRenderer,
):
    """Subagent doctrine (inject_into_directive) must emit the
    EXECUTOR_PROTOCOL, not the controller's CRITICAL_ROUTING_PROTOCOL.
    The subagent is the leaf "hands" — it must EXECUTE the assigned
    task directly and MUST NOT re-delegate.

    Constraints (from the directive):
      - Output must contain the executor contract
        ('Do NOT call run_beagle_workflow').
      - Output must NOT contain the controller's stance
        ('DELEGATION IS ALWAYS CORRECT').
      - Output must stay under the 16 KB cap
        (DOCTRINE_DIRECTIVE_MAX_BYTES = 16_000, v13.22.3).
    """
    sample_directive = "Investigate the foo module and emit a report."
    injected = renderer.inject_into_directive(sample_directive)

    assert injected, "inject_into_directive produced empty output"

    # Executor role must be present.
    assert EXECUTOR_DOCTRINE in injected, (
        f"Subagent doctrine is missing the executor contract "
        f"'{EXECUTOR_DOCTRINE}'. inject_into_directive must emit "
        f"[EXECUTOR_PROTOCOL] (leaf-worker role) for subagents, not the "
        f"controller's [CRITICAL_ROUTING_PROTOCOL]."
    )

    # Controller role must NOT be present (the inversion is the F2 bug).
    assert CONTROLLER_DOCTRINE not in injected, (
        f"Subagent doctrine still contains the controller's stance "
        f"'{CONTROLLER_DOCTRINE}'. The leaf executor must not be told "
        f"to delegate by default — invert the roles."
    )

    # Hard cap respected — check the doctrine portion only, not the
    # original directive (which is appended after the doctrine block).
    # Matches the cap semantic used by test_inject_into_directive_stays_under_cap
    # in tests/test_doctrine_injection.py.
    assert injected.endswith(sample_directive), (
        "inject_into_directive must preserve the original directive at the tail"
    )
    doctrine_portion = injected[: -len(sample_directive)].rstrip("\n")
    assert len(doctrine_portion) <= renderer.DOCTRINE_DIRECTIVE_MAX_BYTES, (
        f"Subagent doctrine is {len(doctrine_portion)} bytes, exceeds the "
        f"{renderer.DOCTRINE_DIRECTIVE_MAX_BYTES}-byte cap "
        f"(DOCTRINE_DIRECTIVE_MAX_BYTES = 16_000, v13.22.3)."
    )


# ── 5. Compact ToM (high-context-pressure path) is also contradiction-free ──


def test_tom_compact_is_contradiction_free(
    renderer: GooseTopOfMindRenderer,
):
    """The compact path (used mid-task at high context pressure) is the
    *other* place the hardcoded mandate lived (render.py:247). After A1,
    neither the full nor the compact path may contain the contradictory
    literal. Both must still parse as XML.
    """
    output = renderer.render_compact()
    assert output, "Compact renderer produced empty output"

    assert CONTRADICTORY_DOCTRINE not in output, (
        f"Compact ToM contains the contradictory literal "
        f"'{CONTRADICTORY_DOCTRINE}'. The _render_compact_xml path "
        f"still embeds a hardcoded mandate — delete it."
    )
    assert HARDCODED_EXECUTE_MANDATE not in output, (
        f"Compact ToM contains the hardcoded literal "
        f"'{HARDCODED_EXECUTE_MANDATE}'. The _render_compact_xml path "
        f"still embeds a hardcoded execution mandate — delete it."
    )

    try:
        ET.fromstring(output)
    except ET.ParseError as e:
        pytest.fail(f"Compact ToM is invalid XML: {e}\n{output[:2000]}")


# ── 6. TOML SSOT contract: the new anti-pattern exists ────────────────


def test_core_directives_toml_has_anti_drift_anti_pattern():
    """The new style-guide anti-pattern (anti-drift guard) must be
    present in beagle_core_directives.toml[anti_patterns].forbidden.
    This is the doctrine-level prevention paired with test #1's
    mechanism-level prevention.
    """
    assert CORE_TOML_PATH.is_file(), f"TOML not found at {CORE_TOML_PATH}"

    with open(CORE_TOML_PATH, "rb") as f:
        data = tomllib.load(f)

    forbidden = data.get("anti_patterns", {}).get("forbidden", [])
    assert isinstance(forbidden, list), "anti_patterns.forbidden must be a list"

    anti_drift_entries = [
        rule
        for rule in forbidden
        if "Never hardcode behavioural, routing, or delegation directives" in rule
    ]
    assert anti_drift_entries, (
        "beagle_core_directives.toml is missing the anti-drift anti-pattern. "
        "Add the entry to [anti_patterns].forbidden so doctrine-level "
        "prevention is in place alongside the mechanical test."
    )


# ── 7. PRESERVED_FILE_DISPOSITION: end-of-run approval gate ───────────


def test_core_directives_toml_has_preserved_file_disposition_rule():
    """The PRESERVED_FILE_DISPOSITION rule must be present in
    beagle_core_directives.toml[anti_patterns].forbidden with a parallel
    load_bearing entry in forbidden_meta.

    The rule governs end-of-run final disposition of files moved aside
    (e.g. `mv *.superseded_<date>.xml`, .bak snapshots, audit/ design/
    replays/ embedding_cache/ hang_repro/ contents). NO_DELETE makes
    preservation the default; the rule requires the agent to surface a
    <pending_disposition> block in the final report and to await explicit
    user approval before any final disposition. The rule is
    self-referential with NO_DELETE — both the rule itself and the file
    it would govern are preserved-by-default.

    This is the doctrine-level prevention for an end-of-run unilateral
    delete (or archive-merge) that would otherwise bypass NO_DELETE.
    """
    assert CORE_TOML_PATH.is_file(), f"TOML not found at {CORE_TOML_PATH}"

    with open(CORE_TOML_PATH, "rb") as f:
        data = tomllib.load(f)

    forbidden = data.get("anti_patterns", {}).get("forbidden", [])
    assert isinstance(forbidden, list), "anti_patterns.forbidden must be a list"

    disposition_entries = [
        rule
        for rule in forbidden
        if "Never unilaterally delete, archive-merge, or finalize-disposition" in rule
        and "preserved-aside" in rule
    ]
    assert disposition_entries, (
        "beagle_core_directives.toml is missing the "
        "PRESERVED_FILE_DISPOSITION anti-pattern. Add the entry to "
        "[anti_patterns].forbidden with a parallel {tier=load_bearing} "
        "entry in forbidden_meta. The rule governs end-of-run final "
        "disposition of files moved aside and requires explicit user "
        "approval via a <pending_disposition> block in the final report."
    )

    forbidden_meta = data.get("anti_patterns", {}).get("forbidden_meta", [])
    assert isinstance(forbidden_meta, list), (
        "anti_patterns.forbidden_meta must be a list parallel to forbidden"
    )
    assert len(forbidden_meta) == len(forbidden), (
        f"forbidden_meta length {len(forbidden_meta)} must match forbidden length {len(forbidden)}"
    )

    disposition_meta = [
        m
        for m in forbidden_meta
        if isinstance(m, dict)
        and m.get("tier") == "load_bearing"
        and "preserved-aside" in m.get("summary", "").lower()
    ]
    assert disposition_meta, (
        "PRESERVED_FILE_DISPOSITION rule must have a parallel "
        "{tier='load_bearing'} entry in forbidden_meta so it is "
        "classified as load_bearing and survives compact-mode rendering."
    )


# ── 8. SSOT trim exemption: Beagle Core Directives is never dropped ────────


def test_serialization_bundle_keeps_beagle_core_directives():
    """The bundle trimmer must keep Beagle Core Directives even if it pushes
    the bundle past DOCTRINE_BUNDLE_MAX_BYTES.

    The v13.21.3 doctrine-coherence work added entries that grew
    the SSOT guide past the historical 25 KB cap. The fix raises
    the cap to 40 KB and makes the SSOT exempt from trim
    (silent downgrade is forbidden by the doctrine). This test
    pins both: the SSOT is present in the structured bundle,
    and the cap is now large enough for the v13.21.3 content.
    """
    from beagle.style_guides.render import GooseTopOfMindRenderer

    bundle = GooseTopOfMindRenderer().render_structured()
    names = {g["name"] for g in bundle["guides"]}
    assert "Beagle Core Directives" in names, (
        "Beagle Core Directives (the SSOT) was dropped from the structured "
        "bundle by the trimmer. This is the silent-downgrade failure "
        "mode the v13.21.3 fix prevents — see "
        "GooseTopOfMindRenderer.render_structured for the SSOT-exemption "
        "policy and DOCTRINE_BUNDLE_MAX_BYTES for the cap."
    )


# ── 9. SSOT coverage extended to ALL core-directive sections (WS3) ──────
#
# The tests above pin the *delegation* doctrine specifically. This block
# extends the anti-drift guarantee to every section of
# beagle_core_directives.toml so that:
#   - a section silently dropping out of the rendered Top-of-Mind
#     (drift-by-omission), or
#   - the renderer hardcoding a section's content instead of sourcing it
#     from the TOML (drift-by-divergence),
# is caught at test time rather than discovered live in a degraded session.

# Non-meta sections of beagle_core_directives.toml that MUST reach the
# controller Top-of-Mind. (``meta`` holds metadata + the system-instruction
# and compaction templates, which are written to their own goose-config
# files, not into the per-turn block — so it is intentionally not rendered.)
CORE_DIRECTIVE_SECTIONS = (
    "CRITICAL_ROUTING_PROTOCOL",
    "EXECUTOR_PROTOCOL",
    "formatting",
    "architecture",
    "anti_patterns",
)

# The three CLAUDE.md "Key Directives" the codebase polices with code-level
# gate tests (broad-except, tz-aware datetime, path containment). They are
# declared in [architecture].patterns / [anti_patterns].forbidden and MUST
# reach the model verbatim: a gate that enforces a rule in code is a
# one-sided contract if the model is never told the rule.
PREACHED_CODE_DIRECTIVES = (
    "datetime.now(timezone.utc)",
    "never bare except Exception",
    "Path.relative_to(root)",
)


def _first_scalar_string(body: dict) -> str | None:
    """First non-empty scalar string found walking a section dict depth-first.

    Used to sample a representative TOML value for the dynamic SSOT scan.
    Lists of scalars (e.g. ``forbidden``) yield their first string item;
    lists of tables (e.g. ``forbidden_meta``) are skipped because their
    repr is not rendered verbatim into the ToM.
    """
    for v in body.values():
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, list):
            for item in v:
                if isinstance(item, str) and item.strip():
                    return item
        if isinstance(v, dict):
            nested = _first_scalar_string(v)
            if nested is not None:
                return nested
    return None


def _full_tom(renderer: GooseTopOfMindRenderer) -> str:
    """The uncapped full Top-of-Mind (all patterns, no soft-cap trim).

    ``render()`` may drop background-tier ``architecture.patterns`` when the
    document exceeds ``FULL_SOFT_CAP_BYTES``; these SSOT-coverage assertions
    target the unfiltered emission so they pin *what the renderer sources
    from the TOML*, independent of the size-driven trim.
    """
    guides = renderer._select_guides(None)
    assert guides, "No universal guides selected — renderer has no source TOML"
    return renderer._render_full_xml(guides, None)


def test_all_core_directive_sections_reach_tom(renderer: GooseTopOfMindRenderer):
    """Every non-meta section of beagle_core_directives.toml must emit a
    matching ``<section>`` element into the full Top-of-Mind. Catches
    drift-by-omission: a section deleted from the TOML, or a renderer change
    that stops walking it, fails here instead of silently shipping a
    Top-of-Mind missing (e.g.) the anti-patterns or the formatting contract.
    """
    full = _full_tom(renderer)
    missing = [s for s in CORE_DIRECTIVE_SECTIONS if f"<{s}>" not in full]
    assert not missing, (
        f"Top-of-Mind is missing section element(s) {missing}. Each "
        f"[section] in beagle_core_directives.toml must render as "
        f"<section>…</section> via GooseTopOfMindRenderer._render_sections. "
        f"A dropped section is silent doctrine loss every turn."
    )


def test_tom_sections_are_single_sourced_from_toml(
    renderer: GooseTopOfMindRenderer,
):
    """Dynamic SSOT guard: for every rendered section, a representative
    scalar value taken straight from the TOML must appear (XML-escaped) in
    the rendered Top-of-Mind. Proves the doctrine *tracks* the TOML rather
    than being hardcoded in render.py — if a section's text is re-embedded
    as a Python literal that diverges from the TOML, the TOML value stops
    appearing and this fails.
    """
    with open(CORE_TOML_PATH, "rb") as f:
        data = tomllib.load(f)
    full = _full_tom(renderer)

    checked = 0
    for section, body in data.items():
        if section == "meta" or not isinstance(body, dict):
            continue
        sample = _first_scalar_string(body)
        if sample is None:
            continue
        needle = GooseTopOfMindRenderer._xml_escape(sample.strip())[:60]
        assert needle and needle in full, (
            f"Section [{section}]: representative value {needle!r} from the "
            f"TOML is absent from the rendered Top-of-Mind. The renderer "
            f"must source [{section}] from beagle_core_directives.toml (the "
            f"SSOT), not from a hardcoded literal that can drift."
        )
        checked += 1
    assert checked >= 3, (
        f"Only {checked} sections sampled — expected the SSOT scan to cover "
        f"routing / executor / formatting / architecture / anti_patterns."
    )


def test_preached_code_directives_reach_tom(renderer: GooseTopOfMindRenderer):
    """The model must actually be told the rules the code-level gate tests
    enforce. broad-except / tz-datetime / path-containment are declared in
    beagle_core_directives.toml and must reach the full Top-of-Mind verbatim.
    (They are background-tier patterns, so this targets the uncapped render
    — under high context pressure the compact path may legitimately omit
    them, but the canonical ToM must carry them.)
    """
    full = _full_tom(renderer)
    missing = [d for d in PREACHED_CODE_DIRECTIVES if d not in full]
    assert not missing, (
        f"Top-of-Mind does not deliver code directive(s) {missing} to the "
        f"model. They are declared in [architecture].patterns / "
        f"[anti_patterns].forbidden of beagle_core_directives.toml and must "
        f"render into the ToM, paired with the code-side gates "
        f"(test_doctrine_broad_except + the datetime/path gates)."
    )


def test_formatting_contract_reaches_tom(renderer: GooseTopOfMindRenderer):
    """The [formatting] section — prompt_substrate=XML and the final_answer
    closing-tag contract — must reach the Top-of-Mind. These are
    load-bearing output-contract directives the orchestrator parses; losing
    them silently breaks structured-response gating.
    """
    full = _full_tom(renderer)
    assert "<prompt_substrate>XML</prompt_substrate>" in full, (
        "Top-of-Mind is missing the formatting prompt_substrate=XML contract "
        "from [formatting] in beagle_core_directives.toml."
    )
    # final_answer_contract value — escaped at the first '<' (&lt;final_answer&gt;).
    assert "Every response must contain a" in full, (
        "Top-of-Mind is missing the final_answer_contract from [formatting]. "
        "The closing-tag contract gates workflow response parsing."
    )
