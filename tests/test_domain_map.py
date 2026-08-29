"""``domain_map.toml`` — cwd → style-guide-domain resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from beagle.style_guides.render import (
    GooseTopOfMindRenderer,
    load_domain_map,
    resolve_domain,
)


def test_domain_map_loads() -> None:
    dmap = load_domain_map()
    assert isinstance(dmap, dict)
    # the shipped map defines these
    assert {"python", "devops"} <= set(dmap)
    assert "04_lang_python" in dmap["python"]["guides"]


def test_resolve_domain_matches_parent_dirs() -> None:
    # a path deep under a python-domain repo still resolves
    assert resolve_domain("/home/server/Projects/beagle/src/beagle/foo") == "python"
    assert resolve_domain("/home/server/Projects/MiniNAS_config/hardware") == "devops"


def test_resolve_domain_unknown_is_none() -> None:
    assert resolve_domain("/tmp/nowhere") is None
    assert resolve_domain(None) is None


def test_select_guides_uses_domain_map() -> None:
    r = GooseTopOfMindRenderer()
    guides = r._select_guides("python")
    names = [(g.get("meta") or {}).get("name") for g in guides]
    assert "Beagle Core Directives" in names
    assert any("Python" in str(n) for n in names)


def test_unknown_domain_falls_back_to_universal() -> None:
    r = GooseTopOfMindRenderer()
    unknown = r._select_guides("no_such_domain")
    universal = r._select_guides(None)
    assert {(g.get("meta") or {}).get("name") for g in unknown} == {
        (g.get("meta") or {}).get("name") for g in universal
    }


def test_malformed_map_is_not_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bad = tmp_path / "domain_map.toml"
    bad.write_text("this is not = valid toml [[[", encoding="utf-8")
    monkeypatch.setattr("beagle.style_guides.render._domain_map_path", lambda: bad)
    assert load_domain_map() == {}
    assert resolve_domain("/home/server/Projects/beagle") is None
