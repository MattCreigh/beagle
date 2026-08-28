"""Load and match TOML style guides by file extension."""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

# v1.1.1 (S6): style guides moved to the canonical config root; resolve them
# through find_guides_dir().
from ..config._config_path import find_guides_dir

logger = logging.getLogger("Beagle.style_guides.loader")

_GUIDES_DIR = find_guides_dir()


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge *overlay* onto *base*, returning a new dict.

    Leaf values in ``overlay`` replace equivalents in ``base`` ("implied
    repeal" — the more proximate, precise source wins); keys present only in
    ``base`` pass through untouched.

    Args:
        base: Lower-precedence mapping (e.g. a central guide).
        overlay: Higher-precedence mapping (e.g. a repo-local guide).

    Returns:
        A new merged dict; neither input is mutated.
    """
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class StyleGuideLoader:
    """Load TOML style guides and match them to file extensions."""

    def __init__(self, guides_dir: Path | None = None) -> None:
        self.guides_dir = guides_dir or _GUIDES_DIR
        self._cache: dict[str, dict] = {}
        self._load_all()

    def _load_all(self) -> None:
        """Load all .toml files from the guides directory."""
        if not self.guides_dir.exists():
            logger.warning("Style guides directory not found: %s", self.guides_dir)
            return
        for path in sorted(self.guides_dir.glob("*.toml")):
            try:
                with open(path, "rb") as f:
                    data = tomllib.load(f)
                name = data.get("meta", {}).get("name", path.stem)
                self._cache[name] = data
                logger.debug("Loaded style guide: %s", name)
            except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional — skip bad guides
                logger.warning("Failed to load style guide %s: %s", path.name, e)

    def match(self, file_extension: str) -> list[dict]:
        """Return all style guides that apply to the given file extension."""
        matches = []
        for _name, guide in self._cache.items():
            applies_to = guide.get("meta", {}).get("applies_to", [])
            if file_extension in applies_to or "*" in applies_to:
                matches.append(guide)
        return matches

    def discover_local(self, target: Path) -> list[tuple[Path, dict]]:
        """Find repo-local ``[DIR]_STYLE_GUIDE.toml`` files above *target*.

        Walks from ``target.parent`` toward the filesystem root. A directory
        named ``foo`` contributes ``foo/FOO_STYLE_GUIDE.toml`` when present.
        Results are ordered NEAREST first so callers folding them in sequence
        give the most proximate guide the final word (implied-repeal
        precedence over farther-local and central guides).

        Args:
            target: File whose location anchors the upward search.

        Returns:
            ``(path, parsed_toml)`` pairs, nearest ancestor first.
        """
        found: list[tuple[Path, dict]] = []
        current = target.resolve().parent
        for _ in range(16):  # bounded walk; guards against odd mounts/loops
            candidate = current / f"{current.name.upper()}_STYLE_GUIDE.toml"
            if candidate.is_file():
                try:
                    with open(candidate, "rb") as handle:
                        found.append((candidate, tomllib.load(handle)))
                except (OSError, tomllib.TOMLDecodeError) as exc:
                    logger.warning("Bad local style guide %s: %s", candidate, exc)
            if current == current.parent:
                break
            current = current.parent
        return found

    def merged_dict(self, target: Path, extension: str | None = None) -> dict:
        """Effective style rules for *target* flattened into ONE dict.

        Precedence ("parliamentary implied repeal"), weakest binds first:

        1. Central guides whose ``applies_to`` matches the extension.
        2. Repo-local guides from :meth:`discover_local`, folded FARTHEST
           first — the nearest directory's guide is applied LAST and
           therefore overrides equivalent keys in every layer beneath it.

        Non-conflicting keys pass through from lower layers unchanged.

        Args:
            target: The file being edited/rendered.
            extension: Override the derived suffix (e.g. ``".py"``).

        Returns:
            The merged rule mapping. Empty central+local yields ``{}``.
        """
        ext = extension if extension is not None else target.suffix
        merged: dict = {}
        for guide in self.match(ext):
            merged = _deep_merge(merged, guide)
        for _path, guide in reversed(self.discover_local(target)):
            merged = _deep_merge(merged, guide)
        return merged

    def get(self, name: str) -> dict | None:
        """Get a specific style guide by name."""
        return self._cache.get(name)

    def get_by_stem(self, stem: str) -> dict | None:
        """Get a specific style guide by filename stem (e.g. 'python_backend').

        Looks up the guide whose source filename matches ``{stem}.toml`` in
        the guides directory. The loader caches by ``meta.name`` (display
        name, e.g. ``"Python Backend"``) but render-time callers pass the
        filename stem (e.g. ``"python_backend"``) — this method bridges the
        two. Returns ``None`` if no matching source file exists.

        The lookup is O(N) over the guides directory; for the canonical
        Beagle deployment with ≤16 guides, this is trivially fast.
        """
        if not self.guides_dir.exists():
            return None
        target = self.guides_dir / f"{stem}.toml"
        if not target.is_file():
            return None
        try:
            with open(target, "rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            return None
        return data

    @property
    def available(self) -> list[str]:
        """List available style guide names."""
        return list(self._cache.keys())
