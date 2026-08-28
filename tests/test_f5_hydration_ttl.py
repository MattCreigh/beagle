"""F5 regression — hydration TTL decoupled from TOML mtime.

The v13.21 audit flagged that ``render_canonical`` short-circuited on
``_newest_source_mtime() <= dest.stat().st_mtime``. That meant a
hydrated RAG/chat block was cached until a TOML was edited, which
contradicts the E3 "live at call-time" goal — RAG indexes and chat
history change without writing to any TOML.

The fix has three parts:

1. The opening ``<hydrated>`` tag in :mod:`tom_hydrator` now carries a
   ``hydrated_at=`` ISO 8601 attribute stamp.
2. ``GooseTopOfMindRenderer.render_canonical`` accepts a
   ``max_age_seconds=`` parameter and, when the TOML-mtime check
   short-circuits, additionally checks the hydrated block's age. If
   the block is older than the TTL, the render is forced.
3. The new ``_hydration_is_stale`` static method parses the
   ``hydrated_at`` attribute and is conservative (unparseable
   timestamps, missing files, etc. all force a re-render).

This file pins all three: the attribute is written, the staleness
check ages the block, and ``force=True`` forces re-render regardless
of mtime.
"""

from __future__ import annotations

import datetime as _dt
import json
import re

# ── 1. The opening <hydrated> tag carries hydrated_at= ───────────────────


def test_hydrated_block_carries_hydrated_at_attribute(monkeypatch):
    """The opening tag includes a hydrated_at ISO 8601 stamp (F5)."""
    import beagle.infrastructure.mcp_rag_server as _rag_mod
    import beagle.style_guides._chatrecall_adapter as _chat_mod
    from beagle.style_guides import tom_hydrator
    from beagle.style_guides.render import GooseTopOfMindRenderer

    async def fake_async_rag(_query, _max_hops=1, _top_k=3):
        return json.dumps(
            {
                "semantic_anchors": [{"file_path": "x.py", "score": 0.9}],
                "structural_relations": [],
            }
        )

    monkeypatch.setattr(_rag_mod, "rag_search", fake_async_rag)
    monkeypatch.setattr(
        _chat_mod, "chatrecall", lambda _query, _limit=10: [{"role": "user", "content": "x"}]
    )

    renderer = GooseTopOfMindRenderer()
    xml, queries = renderer.render_with_placeholders()
    hydrated = tom_hydrator.hydrate(xml, queries)

    m = re.search(r"<hydrated\b[^>]*>", hydrated)
    assert m is not None, "no <hydrated> opening tag in output"
    opening = m.group(0)
    stamp_m = re.search(r'hydrated_at="([^"]+)"', opening)
    assert stamp_m is not None, f"no hydrated_at= on opening tag: {opening!r}"
    stamp = stamp_m.group(1)
    parsed = _dt.datetime.fromisoformat(stamp)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.UTC)
    now = _dt.datetime.now(_dt.UTC)
    age = abs((now - parsed).total_seconds())
    assert age < 60, f"hydrated_at stamp is {age:.1f}s old, expected <60s"


def test_hydrated_block_source_attribute_present(monkeypatch):
    """The opening tag also names the hydration sources (rag+chat)."""
    import beagle.infrastructure.mcp_rag_server as _rag_mod
    import beagle.style_guides._chatrecall_adapter as _chat_mod
    from beagle.style_guides import tom_hydrator
    from beagle.style_guides.render import GooseTopOfMindRenderer

    async def fake_async_rag(_query, _max_hops=1, _top_k=3):
        return json.dumps({"semantic_anchors": [], "structural_relations": []})

    monkeypatch.setattr(_rag_mod, "rag_search", fake_async_rag)
    monkeypatch.setattr(_chat_mod, "chatrecall", lambda _query, _limit=10: [])

    renderer = GooseTopOfMindRenderer()
    xml, queries = renderer.render_with_placeholders()
    hydrated = tom_hydrator.hydrate(xml, queries)
    m = re.search(r"<hydrated\b[^>]*>", hydrated)
    assert m is not None
    assert 'source="rag+chat"' in m.group(0)


# ── 2. _hydration_is_stale ages the block ────────────────────────────────


def test_hydration_is_stale_no_block_returns_false(tmp_path):
    """An artefact with no <hydrated> block is not hydration-stale."""
    from beagle.style_guides.render import GooseTopOfMindRenderer

    f = tmp_path / "tom.xml"
    f.write_text("<beagle_top_of_mind><license_to_deviate/></beagle_top_of_mind>")
    # Even with TTL=0, no <hydrated> block means not stale.
    assert GooseTopOfMindRenderer._hydration_is_stale(f, ttl_seconds=0) is False


def test_hydration_is_stale_missing_file_returns_true(tmp_path):
    """A missing artefact is stale (defensive: never let absence mask bug)."""
    from beagle.style_guides.render import GooseTopOfMindRenderer

    f = tmp_path / "does_not_exist.xml"
    assert GooseTopOfMindRenderer._hydration_is_stale(f, ttl_seconds=3600) is True


