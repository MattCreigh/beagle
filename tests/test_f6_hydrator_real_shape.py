"""F6 regression — hydrator summariser uses real RAG response field names.

v13.22.2 audit found that ``_summarise_rag`` in ``tom_hydrator.py``
used the *test-mock* field names (``file_path``, ``score``,
``from``/``relation``/``to``) but the real ``mcp_rag_server.rag_search``
returns a different shape:

  semantic_anchors[i]:
      ast_entity_id, file, node_name, node_type,
      start_line, end_line, content, distance

  structural_relations[i]:
      source_node, relationship, target_node, filepath, context_snippet

The mock-shape fields do not appear in the real response, so
``_summarise_rag`` returned ``""`` on every real call — the visible
symptom was an artefact with empty ``<rag_result>`` blocks even when
RAG was returning data.

The fix:
- ``_summarise_rag`` now prefers live-shape field names and falls
  back to the old mock names so the F4/F5 tests keep passing.
- This module locks in the live-shape behaviour with a test that
  exercises the real shape and asserts the summary is non-empty.

v13.22.2 — first revision.
"""

from __future__ import annotations

import json

# ── 1. _summarise_rag handles the real response shape ────────────────────


def test_summarise_rag_handles_real_anchor_shape():
    """The live anchor shape uses ``file`` + ``distance`` + ``content``."""
    from beagle.style_guides import tom_hydrator

    live_result = {
        "status": "ok",
        "query": "orchestrator",
        "semantic_anchors": [
            {
                "ast_entity_id": "abc",
                "file": "/Projects/beagle/beagle/core/orchestrator.py",
                "node_name": "run_workflow",
                "node_type": "function_definition",
                "start_line": 100,
                "end_line": 120,
                "content": "def run_workflow(query):\n    state = ...\n    return state",
                "distance": 0.18,
            },
        ],
        "structural_relations": [],
        "metadata": {"vector_count": 1, "graph_count": 0, "duration_ms": 12.3},
    }
    out = tom_hydrator._summarise_rag(live_result)
    assert out, "summary must be non-empty for real-shape anchor"
    # The path is rendered (the real RAG path is the headline signal).
    assert "orchestrator.py" in out
    # The content snippet is rendered — this is the high-signal piece.
    assert "run_workflow" in out
    # The similarity (1 - distance/2) is computed and shown.
    assert "sim=" in out


def test_summarise_rag_handles_real_relation_shape():
    """The live relation shape uses ``source_node`` + ``relationship`` + ``target_node`` + ``filepath`` + ``context_snippet``."""
    from beagle.style_guides import tom_hydrator

    live_result = {
        "status": "ok",
        "query": "x",
        "semantic_anchors": [],
        "structural_relations": [
            {
                "source_node": "run_workflow",
                "relationship": "CALLS",
                "target_node": "load_state",
                "filepath": "/Projects/beagle/beagle/core/orchestrator.py",
                "context_snippet": "def run_workflow(query):\n    state = load_state()\n",
            },
        ],
        "metadata": {},
    }
    out = tom_hydrator._summarise_rag(live_result)
    assert out, "summary must be non-empty for real-shape relation"
    assert "run_workflow" in out
    assert "CALLS" in out
    assert "load_state" in out
    assert "orchestrator.py" in out
    assert "load_state" in out or "state" in out  # context snippet headline


def test_summarise_rag_handles_mixed_real_anchors_and_relations():
    """A full live response with both anchors and relations is rendered fully."""
    from beagle.style_guides import tom_hydrator

    live_result = {
        "status": "ok",
        "query": "x",
        "semantic_anchors": [
            {
                "ast_entity_id": "x1",
                "file": "/a/b.py",
                "node_name": "fn_a",
                "node_type": "function",
                "start_line": 1,
                "end_line": 2,
                "content": "def fn_a(): pass",
                "distance": 0.5,
            },
            {
                "ast_entity_id": "x2",
                "file": "/c/d.py",
                "node_name": "fn_b",
                "node_type": "function",
                "start_line": 3,
                "end_line": 4,
                "content": "def fn_b(): return 1",
                "distance": 1.5,  # near-orthogonal; sim ~ 0.25
            },
        ],
        "structural_relations": [
            {
                "source_node": "fn_a",
                "relationship": "CALLS",
                "target_node": "fn_b",
                "filepath": "/a/b.py",
                "context_snippet": "fn_a()",
            }
        ],
        "metadata": {},
    }
    out = tom_hydrator._summarise_rag(live_result)
    # Two anchors and one relation = three lines.
    lines = [ln for ln in out.splitlines() if ln]
    assert len(lines) == 3, f"expected 3 lines, got {len(lines)}: {out!r}"
    assert "fn_a" in lines[0] and "/a/b.py" in lines[0]
    assert "fn_b" in lines[1] and "/c/d.py" in lines[1]
    # Lower-similarity anchor should still render (the cap is on path
    # count, not score; the operator wants to see what the RAG found).
    assert "REL:" in lines[2]


