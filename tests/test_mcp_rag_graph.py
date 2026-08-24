"""Tests for Kùzu graph query MCP tools (F13).

Validates graph_callers, graph_callees, graph_imports,
graph_dependents, and graph_class_hierarchy.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def mock_kuzu_conn():
    """Mock Kùzu connection with configurable results."""
    conn = MagicMock()

    class MockResults:
        def __init__(self, rows):
            self._rows = rows
            self._idx = 0

        def has_next(self):
            return self._idx < len(self._rows)

        def get_next(self):
            row = self._rows[self._idx]
            self._idx += 1
            return row

    def execute(cypher, parameters=None):
        # Default: return empty results
        return MockResults([])

    conn.execute = execute
    return conn


@pytest.fixture
def mock_mcp_env(mock_kuzu_conn):
    """Set up mock environment for MCP RAG server tests."""
    from beagle.infrastructure import mcp_rag_server

    mcp_rag_server._kuzu_conn = mock_kuzu_conn
    mcp_rag_server._initialized = True
    yield mcp_rag_server


@pytest.mark.asyncio
async def test_graph_callers_empty(mock_mcp_env):
    """graph_callers returns empty list when no results."""
    result = await mock_mcp_env.graph_callers("nonexistent_function")
    data = json.loads(result)
    assert data["function"] == "nonexistent_function"
    assert data["callers"] == []


@pytest.mark.asyncio
async def test_graph_callees_empty(mock_mcp_env):
    """graph_callees returns empty list when no results."""
    result = await mock_mcp_env.graph_callees("nonexistent_function")
    data = json.loads(result)
    assert data["function"] == "nonexistent_function"
    assert data["callees"] == []


@pytest.mark.asyncio
async def test_graph_imports_empty(mock_mcp_env):
    """graph_imports returns empty list when no results."""
    result = await mock_mcp_env.graph_imports("nonexistent_module.py")
    data = json.loads(result)
    assert data["module"] == "nonexistent_module.py"
    assert data["imports"] == []


@pytest.mark.asyncio
async def test_graph_dependents_empty(mock_mcp_env):
    """graph_dependents returns empty list when no results."""
    result = await mock_mcp_env.graph_dependents("nonexistent_module.py")
    data = json.loads(result)
    assert data["module"] == "nonexistent_module.py"
    assert data["dependents"] == []


@pytest.mark.asyncio
async def test_graph_class_hierarchy_empty(mock_mcp_env):
    """graph_class_hierarchy returns empty ancestors/descendants."""
    result = await mock_mcp_env.graph_class_hierarchy("NonexistentClass")
    data = json.loads(result)
    assert data["class"] == "NonexistentClass"
    assert data["ancestors"] == []
    assert data["descendants"] == []


@pytest.mark.asyncio
async def test_graph_callers_with_results(mock_mcp_env):
    """graph_callers returns results from Kùzu query."""
    mock_results = [
        ["caller_a", "/path/to/file1.py"],
    ]

    class Results:
        def __init__(self, rows):
            self._rows = rows
            self._idx = 0

        def has_next(self):
            return self._idx < len(self._rows)

        def get_next(self):
            row = self._rows[self._idx]
            self._idx += 1
            return row

    mock_mcp_env._kuzu_conn.execute = lambda _q, **_: Results(mock_results)

    result = await mock_mcp_env.graph_callers("target_func")
    data = json.loads(result)
    assert data["function"] == "target_func"
    assert len(data["callers"]) > 0


@pytest.mark.asyncio
async def test_graph_class_hierarchy(mock_mcp_env):
    """graph_class_hierarchy returns ancestors from Kùzu."""
    mock_results = [
        ["BaseClass", "/path/to/base.py"],
    ]

    class Results:
        def __init__(self, rows):
            self._rows = rows
            self._idx = 0

        def has_next(self):
            return self._idx < len(self._rows)

        def get_next(self):
            row = self._rows[self._idx]
            self._idx += 1
            return row

    mock_mcp_env._kuzu_conn.execute = lambda _q, **_: Results(mock_results)

    result = await mock_mcp_env.graph_class_hierarchy("DerivedClass")
    data = json.loads(result)
    assert data["class"] == "DerivedClass"
    assert len(data["ancestors"]) > 0


def test_get_graph_hops_and_limit(mock_mcp_env):
    """Config values are read correctly."""
    # When config.toml doesn't exist, uses defaults
    hops, limit = mock_mcp_env._get_graph_hops_and_limit()
    assert hops == 3
    assert limit == 20


def test_execute_graph_query_no_conn(mock_mcp_env):
    """Returns empty list when Kùzu connection is unavailable."""
    mock_mcp_env._kuzu_conn = None
    rows = mock_mcp_env._execute_graph_query("MATCH (n) RETURN n")
    assert rows == []


@pytest.mark.asyncio
async def test_graph_callers_cypher_uses_canonical_node_type(mock_mcp_env):
    """Regression: graph_callers cypher must filter on 'function', not 'FunctionDef'.

    The indexed kuzu data uses lowercase node_type values ('function', 'class')
    per cast_ingestion.py:358 and the ASTChunk docstring at line 206. The
    previous bug used 'FunctionDef' (Python AST style) which never matched
    any indexed node, returning empty for all queries.
    """
    captured: dict[str, str] = {}

    class CapturingResults:
        def has_next(self) -> bool:
            return False

        def get_next(self):
            return None

    def capturing_execute(cypher, parameters=None):
        _ = parameters  # kwarg kept for kuzu execute() signature parity
        captured["cypher"] = cypher
        return CapturingResults()

    mock_mcp_env._kuzu_conn.execute = capturing_execute

    await mock_mcp_env.graph_callers("any_function")
    assert "cypher" in captured, "graph_callers did not invoke kuzu"
    cypher = captured["cypher"]
    assert "node_type = 'function'" in cypher, (
        f"graph_callers cypher missing 'function' filter; got: {cypher!r}"
    )
    assert "FunctionDef" not in cypher, (
        f"graph_callers cypher still uses 'FunctionDef' (regression!); got: {cypher!r}"
    )


@pytest.mark.asyncio
async def test_graph_callees_cypher_uses_canonical_node_type(mock_mcp_env):
    """Regression: graph_callees cypher must filter on 'function', not 'FunctionDef'."""
    captured: dict[str, str] = {}

    class CapturingResults:
        def has_next(self) -> bool:
            return False

        def get_next(self):
            return None

    def capturing_execute(cypher, parameters=None):
        _ = parameters  # kwarg kept for kuzu execute() signature parity
        captured["cypher"] = cypher
        return CapturingResults()

    mock_mcp_env._kuzu_conn.execute = capturing_execute

    await mock_mcp_env.graph_callees("any_function")
    assert "cypher" in captured
    cypher = captured["cypher"]
    assert "node_type = 'function'" in cypher
    assert "FunctionDef" not in cypher


@pytest.mark.asyncio
async def test_graph_class_hierarchy_cypher_uses_canonical_node_type(mock_mcp_env):
    """Regression: graph_class_hierarchy cypher must filter on 'class', not 'ClassDef',
    and use the canonical 'INHERITS_FROM' relation label (not 'EXTENDS').
    """
    captured_cyphers: list[str] = []

    class CapturingResults:
        def has_next(self) -> bool:
            return False

        def get_next(self):
            return None

    def capturing_execute(cypher, parameters=None):
        captured_cyphers.append(cypher)
        return CapturingResults()

    mock_mcp_env._kuzu_conn.execute = capturing_execute

    await mock_mcp_env.graph_class_hierarchy("AnyClass")
    assert len(captured_cyphers) == 2  # ancestors + descendants
    for cypher in captured_cyphers:
        assert "node_type = 'class'" in cypher, (
            f"graph_class_hierarchy cypher missing 'class' filter; got: {cypher!r}"
        )
        assert "ClassDef" not in cypher, (
            f"graph_class_hierarchy cypher still uses 'ClassDef' (regression!); got: {cypher!r}"
        )
        assert "INHERITS_FROM" in cypher, (
            f"graph_class_hierarchy cypher missing 'INHERITS_FROM' label; got: {cypher!r}"
        )
        assert "EXTENDS" not in cypher, (
            f"graph_class_hierarchy cypher still uses 'EXTENDS' (regression!); got: {cypher!r}"
        )
        # Arrow direction must be valid cypher: -[r*1..N]-> not >[r*1..N]->
        assert ">[r" not in cypher, (
            f"graph_class_hierarchy cypher has malformed arrow direction; got: {cypher!r}"
        )


@pytest.mark.asyncio
async def test_graph_imports_cypher_uses_correct_arrow_and_label(mock_mcp_env):
    """Regression: graph_imports cypher must use proper arrow direction (-[r*1..]->,
    not >[r*1..]->) and the canonical 'IMPORTS' label."""
    captured: dict[str, str] = {}

    class CapturingResults:
        def has_next(self) -> bool:
            return False

        def get_next(self):
            return None

    def capturing_execute(cypher, parameters=None):
        _ = parameters  # kwarg kept for kuzu execute() signature parity
        captured["cypher"] = cypher
        return CapturingResults()

    mock_mcp_env._kuzu_conn.execute = capturing_execute

    await mock_mcp_env.graph_imports("any/module.py")
    assert "cypher" in captured
    cypher = captured["cypher"]
    assert "IMPORTS" in cypher
    assert ">[r" not in cypher, (
        f"graph_imports cypher has malformed arrow direction; got: {cypher!r}"
    )


@pytest.mark.asyncio
async def test_graph_dependents_cypher_uses_correct_arrow_and_label(mock_mcp_env):
    """Regression: graph_dependents cypher must use proper arrow direction and 'IMPORTS' label."""
    captured: dict[str, str] = {}

    class CapturingResults:
        def has_next(self) -> bool:
            return False

        def get_next(self):
            return None

    def capturing_execute(cypher, parameters=None):
        _ = parameters  # kwarg kept for kuzu execute() signature parity
        captured["cypher"] = cypher
        return CapturingResults()

    mock_mcp_env._kuzu_conn.execute = capturing_execute

    await mock_mcp_env.graph_dependents("any/module.py")
    assert "cypher" in captured
    cypher = captured["cypher"]
    assert "IMPORTS" in cypher
    assert ">[r" not in cypher, (
        f"graph_dependents cypher has malformed arrow direction; got: {cypher!r}"
    )


# ── Regression: RECURSIVE_REL binder safety (2026-08-23) ────────────────────
# Variable-length paths bind `r` as RECURSIVE_REL. Filtering `r._label` inside
# WHERE makes Kùzu's binder reject the query outright:
#   Binder exception: r has data type RECURSIVE_REL but (NODE,REL,STRUCT,ANY)
#   was expected
# The relationship filter must live ON THE PATTERN instead: [r:LABEL*1..n].
# Every graph tool that narrows by edge label is pinned here.


def _assert_label_on_pattern(cypher: str, label: str) -> None:
    assert f"{label}" in cypher
    assert "._label" not in cypher, (
        f"cypher filters r._label in WHERE — Kùzu binder rejects RECURSIVE_REL "
        f"property access; move the label onto the pattern. Got: {cypher!r}"
    )
    assert f":{label}*" in cypher.replace(" ", ""), (
        f"variable-length pattern must carry its label explicitly "
        f"(expected ':{label}*'). Got: {cypher!r}"
    )


@pytest.mark.asyncio
async def test_graph_imports_label_lives_on_pattern(mock_mcp_env):
    captured: dict[str, str] = {}

    class NoRows:
        def has_next(self) -> bool:
            return False

        def get_next(self):
            return None

    def capturing_execute(cypher, parameters=None):
        _ = parameters  # kwarg kept for kuzu execute() signature parity
        captured["cypher"] = cypher
        return NoRows()

    mock_mcp_env._kuzu_conn.execute = capturing_execute

    await mock_mcp_env.graph_imports("any/module.py")
    _assert_label_on_pattern(captured["cypher"], "IMPORTS")


@pytest.mark.asyncio
async def test_graph_dependents_label_lives_on_pattern(mock_mcp_env):
    captured: dict[str, str] = {}

    class NoRows:
        def has_next(self) -> bool:
            return False

        def get_next(self):
            return None

    def capturing_execute(cypher, parameters=None):
        _ = parameters  # kwarg kept for kuzu execute() signature parity
        captured["cypher"] = cypher
        return NoRows()

    mock_mcp_env._kuzu_conn.execute = capturing_execute

    await mock_mcp_env.graph_dependents("any/module.py")
    _assert_label_on_pattern(captured["cypher"], "IMPORTS")


@pytest.mark.asyncio
async def test_graph_class_hierarchy_labels_live_on_pattern(mock_mcp_env):
    captured: list[str] = []

    class NoRows:
        def has_next(self) -> bool:
            return False

        def get_next(self):
            return None

    def capturing_execute(cypher, parameters=None):
        _ = parameters  # kwarg kept for kuzu execute() signature parity
        captured.append(cypher)
        return NoRows()

    mock_mcp_env._kuzu_conn.execute = capturing_execute

    await mock_mcp_env.graph_class_hierarchy("SomeClass")
    assert len(captured) == 2, f"expected ancestors+descendants queries, got {len(captured)}"
    for cypher in captured:
        _assert_label_on_pattern(cypher, "INHERITS_FROM")
