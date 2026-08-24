"""Format style guides for XML injection into prompts."""

from __future__ import annotations

import logging
from pathlib import Path

from ._xml import xml_escape
from .loader import StyleGuideLoader

logger = logging.getLogger("Beagle.style_guides.injector")


class ContextInjector:
    """Inject style guide content into prompts as XML blocks."""

    def __init__(self, loader: StyleGuideLoader | None = None) -> None:
        self.loader = loader or StyleGuideLoader()

    def inject(self, file_extension: str) -> str:
        """Generate XML-formatted style guide block for a file extension."""
        guides = self.loader.match(file_extension)
        if not guides:
            return ""

        parts = ["<style_guide>"]
        for guide in guides:
            parts.append(self._render_guide(guide, indent="  "))
        parts.append("</style_guide>")
        return "\n".join(parts)

    def inject_for_file(self, file_path: str | Path) -> str:
        """Render style guidance relevant to *file_path* with precedence.

        Central (extension-matched) guides render first, then repo-local
        ``[DIR]_STYLE_GUIDE.toml`` guides ordered FARTHEST-to-NEAREST, under
        a wrapper that states the precedence rule. Consumers treat later
        ``<guide>`` blocks as overriding earlier equivalents ("implied
        repeal"); non-conflicting content from all layers stands.

        Args:
            file_path: The file being edited or rendered.

        Returns:
            XML block, or "" when nothing applies.
        """
        path = Path(file_path)
        guides = list(self.loader.match(path.suffix))
        local = [guide for _p, guide in reversed(self.loader.discover_local(path))]
        if not guides and not local:
            return ""

        parts = ['<style_guide precedence="nearest_local_overrides_central">']
        for guide in [*guides, *local]:
            parts.append(self._render_guide(guide, indent="  "))
        parts.append("</style_guide>")
        return "\n".join(parts)

    def _render_guide(self, guide: dict, indent: str = "  ") -> str:
        """Render one guide dict as an indented ``<guide>`` XML block.

        Args:
            guide: Parsed style-guide mapping.
            indent: Base indentation for the ``<guide>`` element.

        Returns:
            Multi-line XML fragment (no trailing newline).
        """
        pad = " " * len(indent)
        name = guide.get("meta", {}).get("name", "unnamed")
        parts = [f'{indent}<guide name="{xml_escape(str(name))}">']

        if "formatting" in guide:
            parts.append(f"{pad}  <formatting>")
            for key, value in guide["formatting"].items():
                parts.append(f"{pad}    <{key}>{xml_escape(str(value))}</{key}>")
            parts.append(f"{pad}  </formatting>")

        if "architecture" in guide:
            patterns = guide["architecture"].get("patterns", [])
            if patterns:
                parts.append(f"{pad}  <architecture>")
                for p in patterns:
                    parts.append(f"{pad}    <pattern>{xml_escape(str(p))}</pattern>")
                parts.append(f"{pad}  </architecture>")

        if "anti_patterns" in guide:
            forbidden = guide["anti_patterns"].get("forbidden", [])
            if forbidden:
                parts.append(f"{pad}  <anti_patterns>")
                for f in forbidden:
                    parts.append(f"{pad}    <forbidden>{xml_escape(str(f))}</forbidden>")
                parts.append(f"{pad}  </anti_patterns>")

        parts.append(f"{indent}</guide>")
        return "\n".join(parts)
