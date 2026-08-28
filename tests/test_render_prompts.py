"""Tests for the v13.21.3 prompt-substrate renderers.

Covers:
- render_system_instruction() writes XML to canonical path
- render_compaction_prompt() writes XML to canonical path
- _load_template() raises if the TOML key is missing
- render_all() includes the new artefacts in its result dict
- The output files are well-formed XML
- The output files do NOT contain markdown-only content (no top-level `# `)

Per directive: prompt-substrate files are XML or YAML ONLY — no MD, ever.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


@pytest.fixture
def tmp_output(tmp_path: Path) -> Path:
    """Per-test output directory under tmp_path so we don't clobber
    the real ~/.config/goose files."""
    return tmp_path


def test_render_system_instruction_writes_xml(tmp_output: Path) -> None:
    from beagle.style_guides.render import (
        GooseTopOfMindRenderer,
    )

    target = tmp_output / "system_instruction.xml"
    renderer = GooseTopOfMindRenderer()
    written = renderer.render_system_instruction(output_path=target)

    assert written == target
    assert target.is_file()
    assert target.stat().st_size > 500, "system instruction should be non-trivial"

    # Must be well-formed XML
    tree = ET.parse(target)
    root = tree.getroot()
    # The template wraps in <beagle_top_of_mind> envelope (per
    # system_instruction_template) or is the bare template — both are
    # well-formed XML.
    assert root.tag in {"beagle_top_of_mind", "system_instruction"}


def test_render_compaction_prompt_writes_xml(tmp_output: Path) -> None:
    from beagle.style_guides.render import (
        GooseTopOfMindRenderer,
    )

    target = tmp_output / "compaction.xml"
    renderer = GooseTopOfMindRenderer()
    written = renderer.render_compaction_prompt(output_path=target)

    assert written == target
    assert target.is_file()
    assert target.stat().st_size > 500

    # Must be well-formed XML with the expected root
    tree = ET.parse(target)
    root = tree.getroot()
    assert root.tag == "compaction_prompt"
    # version attribute
    assert root.attrib.get("version") == "2"


def test_compaction_prompt_contains_required_sentinels(tmp_output: Path) -> None:
    """The watchdog greps for beagle_session_bootstrap + resume_marker."""
    from beagle.style_guides.render import (
        GooseTopOfMindRenderer,
    )

    target = tmp_output / "compaction.xml"
    GooseTopOfMindRenderer().render_compaction_prompt(output_path=target)

    text = target.read_text(encoding="utf-8")
    assert "beagle_session_bootstrap" in text
    assert "resume_marker" in text
    assert "post_compaction_hydration" in text


def test_system_instruction_round_trips_toml_core_routing_rule(
    tmp_output: Path,
) -> None:
    """v13.22.4 (P3-3): the rendered ``beagle_system_instruction.xml`` must
    round-trip against the canonical TOML SSOT. A future SSOT edit that
    changes ``<core_routing_rule>`` must produce a different rendered
    file, and the rendered file must contain text that is mechanically
    derived from the TOML (not hardcoded).

    Concretely:
    1. Parse ``beagle_core_directives.toml`` and extract the literal
       ``system_instruction_template`` string.
    2. Render the system instruction.
    3. Assert the rendered output is byte-equal (or, when placeholder
       substitution is involved, byte-prefix-equal) to the TOML
       template. This prevents the renderer from adding or dropping
       any doctrinally-relevant XML that is not in the SSOT.
    """
    import tomllib

    # v1.1.1 (S6): guides moved to /home/Beagle_Config/style_guides/guides.
    from beagle.config._config_path import find_guides_dir
    from beagle.style_guides.render import GooseTopOfMindRenderer

    # Locate the SSOT TOML (the canonical source).
    toml_path = find_guides_dir() / "beagle_core_directives.toml"
    assert toml_path.is_file(), f"SSOT TOML missing: {toml_path}"

    data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    # The templates appear before any [table] header in the source, so
    # tomllib attaches them to the first table encountered ([meta]).
    # If a future refactor moves them to their own table, this test
    # still finds them by searching top-level + meta.
    _containers: list[dict[str, object]] = [data] + [
        v for v in data.values() if isinstance(v, dict)
    ]
    ssot_template = ""
    for container in _containers:
        if "system_instruction_template" in container:
            ssot_template = str(container["system_instruction_template"])
            break
    assert ssot_template, (
        f"system_instruction_template not found in TOML SSOT {toml_path}. "
        f"Top-level keys: {list(data.keys())}"
    )

    # The SSOT template is a raw multi-line string. The renderer writes
    # it verbatim to the canonical path (per the render.py docstring).
    # Therefore the rendered file MUST equal the SSOT template exactly
    # (no additions, no omissions, no reformatting).
    target = tmp_output / "system_instruction_round_trip.xml"
    GooseTopOfMindRenderer().render_system_instruction(output_path=target)

    rendered = target.read_text(encoding="utf-8")
    assert rendered == ssot_template, (
        f"Rendered system instruction has drifted from the TOML SSOT. "
        f"This indicates the renderer is doing more than writing the "
        f"TOML template verbatim. Diff:\n"
        f"--- SSOT (TOML) ---\n{ssot_template[:500]}\n"
        f"--- Rendered ---\n{rendered[:500]}"
    )

    # Also assert the SSOT template carries the doctrinally-relevant
    # block we care about (regression guard for accidental removal).
    assert "<core_routing_rule>" in ssot_template
    assert "route_query_to_workflow" in ssot_template
    assert "run_beagle_workflow" in ssot_template
    assert "beagle_session_bootstrap" in ssot_template


def test_system_instruction_contains_session_continuity(tmp_output: Path) -> None:
    from beagle.style_guides.render import (
        GooseTopOfMindRenderer,
    )

    target = tmp_output / "si.xml"
    GooseTopOfMindRenderer().render_system_instruction(output_path=target)

    text = target.read_text(encoding="utf-8")
    assert "beagle_session_bootstrap" in text
    # The template has explicit on_session_start / on_meaningful_change /
    # on_compaction directives for the watchdog to parse.
    assert "on_session_start" in text
    assert "on_compaction" in text


def test_load_template_raises_for_missing_key(tmp_output: Path) -> None:
    """If a future template key is requested but absent from the TOML,
    the renderer MUST raise KeyError — never silently emit empty content."""
    from beagle.style_guides.render import (
        GooseTopOfMindRenderer,
    )

    renderer = GooseTopOfMindRenderer()
    with pytest.raises(KeyError, match="not_a_real_key"):
        renderer._load_template("not_a_real_key")


def test_render_all_includes_new_artefacts(tmp_output: Path, monkeypatch) -> None:
    """render_all() must include the new system_instruction and
    compaction_prompt entries. We monkeypatch the canonical paths to
    tmp_output so we don't clobber the real config."""
    from beagle.style_guides import render as render_mod
    from beagle.style_guides.render import (
        GooseTopOfMindRenderer as _Renderer,
    )

    monkeypatch.setattr(
        _Renderer,
        "SYSTEM_INSTRUCTION_PATH",
        tmp_output / "si.xml",
    )
    monkeypatch.setattr(
        _Renderer,
        "COMPACTION_PROMPT_PATH",
        tmp_output / "cp.xml",
    )

    renderer = render_mod.GooseTopOfMindRenderer()
    results = renderer.render_all()

    assert "system_instruction" in results
    assert "compaction_prompt" in results
    assert results["system_instruction"].is_file()
    assert results["compaction_prompt"].is_file()


