"""WS1: lock the ``render_canonical`` staleness guard.

``render_canonical`` is the per-turn live-propagation entry point: the
external Goose "tom extension" (``GOOSE_MOIM_MESSAGE_FILE``) calls it (via
``beagle render-hints --quiet``) every turn so a mid-session edit to a
style-guide TOML reaches the running model on the next turn.

For that to be safe it must satisfy two invariants, pinned here so a
regression fails at test time rather than live:

  * **Cheap when fresh** — when no source TOML has changed (and the
    hydrated block, if any, is within TTL) it MUST short-circuit and
    return the existing artefact WITHOUT re-rendering. A regression here
    re-renders every turn and burns the RAG/chat hydration budget.
  * **Fresh when stale** — when any source TOML is newer than the
    artefact (or the artefact is missing, or a force is requested) it MUST
    re-render. A regression here serves a stale Top-of-Mind past a TOML
    edit, silently ignoring the doctrine change the edit made.

The single re-render path is ``render_to_file_hydrated`` (render.py:620);
stubbing it captures every re-render without touching the network/MCP, and
monkeypatching the module-level ``_CANONICAL_PATH`` keeps the test off the
real ``~/.config/goose`` artefact.
"""

from __future__ import annotations

import os
from pathlib import Path

import beagle.style_guides.render as render_mod
from beagle.style_guides.render import GooseTopOfMindRenderer

_FRESH_DEST_CONTENT = "<beagle_top_of_mind></beagle_top_of_mind>\n"


def _make_renderer(
    monkeypatch,
    tmp_path: Path,
    *,
    source_mtime: float,
    dest_exists: bool = True,
    dest_mtime: float | None = None,
    dest_content: str = _FRESH_DEST_CONTENT,
):
    """Build a renderer whose source-mtime, canonical dest, and re-render
    path are all controlled, so ``render_canonical`` can be exercised
    hermetically (no real TOML touches, no network hydration).

    Returns ``(renderer, dest_path, calls)`` where ``calls["n"]`` counts
    how many times the (stubbed) re-render path fired.
    """
    dest = tmp_path / "beagle_top_of_mind.xml"
    if dest_exists:
        dest.write_text(dest_content, encoding="utf-8")
        if dest_mtime is not None:
            os.utime(dest, (dest_mtime, dest_mtime))
    monkeypatch.setattr(render_mod, "_CANONICAL_PATH", dest)

    renderer = GooseTopOfMindRenderer()
    # Control the "newest source TOML mtime" the guard compares against.
    monkeypatch.setattr(renderer, "_newest_source_mtime", lambda: source_mtime)

    calls = {"n": 0}

    def _fake_hydrated(d, domain=None):
        calls["n"] += 1
        Path(d).write_text("<beagle_top_of_mind>rendered</beagle_top_of_mind>\n", encoding="utf-8")
        return Path(d)

    monkeypatch.setattr(renderer, "render_to_file_hydrated", _fake_hydrated)
    return renderer, dest, calls


def test_no_rerender_when_sources_unchanged(monkeypatch, tmp_path):
    """Sources older than the artefact, no hydrated block → no re-render."""
    renderer, _dest, calls = _make_renderer(
        monkeypatch, tmp_path, source_mtime=1000.0, dest_mtime=2000.0
    )
    out = renderer.render_canonical()
    assert out == _dest
    assert calls["n"] == 0, (
        "render_canonical re-rendered despite unchanged source TOMLs — the "
        "per-turn call must be a no-op when fresh, or it burns the "
        "RAG/chat hydration budget every turn."
    )


def test_rerender_when_source_toml_newer(monkeypatch, tmp_path):
    """A source TOML newer than the artefact → must re-render."""
    renderer, _dest, calls = _make_renderer(
        monkeypatch, tmp_path, source_mtime=3000.0, dest_mtime=2000.0
    )
    renderer.render_canonical()
    assert calls["n"] == 1, (
        "render_canonical did NOT re-render after a source TOML changed — a "
        "mid-session edit to a style-guide TOML would never reach the model."
    )


def test_rerender_when_dest_missing(monkeypatch, tmp_path):
    """No artefact on disk yet → must render."""
    renderer, _dest, calls = _make_renderer(
        monkeypatch, tmp_path, source_mtime=1000.0, dest_exists=False
    )
    renderer.render_canonical()
    assert calls["n"] == 1


def test_no_hydrated_block_means_no_age_rerender(monkeypatch, tmp_path):
    """Fresh sources + no ``<hydrated>`` block + positive TTL → still a no-op.

    The hydration-age clock only governs artefacts that actually carry a
    hydrated block; a pure-TOML artefact must not be re-rendered on age.
    """
    renderer, _dest, calls = _make_renderer(
        monkeypatch, tmp_path, source_mtime=1000.0, dest_mtime=2000.0
    )
    renderer.render_canonical(max_age_seconds=60)
    assert calls["n"] == 0


def test_force_rerender_with_zero_max_age(monkeypatch, tmp_path):
    """Fresh sources but a stale ``<hydrated>`` block under a 0s TTL → force.

    ``force=True`` is the only way to bypass the cache policy (watchdog
    after a RAG index update). It must re-render even when the source TOMLs
    are unchanged, provided the artefact carries an aged hydrated block.
    """
    hydrated = '<hydrated hydrated_at="2000-01-01T00:00:00+00:00"/>\n'
    renderer, _dest, calls = _make_renderer(
        monkeypatch,
        tmp_path,
        source_mtime=1000.0,
        dest_mtime=2000.0,
        dest_content=hydrated,
    )
    renderer.render_canonical(force=True)
    assert calls["n"] == 1, (
        "force=True must re-render an aged hydrated artefact even when the "
        "source TOMLs are unchanged."
    )
