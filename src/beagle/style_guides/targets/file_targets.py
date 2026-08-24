"""File-based render targets: write rendered content to a file.

These are the targets for front ends that consume directives from a file on
disk — ``.goosehints`` (goose), ``CLAUDE.md`` (claude), and the canonical
``beagle_top_of_mind.xml``. Each writes atomically and is pure/offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from beagle.style_guides.targets.base import EmitOptions


@dataclass
class FileRenderTarget:
    """Write rendered content to a named file under a scope or target dir.

    Attributes:
        name: The CLI-facing target name.
        filename: The file to write (e.g. ``.goosehints``, ``CLAUDE.md``).

    """

    name: str
    filename: str

    def emit(self, content: str, options: EmitOptions) -> str:
        """Write content to the target file.

        Args:
            content: The rendered directive text.
            options: Emission options.

        Returns:
            A status string naming the written path.

        """
        base = options.target_dir or options.scope or Path.cwd()
        target = Path(base) / self.filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"{self.name} -> {target}"


goosehints_target = FileRenderTarget(name="goosehints", filename=".goosehints")
claude_md_target = FileRenderTarget(name="claude_md", filename="CLAUDE.md")
top_of_mind_xml_target = FileRenderTarget(name="top_of_mind_xml", filename="beagle_top_of_mind.xml")