def test_no_markdown_top_level_headers_in_substrate(tmp_output: Path) -> None:
    """Per directive: prompt-substrate is XML or YAML only — no MD ever.
    Specifically, no '# ' or '## ' top-level markdown headers."""
    from beagle.style_guides.render import (
        GooseTopOfMindRenderer,
    )

    si = tmp_output / "si.xml"
    cp = tmp_output / "cp.xml"
    GooseTopOfMindRenderer().render_system_instruction(output_path=si)
    GooseTopOfMindRenderer().render_compaction_prompt(output_path=cp)

    for f in (si, cp):
        text = f.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.lstrip()
            # Allow XML comments and XML tags; reject '# ' at line start
            assert not stripped.startswith("# "), f"{f}: contains markdown header at line: {line!r}"
            assert not stripped.startswith("## "), (
                f"{f}: contains markdown header at line: {line!r}"
            )


def test_module_level_convenience_functions_exist() -> None:
    """The module-level render_system_instruction / render_compaction_prompt
    functions must exist (used by CLI subcommand + Makefile target)."""
    from beagle.style_guides import render as render_mod

    assert callable(getattr(render_mod, "render_system_instruction", None))
    assert callable(getattr(render_mod, "render_compaction_prompt", None))
    assert callable(getattr(render_mod, "render_all", None))


