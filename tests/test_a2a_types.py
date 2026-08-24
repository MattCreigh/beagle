"""SP-5: tests for bridges/a2a_types.AgentCard (was zero-coverage).

beagle-spotless-phase2, work package SP-5. The AgentCard dataclass was
extracted into this leaf module in SP-7 (to break the a2a_server <->
a2a_card_builder cycle) and had no direct test coverage. These tests exercise
the dataclass construction, field defaults, and the re-export from a2a_server.
"""

from __future__ import annotations

from dataclasses import fields

from beagle.bridges.a2a_types import AgentCard


def test_agent_card_defaults() -> None:
    """AgentCard has the A2A-spec defaults."""
    card = AgentCard(name="researcher")
    assert card.name == "researcher"
    assert card.description == ""
    assert card.version == "1.0.0"
    assert card.capabilities == []
    assert card.input_schema == {}
    assert card.output_schema == {}
    assert card.endpoint_url == ""


def test_agent_card_required_fields() -> None:
    """Only name is required; the rest default."""
    card = AgentCard(name="coder")
    assert card.name == "coder"


def test_agent_card_all_fields() -> None:
    """Every field is assignable at construction."""
    card = AgentCard(
        name="auditor",
        description="security review",
        version="2.1.0",
        capabilities=["execute_workflow"],
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        endpoint_url="http://localhost:8000/a2a",
    )
    assert card.description == "security review"
    assert card.version == "2.1.0"
    assert card.capabilities == ["execute_workflow"]
    assert card.input_schema == {"type": "object"}
    assert card.endpoint_url == "http://localhost:8000/a2a"


def test_agent_card_field_order() -> None:
    """Field order matches the A2A spec (name first, then optional metadata)."""
    field_names = [f.name for f in fields(AgentCard)]
    assert field_names == [
        "name",
        "description",
        "version",
        "capabilities",
        "input_schema",
        "output_schema",
        "endpoint_url",
    ]


def test_agent_card_reexported_from_a2a_server() -> None:
    """a2a_server re-exports AgentCard for backward compatibility."""
    from beagle.bridges.a2a_server import AgentCard as Reexported

    assert Reexported is AgentCard


def test_agent_card_is_dataclass() -> None:
    """AgentCard is a dataclass (equality + repr work)."""
    a = AgentCard(name="x")
    b = AgentCard(name="x")
    c = AgentCard(name="y")
    assert a == b
    assert a != c
    assert "AgentCard" in repr(a)
