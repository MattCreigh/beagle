"""The ``goose_cli`` sub-agent execution runtime.

This is the default :class:`~beagle.runtime.base.AgentRuntime`. It spawns
a ``goose`` subprocess for each sub-agent and drives it over stdio. It is
axis 2 only: it does not model the user-facing front end.

The skeleton satisfies the protocol and resolves the binary lazily at
spawn time, so importing this module never requires a ``goose`` binary to
be present. Moving the real orchestrator spawn sites onto this interface
happens in B1c / B1d.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from beagle.config.paths import resolve_goose_bin
from beagle.runtime.base import AgentHandle, AgentRuntime, AgentSpec, RuntimeHealth


def default_goose_binary() -> str:
    """Schema factory for the resolved goose binary (non-raising).

    Returns the configured goose binary path without raising when the
    binary is absent, so it is safe as a dataclass ``default_factory``.
    Callers that need the executability guarantee use
    :meth:`GooseCliRuntime.resolved_binary` instead.

    Returns:
        The resolved path string, possibly empty or a literal fallback.

    """
    return GooseCliRuntime().binary_path


class GooseCliRuntime(AgentRuntime):
    """Spawn and drive ``goose`` subprocesses.

    Attributes:
        name: Stable runtime name, ``goose_cli``.

    """

    name: str = "goose_cli"

    def __init__(self, binary_path: str | None = None) -> None:
        """Initialise the runtime.

        Args:
            binary_path: Override for the goose binary. Defaults to
                :func:`beagle.config.paths.resolve_goose_bin`.

        """
        self._binary_path = binary_path or resolve_goose_bin()

    @property
    def binary_path(self) -> str:
        """Return the configured goose binary path without validating it.

        Non-raising accessor for call sites (config factories, health
        checks, firewall) that need the resolved path but must not crash
        when the binary is absent — they defer the executability decision.

        Returns:
            The resolved path string, which may be empty or the literal
            fallback when no binary was found.

        """
        return self._binary_path

    def resolved_binary(self) -> str:
        """Return the binary path, raising if it is not executable.

        Public accessor for call sites that need the resolved goose binary
        path to build their own subprocess command. This is the only place
        the core modules resolve the binary; ``resolve_goose_bin`` itself
        stays inside the runtime package.

        Returns:
            The resolved goose binary path.

        Raises:
            RuntimeError: When the binary is empty, missing, or not
                executable.

        """
        import os
        from pathlib import Path

        if not self._binary_path:
            raise RuntimeError("goose binary not configured; set GOOSE_BIN or PATH")
        bin_path = Path(self._binary_path).resolve()
        if not bin_path.is_file() or not os.access(bin_path, os.X_OK):
            raise RuntimeError(
                f"Goose binary not found or lacks execution permissions at: {bin_path}"
            )
        return str(bin_path)

    async def spawn(self, spec: AgentSpec) -> AgentHandle:
        """Spawn a ``goose`` subprocess for the agent spec.

        Args:
            spec: The agent description to spawn.

        Returns:
            An :class:`AgentHandle` wrapping the subprocess.

        Raises:
            RuntimeError: When the goose binary is unavailable.

        """
        binary = self.resolved_binary()
        process = await self._start_process(binary, spec)
        return AgentHandle(
            agent_id=spec.name,
            runtime_name=self.name,
            process=process,
        )

    async def _start_process(self, binary: str, spec: AgentSpec) -> Any:
        """Start the underlying subprocess.

        Args:
            binary: Path to the goose binary.
            spec: The agent spec.

        Returns:
            The :class:`asyncio.subprocess.Process`.

        """
        import asyncio

        cmd = [
            binary,
            "run",
            "--provider",
            spec.environment.get("GOOSE_PROVIDER", "ollama_cloud"),
            "--model",
            spec.environment.get("GOOSE_MODEL") or spec.model or "minimax-m3:cloud",
            "--with-builtin",
            "developer",
            "-i",
            "-",
            "--system",
            spec.system_prompt,
            "-q",
        ]
        return await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**spec.environment},
        )

    async def send_message(self, handle: AgentHandle, message: str) -> str:
        """Send a message and return the full reply.

        Args:
            handle: Handle returned by :meth:`spawn`.
            message: The prompt payload.

        Returns:
            The complete reply text.

        """
        chunks = [chunk async for chunk in self.stream(handle, message)]
        return "".join(chunks)

    async def stream(self, handle: AgentHandle, message: str) -> AsyncIterator[str]:
        """Stream the sub-agent's output for a message.

        Args:
            handle: Handle returned by :meth:`spawn`.
            message: The prompt payload.

        Yields:
            Incremental output text.

        """
        process = handle.process
        if process is None or process.stdout is None:
            raise RuntimeError("agent process is not running")
        process.stdin.write(message.encode())
        await process.stdin.drain()
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            decoded = line.decode(errors="replace").strip()
            if decoded:
                yield decoded

    async def terminate(self, handle: AgentHandle) -> None:
        """Terminate the sub-agent process.

        Args:
            handle: Handle returned by :meth:`spawn`.

        """
        process = handle.process
        if process is None:
            return
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await process.wait()

    async def health_check(self) -> RuntimeHealth:
        """Report whether the goose binary is usable.

        Returns:
            A :class:`RuntimeHealth` snapshot.

        """
        try:
            binary = self.resolved_binary()
        except RuntimeError as exc:
            return RuntimeHealth(healthy=False, detail=str(exc))
        return RuntimeHealth(healthy=True, detail="goose binary present", binary_path=binary)