# ── 2. _summarise_rag backward-compat with the mock field names ──────────


def test_summarise_rag_accepts_legacy_mock_field_names():
    """The F4 tests use the mock shape; this locks in backward-compat."""
    from beagle.style_guides import tom_hydrator

    mock_result = {
        "semantic_anchors": [{"file_path": "/x/y.py", "score": 0.9}],
        "structural_relations": [],
    }
    out = tom_hydrator._summarise_rag(mock_result)
    assert "y.py" in out
    assert "score=0.90" in out


def test_summarise_rag_accepts_legacy_relation_field_names():
    from beagle.style_guides import tom_hydrator

    mock_result = {
        "semantic_anchors": [],
        "structural_relations": [{"from": "alpha", "relation": "DEPENDS_ON", "to": "beta"}],
    }
    out = tom_hydrator._summarise_rag(mock_result)
    assert "alpha" in out
    assert "beta" in out
    assert "DEPENDS_ON" in out


# ── 3. Full end-to-end through the real mcp_rag_server shape ─────────────


def test_hydrate_against_real_rag_shape_returns_non_empty_blocks(tmp_path, monkeypatch):
    """Mock mcp_rag_server.rag_search with the real response shape, run
    hydrate(), and assert the resulting <rag_result> block is non-empty.

    This is the closest in-process reproduction of the production
    scenario where the user complains "the hydrator returns empty
    <rag_result> blocks". With the F6 fix, the block content is
    non-empty; without it, the block would be ``>…</`` (empty).
    """
    from beagle.style_guides import tom_hydrator

    real_shape = {
        "status": "ok",
        "query": "anything",
        "semantic_anchors": [
            {
                "ast_entity_id": "z1",
                "file": "/Projects/beagle/beagle/cli/cli.py",
                "node_name": "main",
                "node_type": "function",
                "start_line": 2000,
                "end_line": 2030,
                "content": "def main():\n    # CLI entry point",
                "distance": 0.25,
            }
        ],
        "structural_relations": [
            {
                "source_node": "main",
                "relationship": "CALLS",
                "target_node": "render_canonical",
                "filepath": "/Projects/beagle/beagle/style_guides/render.py",
                "context_snippet": "render_canonical()",
            }
        ],
        "metadata": {},
    }

    async def fake_real_rag(query, max_hops=1, top_k=3):
        return json.dumps(real_shape)

    import beagle.infrastructure.mcp_rag_server as _rag_mod

    monkeypatch.setattr(_rag_mod, "rag_search", fake_real_rag)

    # Stub chatrecall to return [] (the live adapter still returns
    # []; the chat-side is locked in by the F4 tests).
    import beagle.style_guides._chatrecall_adapter as _chat_mod

    monkeypatch.setattr(_chat_mod, "chatrecall", lambda query, limit=10: [])

    xml = (
        "<beagle_top_of_mind>\n"
        "  <hydrator>\n"
        '    <rag id="rag_0" query="anything" guide="test"/>\n'
        "  </hydrator>\n"
        "</beagle_top_of_mind>\n"
    )
    queries = [{"id": "rag_0", "source": "rag", "query": "anything", "guide": "test"}]

    out = tom_hydrator.hydrate(xml, queries)

    # The RAG result must be rendered (this is what was broken).
    assert "<rag_result" in out
    rag_block = out.split("<rag_result", 1)[1].split("</rag_result>", 1)[0]
    inner = rag_block.split(">", 1)[1] if ">" in rag_block else ""
    assert inner.strip(), (
        f"<rag_result> block is empty — the F6 regression has returned. Got: {rag_block!r}"
    )
    # The path and node-name are surfaced.
    assert "cli.py" in inner
    assert "main" in inner
    # The relation is also surfaced.
    assert "render_canonical" in inner
