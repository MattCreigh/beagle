"""BlockRegistry singleton for discovering and registering blocks."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .errors import BlockNotFoundError

logger = logging.getLogger("Beagle.blocks.registry")


@runtime_checkable
class PythonBlock(Protocol):
    """Protocol for Python-implemented blocks."""

    __block_name__: str

    def __call__(self, context: Any, **kwargs: Any) -> Any: ...


class BlockRegistry:
    """Registry for all block implementations.

    Discovers Python blocks and XML blocks at initialization.
    """

    _instance: BlockRegistry | None = None

    def __init__(self) -> None:
        self._python: dict[str, PythonBlock] = {}
        self._xml: dict[str, Path] = {}

    @classmethod
    def instance(cls) -> BlockRegistry:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register_python(self, name: str, block: PythonBlock) -> None:
        """Register a Python block."""
        self._python[name] = block
        logger.debug(f"Registered Python block: {name}")

    def register_xml(self, name: str, path: Path) -> None:
        """Register an XML block by file path."""
        self._xml[name] = path
        logger.debug(f"Registered XML block: {name} -> {path}")

    def get_python(self, name: str) -> PythonBlock:
        """Lookup a Python block by name."""
        if name not in self._python:
            raise BlockNotFoundError(f"Python block '{name}' not found", block_name=name)
        return self._python[name]

    def get_xml(self, name: str) -> Path:
        """Lookup an XML block path by name."""
        if name not in self._xml:
            raise BlockNotFoundError(f"XML block '{name}' not found", block_name=name)
        return self._xml[name]

    def has_block(self, name: str) -> bool:
        """Check if any block type is registered under name."""
        return name in self._python or name in self._xml

    def list_python(self) -> list[str]:
        """List registered Python block names."""
        return list(self._python)

    def list_xml(self) -> list[str]:
        """List registered XML block names."""
        return list(self._xml)

    def discover(self, package: str | None = None) -> int:
        """Auto-discover blocks in the python_blocks package.

        Returns number of blocks discovered.
        """

        count = 0
        if package is None:
            package = "beagle.blocks.python_blocks"
        try:
            mod = importlib.import_module(package)
            for name in dir(mod):
                obj = getattr(mod, name)
                if callable(obj) and hasattr(obj, "__block_name__"):
                    self.register_python(obj.__block_name__, obj)
                    count += 1
        except ImportError:
            logger.warning(f"Could not import {package} for block discovery")
        return count

    def discover_xml(self, stdlib_dir: Path | None = None) -> int:
        """Auto-discover XML blocks in stdlib directory.

        Returns number of blocks discovered.
        """
        if stdlib_dir is None:
            stdlib_dir = Path(__file__).parent / "xml_blocks" / "stdlib"
        count = 0
        if stdlib_dir.exists():
            for xml_file in sorted(stdlib_dir.glob("*.xml")):
                name = xml_file.stem
                self.register_xml(name, xml_file)
                count += 1
        return count
