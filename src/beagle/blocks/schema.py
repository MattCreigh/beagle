"""Pydantic v2 schema models for block agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field


class SchemaVersion(BaseModel):
    """Version identifier for block schema compatibility."""

    major: int = 1
    minor: int = 0
    patch: int = 0

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


class VariableBinding(BaseModel):
    """Variable binding for template rendering."""

    name: str
    source: str = "literal"  # "literal", "input", "block_output", "env"
    value: str | None = None
    default: str | None = None
    required: bool = True


class BlockRef(BaseModel):
    """Reference to another block by name."""

    name: str
    output_as: str | None = None  # alias the output under this name
    condition: str | None = None  # Jinja condition for conditional deps


class AgentDefinition(BaseModel):
    """Definition of a block-composed agent from TOML/YAML."""

    name: str
    version: SchemaVersion = Field(default_factory=SchemaVersion)
    description: str = ""
    blocks: list[str] = Field(default_factory=list)
    style_guides: list[str] = Field(default_factory=list)
    variables: list[VariableBinding] = Field(default_factory=list)
    model: str = "default"
    max_depth: int = 10
    schema_version: str = "1.0.0"


@dataclass
class AgentManifest:
    """Resolved manifest with all blocks and dependencies materialized."""

    name: str
    blocks: list[BlockRef] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)
    style_guides: list[str] = field(default_factory=list)
