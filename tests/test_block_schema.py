"""Tests for block schema models."""

from __future__ import annotations

from beagle.blocks.schema import (
    AgentDefinition,
    AgentManifest,
    BlockRef,
    SchemaVersion,
    VariableBinding,
)


def test_schema_version_str():
    v = SchemaVersion(major=1, minor=2, patch=3)
    assert str(v) == "1.2.3"


def test_variable_binding_defaults():
    vb = VariableBinding(name="x")
    assert vb.source == "literal"
    assert vb.required is True
    assert vb.value is None


def test_block_ref():
    br = BlockRef(name="my_block", output_as="result")
    assert br.name == "my_block"
    assert br.output_as == "result"


def test_agent_definition_defaults():
    ad = AgentDefinition(name="test_agent")
    assert ad.name == "test_agent"
    assert ad.blocks == []
    assert ad.style_guides == []
    assert ad.variables == []
    assert ad.model == "default"
    assert ad.max_depth == 10


def test_agent_manifest():
    am = AgentManifest(name="test")
    assert am.blocks == []
    assert am.inputs == {}


def test_agent_definition_with_blocks():
    ad = AgentDefinition(
        name="dev_agent",
        blocks=["plan", "execute", "verify"],
        variables=[VariableBinding(name="lang", value="python")],
    )
    assert len(ad.blocks) == 3
    assert ad.variables[0].name == "lang"
