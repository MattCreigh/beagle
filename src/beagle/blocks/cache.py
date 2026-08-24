"""AgentCache — deterministic caching for block composition results.

Beagle v13.8.1 Phase 3: SHA-256(TOML + style guides + schema version),
store in .beagle/block_cache/ with 0o700/0o600 perms.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("Beagle.blocks.cache")


class AgentCache:
    """Filesystem-backed cache for block execution results.

    Usage:
        cache = AgentCache()
        cache.set("abc123", {"result": "ok"})
        data = cache.get("abc123")
    """

    DEFAULT_DIR = Path.home() / ".beagle" / "block_cache"

    def __init__(self, cache_dir: Path | str | None = None) -> None:
        self._dir = Path(cache_dir) if cache_dir else self.DEFAULT_DIR
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        """Create cache directory with 0o700 permissions."""
        self._dir.mkdir(parents=True, exist_ok=True)
        try:
            self._dir.chmod(0o700)
        except OSError as exc:
            logger.warning(f"Could not chmod {self._dir}: {exc}")

    @staticmethod
    def make_key(
        recipe_name: str,
        recipe_version: str,
        block_name: str,
        schema_version: str,
        style_guides: list[str],
        params: dict[str, Any],
    ) -> str:
        """Deterministic SHA-256 cache key."""
        hasher = hashlib.sha256()
        hasher.update(recipe_name.encode())
        hasher.update(recipe_version.encode())
        hasher.update(block_name.encode())
        hasher.update(schema_version.encode())
        for sg in sorted(style_guides):
            hasher.update(sg.encode())
        for k, v in sorted(params.items()):
            hasher.update(f"{k}={v}".encode())
        return hasher.hexdigest()

    def _path(self, key: str) -> Path:
        """Shard keys into subdirs by first 2 hex chars."""
        return self._dir / key[:2] / key

    def get(self, key: str) -> Any | None:
        """Retrieve cached value by key, or None if missing."""
        path = self._path(key)
        if not path.exists():
            return None
        try:
            raw = path.read_bytes()
            return json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"Cache read failed for {key}: {exc}")
            return None

    def set(self, key: str, value: Any) -> Path:
        """Store value under key with 0o600 file permissions."""
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            with contextlib.suppress(OSError):
                path.chmod(0o600)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, default=str), encoding="utf-8")
        tmp.replace(path)
        path.chmod(0o600)
        logger.debug(f"Cached block result: {key}")
        return path

    def invalidate(self, key: str) -> bool:
        """Remove a single cached entry."""
        path = self._path(key)
        if path.exists():
            path.unlink()
            return True
        return False

    def clear(self) -> int:
        """Remove all cached entries.  Returns count removed."""
        count = 0
        for child in self._dir.rglob("*"):
            if child.is_file():
                child.unlink()
                count += 1
        return count

    def stats(self) -> dict[str, int]:
        """Return cache statistics: entries, total_bytes."""
        entries = 0
        total_bytes = 0
        for child in self._dir.rglob("*"):
            if child.is_file():
                entries += 1
                total_bytes += child.stat().st_size
        return {"entries": entries, "total_bytes": total_bytes}
