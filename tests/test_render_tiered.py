"""Tiered Top-of-Mind render — the layered per-turn artefact.

The canonical ``beagle render-hints`` output is the *tiered* render: only the
load-bearing tier inline, the rest reachable via ``get_style_guide``. These
tests pin:

  1. the tiered render is well-formed XML and materially smaller than the full;
  2. it still carries the coercive header (license_to_deviate + identity);
  3. every ``tier = "load_bearing"`` rule from run_to_completion appears;
  4. the ``_tiered`` protocol form is used (not the full prose);
  5. an ``<expand>`` block names ``get_style_guide``.
"""

from __future__ import annotations

import tomllib
import xml.etree.ElementTree as ET

import pytest

from beagle.config._config_path import find_guides_dir
from beagle.style_guides.render import GooseTopOfMindRenderer


@pytest.fixture(scope="module")
def renderer() -> GooseTopOfMindRenderer:
    return GooseTopOfMindRenderer()


@pytest.fixture(scope="module")
def tiered_xml(renderer: GooseTopOfMindRenderer) -> str:
    guides = renderer._select_guides(None)
    return renderer._render_tiered_xml(guides, None)


def test_tiered_is_well_formed(tiered_xml: str) -> None:
    # strip the leading XML comment so ET has a single root
    body = tiered_xml.split("-->", 1)[1] if "-->" in tiered_xml else tiered_xml
    root = ET.fromstring(body)
    assert root.tag == "beagle_top_of_mind"


def test_tiered_is_smaller_than_full(renderer: GooseTopOfMindRenderer, tiered_xml: str) -> None:
    guides = renderer._select_guides(None)
    full = renderer._render_full_xml(guides, None)
    assert len(tiered_xml.encode()) < len(full.encode()) * 0.5, (
        f"tiered {len(tiered_xml.encode())}B is not materially smaller than "
        f"full {len(full.encode())}B"
    )


def test_tiered_keeps_coercive_header(tiered_xml: str) -> None:
    assert "<license_to_deviate" in tiered_xml
    assert "<beagle_system_identity>" in tiered_xml
    assert "NEVER STOP" in tiered_xml


def test_tiered_carries_every_load_bearing_rtc_rule(tiered_xml: str) -> None:
    with (find_guides_dir() / "run_to_completion.toml").open("rb") as fh:
        rtc = tomllib.load(fh)
    load_bearing = [
        m["id"] for m in rtc["meta"]["rules_meta"] if m.get("tier") == "load_bearing"
    ]
    assert load_bearing, "run_to_completion has no load_bearing rules_meta"
    for rid in load_bearing:
        assert f'id="{rid}"' in tiered_xml, f"load-bearing rule {rid} missing from tiered ToM"


def test_tiered_uses_terse_protocol_form(tiered_xml: str) -> None:
    # the _tiered form of CRITICAL_ROUTING_PROTOCOL, not the full prose
    assert 'CRITICAL_ROUTING_PROTOCOL tier="load_bearing"' in tiered_xml
    assert "goose = CONTROLLER" in tiered_xml
    # the verbose full-prose sentence must NOT be inline
    assert "Minimising direct execution minimises expensive-model calls" not in tiered_xml


def test_tiered_names_the_expansion_tool(tiered_xml: str) -> None:
    assert "<expand>" in tiered_xml
    assert "get_style_guide" in tiered_xml