def test_render_all_target_root_writes_per_repo_pointers(tmp_output: Path, monkeypatch) -> None:
    """v13.22.2: ``render_all(repo_root=...)`` (or the ``target_root`` ctor
    arg) must redirect the per-repo artefacts (.goosehints,
    .goose/standards.md, root CLAUDE.md) to the target directory. The
    home-canonical artefacts (Top-of-Mind, system instruction, compaction
    prompt) are unaffected and still go to the home directory.

    This is the contract that powers ``beagle render-prompts --target <dir>``
    so sibling repos (server_1_skylon, server_1_orpheus) can carry the v13.22.1
    XML-pointer style on their own pointer files.
    """
    from beagle.style_guides import render as render_mod
    from beagle.style_guides.render import (
        GooseTopOfMindRenderer as _Renderer,
    )

    # Redirect the canonical home artefacts into tmp_output so the test
    # doesn't clobber the real ~/.config/goose files.
    monkeypatch.setattr(
        _Renderer,
        "SYSTEM_INSTRUCTION_PATH",
        tmp_output / "si.xml",
    )
    monkeypatch.setattr(
        _Renderer,
        "COMPACTION_PROMPT_PATH",
        tmp_output / "cp.xml",
    )

    target = tmp_output / "external_repo"
    target.mkdir()

    renderer = render_mod.GooseTopOfMindRenderer(target_root=target)
    results = renderer.render_all()

    # Per-repo artefacts landed under the target.
    assert results[".goosehints"] == target / ".goosehints"
    assert results[".goosehints"].is_file()
    assert results["standards_md"] == target / ".goose" / "standards.md"
    assert results["standards_md"].is_file()
    assert results["root_claude_md"] == target / "CLAUDE.md"
    assert results["root_claude_md"].is_file()

    # No beagle subpackage for an external target — the
    # package CLAUDE.md entry is the empty Path placeholder.
    assert results["pkg_claude_md"] == Path()

    # Home-canonical artefacts still go to the home paths (redirected to
    # tmp_output above), NOT to the target.
    assert results["hints"] != target / "beagle_top_of_mind.xml"
    assert results["system_instruction"] == tmp_output / "si.xml"
    assert results["compaction_prompt"] == tmp_output / "cp.xml"

    # The .goosehints content is the same template used by the
    # beagle package — no doctrine drift between
    # canonical and target.
    target_hints_text = (target / ".goosehints").read_text(encoding="utf-8")
    assert "<goose_beagle_pointer" in target_hints_text
    assert "beagleutilityserver__beagle_session_bootstrap" in target_hints_text

    # standards.md is the thin XML pointer (no doctrine copy).
    standards_text = (target / ".goose" / "standards.md").read_text(encoding="utf-8")
    assert '<beagle_pointer kind="standards"' in standards_text
    # The pointer must reference the TOML SSOT paths.
    # The package lives at repo-root src/, so the emitted pointer names
    # src/style_guides/guides/ — it said beagle/ (the pre-rename directory),
    # which sent every reader of the generated pointer to a path that no
    # longer exists.
    assert "src/beagle/style_guides/guides/" in standards_text


def test_render_all_default_target_is_package_repo_root() -> None:
    """When no target_root / repo_root is supplied, render_all() must
    continue to write per-repo artefacts under the beagle
    package root (i.e. the existing default behaviour is preserved).

    v13.22.2 also pins that the package CLAUDE.md is emitted (not the
    empty placeholder) in the no-target case — only --target suppresses it.
    """
    from beagle.style_guides import render as render_mod

    renderer = render_mod.GooseTopOfMindRenderer()
    # The property is the package repo root, not the user's home.
    pkg_root = render_mod.GooseTopOfMindRenderer._repo_root()
    assert renderer.target_root == pkg_root

    # The pyproject/.git marker only exists when running from a source
    # checkout. Under a wheel install _repo_root() resolves to
    # site-packages/beagle, which is a package directory and not a repo —
    # asserting a repo marker there fails for a correct install rather than
    # catching a real defect.
    is_source_checkout = (pkg_root / "pyproject.toml").is_file() or (pkg_root / ".git").is_dir()

    # Without --target, the package CLAUDE.md MUST be emitted (this was
    # a regression in an earlier draft of the v13.22.2 refactor where
    # pkg_claude_md was incorrectly short-circuited to Path()).
    results = renderer.render_all()
    assert results["pkg_claude_md"].is_file(), (
        f"pkg_claude_md must be a real file in the no-target case; got {results['pkg_claude_md']!r}"
    )
    # The package CLAUDE.md lives at <pkg_root>/src/CLAUDE.md — the package
    # is at repo-root src/ (pyproject.toml package-dir mapping). This asserted
    # <pkg_root>/beagle/CLAUDE.md, the pre-rename directory, which is what
    # kept the dead beagle/ tree alive: every render recreated the file there.
    assert results["pkg_claude_md"] == pkg_root / "src" / "beagle" / "CLAUDE.md"
    if is_source_checkout:
        # In a checkout the emitted pointer sits beside the package source.
        assert results["pkg_claude_md"].parent.name == "beagle"


def test_render_never_creates_nested_src_dir() -> None:
    """Regression: the package CLAUDE.md must never be written under a nested
    src/beagle/src/beagle/ path (the pre-v1.0.0 wrong target). render_all()
    emits the package pointer to <pkg_root>/src/beagle/CLAUDE.md, so running
    it must not leave any path segment 'src/beagle/src' behind.
    """
    from beagle.style_guides import render as render_mod

    pkg_root = render_mod.GooseTopOfMindRenderer._repo_root()
    # The wrong nested dir that used to be recreated by every render.
    stale_nested = pkg_root / "src" / "beagle" / "src" / "beagle"

    # Render fresh (per-repo artefacts go to a temp target so we don't
    # disturb the real repo, but the pkg CLAUDE.md is only emitted at the
    # real package root — run it directly via the pointer emitter).
    from beagle.style_guides.render import GooseTopOfMindRenderer

    renderer = GooseTopOfMindRenderer()
    written = renderer.render_claude_md(variant="package")

    assert written == pkg_root / "src" / "beagle" / "CLAUDE.md"
    # The nested dir must not exist and must not be recreated by rendering.
    assert not stale_nested.exists()
    assert not (pkg_root / "src" / "beagle" / "src").exists()