def test_hydration_is_stale_fresh_block_is_not_stale(tmp_path):
    """A hydrated_at from now is not stale for any reasonable TTL."""
    from beagle.style_guides.render import GooseTopOfMindRenderer

    f = tmp_path / "tom.xml"
    fresh = _dt.datetime.now(_dt.UTC).isoformat()
    f.write_text(
        f'<beagle_top_of_mind>\n<hydrated hydrated_at="{fresh}" source="rag+chat">'
        "\n</hydrated>\n</beagle_top_of_mind>"
    )
    assert GooseTopOfMindRenderer._hydration_is_stale(f, ttl_seconds=60) is False


def test_hydration_is_stale_old_block_is_stale(tmp_path):
    """A hydrated_at from >TTL ago forces a re-render."""
    from beagle.style_guides.render import GooseTopOfMindRenderer

    f = tmp_path / "tom.xml"
    old = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(seconds=120)).isoformat()
    f.write_text(
        f'<beagle_top_of_mind>\n<hydrated hydrated_at="{old}" source="rag+chat">'
        "\n</hydrated>\n</beagle_top_of_mind>"
    )
    # TTL=60s, age=120s → stale
    assert GooseTopOfMindRenderer._hydration_is_stale(f, ttl_seconds=60) is True


def test_hydration_is_stale_unparseable_timestamp_returns_true(tmp_path):
    """A garbage hydrated_at value is treated as stale (defensive)."""
    from beagle.style_guides.render import GooseTopOfMindRenderer

    f = tmp_path / "tom.xml"
    f.write_text(
        '<beagle_top_of_mind>\n<hydrated hydrated_at="not-a-date" source="rag+chat">'
        "\n</hydrated>\n</beagle_top_of_mind>"
    )
    assert GooseTopOfMindRenderer._hydration_is_stale(f, ttl_seconds=3600) is True


def test_hydration_is_stale_naive_timestamp_treated_as_utc(tmp_path):
    """A naive datetime is assumed UTC (Beagle datetime policy)."""
    from beagle.style_guides.render import GooseTopOfMindRenderer

    f = tmp_path / "tom.xml"
    # Naive (no tz) timestamp from 5s ago.
    # Derived from an aware UTC clock, then stripped: the naive shape is the
    # test input, but we never read a naive wall clock to produce it (DTZ005).
    naive_recent = (
        (_dt.datetime.now(_dt.UTC) - _dt.timedelta(seconds=5)).replace(tzinfo=None).isoformat()
    )
    f.write_text(
        f'<beagle_top_of_mind>\n<hydrated hydrated_at="{naive_recent}" '
        f'source="rag+chat">\n</hydrated>\n</beagle_top_of_mind>'
    )
    # TTL=60s, age=5s → not stale (naive assumed UTC).
    assert GooseTopOfMindRenderer._hydration_is_stale(f, ttl_seconds=60) is False


# ── 3. render_canonical uses the TTL ─────────────────────────────────────


def test_render_canonical_force_renders_when_destination_has_hydrated_block(tmp_path, monkeypatch):
    """force=True forces a re-render when the artefact already has a hydrated block.

    This is the test-equivalent of the watchdog's "RAG index was just
    updated, refresh the hydrated block" call path, and covers the case
    where the hydrator has already run.
    """
    from beagle.style_guides.render import GooseTopOfMindRenderer

    monkeypatch.setenv("HOME", str(tmp_path))
    canonical = tmp_path / ".config" / "goose" / "beagle_top_of_mind.xml"
    canonical.parent.mkdir(parents=True, exist_ok=True)

    # Pre-populate with a *fresh* hydrated block.
    fresh = _dt.datetime.now(_dt.UTC).isoformat()
    canonical.write_text(
        f'<beagle_top_of_mind>\n<hydrated hydrated_at="{fresh}" source="rag+chat">'
        "\n</hydrated>\n</beagle_top_of_mind>"
    )
    pre_mtime = canonical.stat().st_mtime
    pre_content = canonical.read_text()

    # With max_age_seconds=60, a fresh block is NOT stale → short-circuit.
    # To exercise the override, we need a "render" call that would
    # actually do work. Patch render_to_file_hydrated to record the
    # call so we can detect a forced re-render.
    called = {"count": 0}

    def _fake_render_to_file(self, path, domain=None):
        del domain  # required keyword of the mocked signature; unused here
        called["count"] += 1
        path.write_text(
            '<beagle_top_of_mind>\n<hydrated hydrated_at="OVERRIDE" '
            'source="rag+chat">\n</hydrated>\n</beagle_top_of_mind>'
        )
        return path

    monkeypatch.setattr(GooseTopOfMindRenderer, "render_to_file_hydrated", _fake_render_to_file)

    # Fresh block + default TTL → no re-render.
    GooseTopOfMindRenderer().render_canonical()
    assert called["count"] == 0
    assert canonical.read_text() == pre_content

    # Same fresh block + force=True → forced re-render.
    GooseTopOfMindRenderer().render_canonical(force=True)
    assert called["count"] == 1
    assert "OVERRIDE" in canonical.read_text()
    # And mtime bumped.
    assert canonical.stat().st_mtime >= pre_mtime


