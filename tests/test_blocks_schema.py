"""SP-5: tests for blocks/schema (was zero-coverage).

beagle-spotless-phase2, work package SP-5. The pydantic v2 block schema models
(SchemaVersion, VariableBinding, BlockRef, AgentDefinition) and the AgentManifest
dataclass had no direct tests.
"""

from __future__ import annotations

from beagle.blocks.schema import (
    AgentDefinition,
    AgentManifest,
    BlockRef,
    SchemaVersion,
    VariableBinding,
)


def test_schema_version_defaults_and_str() -> None:
    """SchemaVersion defaults to 1.0.0 and renders as X.Y.Z."""
    v = SchemaVersion()
    assert (v.major, v.minor, v.patch) == (1, 0, 0)
    assert str(v) == "1.0.0"


def test_schema_version_custom() -> None:
    """SchemaVersion can be built with explicit parts."""
    v = SchemaVersion(major=2, minor=3, patch=4)
    assert str(v) == "2.3.4"


def test_variable_binding_defaults() -> None:
    """VariableBinding has literal source and required=True by default."""
    vb = VariableBinding(name="x")
    assert vb.source == "literal"
    assert vb.value is None
    assert vb.default is None
    assert vb.required is True


def test_block_ref_defaults() -> None:
    """BlockRef has optional output_as and condition."""
    ref = BlockRef(name="block1")
    assert ref.name == "block1"
    assert ref.output_as is None
    assert ref.condition is None


def test_agent_definition_defaults() -> None:
    """AgentDefinition carries a version and empty block list by default."""
    ad = AgentDefinition(name="agent1")
    assert str(ad.version) == "1.0.0"
    assert ad.blocks == []
    assert ad.style_guides == []
    assert ad.model == "default"
    assert ad.max_depth == 10


def test_agent_definition_full() -> None:
    """AgentDefinition accepts nested VariableBinding objects."""
    ad = AgentDefinition(
        name="agent1",
        variables=[VariableBinding(name="mode", value="audit")],
        blocks=["read_file", "render"],
    )
    assert len(ad.variables) == 1
    assert ad.variables[0].name == "mode"
    assert ad.blocks == ["read_file", "render"]


def test_agent_manifest_defaults() -> None:
    """AgentManifest has empty blocks/inputs/style_guides by default."""
    m = AgentManifest(name="m")
    assert m.blocks == []
    assert m.inputs == {}
    assert m.style_guides == []


def test_agent_manifest_with_refs() -> None:
    """AgentManifest materializes BlockRef objects."""
    m = AgentManifest(name="m", blocks=[BlockRef(name="b")])
    assert len(m.blocks) == 1
    assert m.blocks[0].name == "b"
