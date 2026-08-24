"""XMLBlock dataclass and loader for XML-based blocks."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from defusedxml import ElementTree as ET


@dataclass(frozen=True)
class XMLBlock:
    """Frozen dataclass for an XML-defined block."""

    name: str
    description: str = ""
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    template: str = ""  # raw body text between start/end tags
    raw_xml: str = ""  # entire block XML


def load_xml_block(path: Path) -> XMLBlock:
    """Load an XML block from file.

    Expected format:
    <block name="...">
      <description>...</description>
      <inputs>...</inputs>
      <outputs>...</outputs>
      <template>...Jinja2 template...</template>
    </block>
    """
    raw = path.read_text(encoding="utf-8")
    root = ET.fromstring(raw)
    if root.tag != "block":
        raise ValueError(f"Expected root <block>, got <{root.tag}> in {path}")

    name = root.get("name", path.stem)
    description = ""
    inputs: list[str] = []
    outputs: list[str] = []
    template = ""

    for child in root:
        if child.tag == "description":
            description = (child.text or "").strip()
        elif child.tag == "inputs":
            inputs = [s.strip() for s in (child.text or "").split(",") if s.strip()]
        elif child.tag == "outputs":
            outputs = [s.strip() for s in (child.text or "").split(",") if s.strip()]
        elif child.tag == "template":
            template = child.text or ""

    return XMLBlock(
        name=name,
        description=description,
        inputs=inputs,
        outputs=outputs,
        template=template,
        raw_xml=raw,
    )