# ── BGL-050 regression: force must work on a destination with NO hydration
#    block, which is what production has. The sibling test above covers an
#    artefact WITH a hydrated block; its fixture supplied the only condition
#    under which the old code worked, so the old code was green in CI and
#    broken in production.
def test_force_renders_when_destination_has_no_hydrated_block(tmp_path, monkeypatch):
    """force=True renders even when the destination has no hydrated block.

    This is the production precondition: the live artefact carries a
    <hydrator> placeholder and no <hydrated> element. The old code routed
    force through _hydration_is_stale, which returned False on such an
    artefact, so the render was silently skipped.
    """
    from beagle.style_guides.render import GooseTopOfMindRenderer

    monkeypatch.setenv("HOME", str(tmp_path))
    canonical = tmp_path / ".config" / "goose" / "beagle_top_of_mind.xml"
    canonical.parent.mkdir(parents=True, exist_ok=True)

    canonical.write_text("<beagle_top_of_mind>\n<hydrator/>\n</beagle_top_of_mind>")
    called = {"count": 0}

    def _fake_render_to_file(self, path, domain=None):
        del domain  # required keyword of the mocked signature; unused here
        called["count"] += 1
        path.write_text("<beagle_top_of_mind/>\n")
        return path

    monkeypatch.setattr(GooseTopOfMindRenderer, "render_to_file_hydrated", _fake_render_to_file)

    GooseTopOfMindRenderer().render_canonical(force=True)
    assert called["count"] == 1, "force=True must render even with no hydrated block"


def test_no_force_short_circuits_when_destination_has_no_hydrated_block(tmp_path, monkeypatch):
    """force=False short-circuits when the destination has no hydrated block.

    TM-1 deliberately did not change the cache policy for force=False. A
    fresh artefact with no hydrated block and unchanged source TOMLs must
    short-circuit without rendering.
    """
    from beagle.style_guides.render import GooseTopOfMindRenderer

    monkeypatch.setenv("HOME", str(tmp_path))
    canonical = tmp_path / ".config" / "goose" / "beagle_top_of_mind.xml"
    canonical.parent.mkdir(parents=True, exist_ok=True)

    canonical.write_text("<beagle_top_of_mind>\n<hydrator/>\n</beagle_top_of_mind>")
    called = {"count": 0}

    def _fake_render_to_file(self, path, domain=None):
        del domain  # required keyword of the mocked signature; unused here
        called["count"] += 1
        path.write_text("<beagle_top_of_mind/>\n")
        return path

    monkeypatch.setattr(GooseTopOfMindRenderer, "render_to_file_hydrated", _fake_render_to_file)

    GooseTopOfMindRenderer().render_canonical()
    assert called["count"] == 0, "force=False must short-circuit"


def test_render_canonical_triggers_on_stale_hydration(tmp_path, monkeypatch):
    """A 5-minute-old hydrated block, with TTL=60s, forces a re-render.

    The TOML mtime is NOT changed; only the hydrated block's age
    triggers the re-render. This is the F5 acceptance test.
    """
    from beagle.style_guides.render import GooseTopOfMindRenderer

    monkeypatch.setenv("HOME", str(tmp_path))
    canonical = tmp_path / ".config" / "goose" / "beagle_top_of_mind.xml"
    canonical.parent.mkdir(parents=True, exist_ok=True)

    # Pre-populate with a 5-minute-old hydrated block. Set the
    # artefact's mtime to be *newer* than every TOML (so the TOML
    # short-circuit would otherwise fire and skip the render).
    old = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(seconds=300)).isoformat()
    canonical.write_text(
        f'<beagle_top_of_mind>\n<hydrated hydrated_at="{old}" source="rag+chat">'
        "\n</hydrated>\n</beagle_top_of_mind>"
    )
    import os

    # Bump artefact mtime to be newer than any TOML.
    future_mtime = (
        max(p.stat().st_mtime for p in (canonical.parent).glob("*.toml")) + 100
        if list((canonical.parent).glob("*.toml"))
        else 9999999999
    )
    # Set artefact mtime to future (no TOMLs in tmp_path anyway).
    os.utime(canonical, (future_mtime, future_mtime))

    called = {"count": 0}

    def _fake_render(self, path, domain=None):
        del domain  # required keyword of the mocked signature; unused here
        called["count"] += 1
        path.write_text(
            '<beagle_top_of_mind>\n<hydrated hydrated_at="FRESH" '
            'source="rag+chat">\n</hydrated>\n</beagle_top_of_mind>'
        )
        return path

    monkeypatch.setattr(GooseTopOfMindRenderer, "render_to_file_hydrated", _fake_render)

    # With default TTL (60s), a 300s-old block is stale → forced re-render.
    GooseTopOfMindRenderer().render_canonical()
    assert called["count"] == 1
    assert "FRESH" in canonical.read_text()
