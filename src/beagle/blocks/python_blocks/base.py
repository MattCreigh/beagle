"""Base PythonBlock Protocol + @python_block decorator."""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..errors import ExecutionError


@dataclass(frozen=True)
class BlockMetadata:
    """Metadata attached to a Python block."""

    name: str
    description: str
    inputs: list[str] | None = None
    outputs: list[str] | None = None
    cost_weight: float = 1.0
    timeout_seconds: float = 30.0
    retry_count: int = 0


def python_block(
    name: str | None = None,
    description: str = "",
    inputs: list[str] | None = None,
    outputs: list[str] | None = None,
    cost_weight: float = 1.0,
    timeout_seconds: float = 30.0,
    retry_count: int = 0,
) -> Callable:
    """Decorator to register a function as a PythonBlock.

    Usage:
        @python_block(name="read_file", description="Read a file")
        def read_file(ctx, path: str) -> str:
            return Path(path).read_text()
    """

    def decorator(func: Callable) -> Callable:
        meta = BlockMetadata(
            name=name or func.__name__,
            description=description or func.__doc__ or "",
            inputs=inputs,
            outputs=outputs,
            cost_weight=cost_weight,
            timeout_seconds=timeout_seconds,
            retry_count=retry_count,
        )

        @functools.wraps(func)
        def wrapper(ctx: Any, **kwargs: Any) -> Any:
            start = time.monotonic()
            try:
                result = func(ctx, **kwargs)
                return {"success": True, "output": result, "duration": time.monotonic() - start}
            except Exception as exc:  # broad catch intentional
                raise ExecutionError(
                    f"Block '{meta.name}' failed: {exc}",
                    block_name=meta.name,
                    details={"duration": time.monotonic() - start},
                ) from exc

        wrapper.__block_name__ = meta.name  # type: ignore[attr-defined]
        wrapper.__block_meta__ = meta  # type: ignore[attr-defined]
        wrapper.__raw_func__ = func  # type: ignore[attr-defined]
        return wrapper

    return decorator
