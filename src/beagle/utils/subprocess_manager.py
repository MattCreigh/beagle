"""Bounded Async Subprocess Manager — replaces heavy Goose CLI subprocesses.

Task 1 — Edge-Hardware Optimization:
- ``asyncio.Semaphore(8)`` for concurrency-limiting external OS binaries
- 5 MB hard limit on stdout/stderr buffers to prevent memory exhaustion
- Wraps ``asyncio.create_subprocess_exec`` with GIL-safe, bounded I/O

Usage (in-process ReAct loop):
    mgr = SubprocessManager(max_concurrency=8, buffer_limit_mb=5)
    result = await mgr.run("git", "status", cwd="/path/to/repo")
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from dataclasses import dataclass

logger = logging.getLogger("Beagle.utils.subprocess_manager")

_DEFAULT_BUFFER_LIMIT_BYTES = 5 * 1024 * 1024  # 5 MB


@dataclass
class SubprocessResult:
    """Structured result from a bounded subprocess execution."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class SubprocessManager:
    """Semaphore-bounded async subprocess executor for GIL-safe OS binary calls.

    Enforces:
    - Maximum concurrent subprocesses via ``asyncio.Semaphore``
    - Per-stream memory cap (default 5 MB) to prevent OOM
    - ``start_new_session=True`` so signals don't propagate unexpectedly
    """

    def __init__(
        self,
        max_concurrency: int = 8,
        buffer_limit_mb: float = 5.0,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._buffer_limit = int(buffer_limit_mb * 1024 * 1024)
        self._max_concurrency = max_concurrency

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    async def run(
        self,
        *args: str,
        cwd: str | None = None,
        timeout: float = 60.0,
        env: dict[str, str] | None = None,
    ) -> SubprocessResult:
        """Execute an OS binary with bounded concurrency and memory.

        Args:
            *args: Command and arguments (e.g., ``"git", "status"``).
            cwd: Working directory. ``None`` = inherit.
            timeout: Seconds before SIGTERM (then SIGKILL after 3 s grace).
            env: Environment dict. ``None`` = inherit current process env.

        Returns:
            ``SubprocessResult`` with return code, stdout, stderr, and
            truncation flags.

        """
        if not args:
            raise ValueError("At least one command argument is required")

        async with self._semaphore:
            logger.debug(
                "SubprocessManager: running %s (cwd=%s, timeout=%.1f s)",
                args,
                cwd or ".",
                timeout,
            )

            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env=env,
            )

            stdout_bytes = b""
            stderr_bytes = b""
            stdout_truncated = False
            stderr_truncated = False

            async def _read_stream(
                stream: asyncio.StreamReader | None,
                label: str,
            ) -> tuple[bytes, bool]:
                """Read from *stream* enforcing the buffer limit."""
                if stream is None:
                    return b"", False
                data = bytearray()
                truncated = False
                while True:
                    chunk = await stream.read(65536)  # 64 KiB chunks
                    if not chunk:
                        break
                    if len(data) + len(chunk) > self._buffer_limit:
                        remaining = self._buffer_limit - len(data)
                        if remaining > 0:
                            data.extend(chunk[:remaining])
                        truncated = True
                        # Drain remaining bytes to avoid broken pipe
                        while True:
                            drain = await stream.read(65536)
                            if not drain:
                                break
                        break
                    data.extend(chunk)
                return bytes(data), truncated

            try:
                stdout_task = asyncio.create_task(_read_stream(process.stdout, "stdout"))
                stderr_task = asyncio.create_task(_read_stream(process.stderr, "stderr"))
                process_wait = asyncio.create_task(process.wait())

                done, pending = await asyncio.wait(
                    [stdout_task, stderr_task, process_wait],
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=timeout,
                )

                timed_out = False

                if process_wait in done and process_wait.exception() is None:
                    # Process exited naturally — gather remaining streams
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(stdout_task, stderr_task),
                            timeout=5.0,
                        )
                    except TimeoutError:
                        logger.warning("Stream drain timed out after process exit")

                elif done:
                    # Stream read completed first — wait for process
                    try:
                        await asyncio.wait_for(process_wait, timeout=5.0)
                    except TimeoutError:
                        logger.warning("Process didn't exit after stream completion")

                else:
                    # Timeout — all tasks still pending
                    timed_out = True
                    logger.warning(
                        "SubprocessManager: %s timed out after %.1f s — terminating",
                        args,
                        timeout,
                    )
                    for task in pending:
                        task.cancel()
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                    try:
                        await asyncio.wait_for(process.wait(), timeout=3.0)
                    except TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                        await process.wait()

                stdout_bytes, stdout_truncated = stdout_task.result()
                stderr_bytes, stderr_truncated = stderr_task.result()

            except TimeoutError:
                timed_out = True
                logger.warning("SubprocessManager: %s timed out — killing", args)
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=3.0)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    await process.wait()
            finally:
                # Ensure process is cleaned up
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    await process.wait()

            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")

            if stdout_truncated:
                logger.warning(
                    "SubprocessManager stdout truncated at %d MB for %s",
                    self._buffer_limit // (1024 * 1024),
                    args,
                )
            if stderr_truncated:
                logger.warning(
                    "SubprocessManager stderr truncated at %d MB for %s",
                    self._buffer_limit // (1024 * 1024),
                    args,
                )

            return SubprocessResult(
                returncode=process.returncode or -1,
                stdout=stdout_text,
                stderr=stderr_text,
                timed_out=timed_out,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
            )


# ── Global singleton ──────────────────────────────────────────────────────────

_subprocess_manager: SubprocessManager | None = None
_subprocess_manager_lock = asyncio.Lock()


async def get_subprocess_manager(
    max_concurrency: int = 8,
    buffer_limit_mb: float = 5.0,
) -> SubprocessManager:
    """Get or create the global SubprocessManager singleton."""
    global _subprocess_manager
    if _subprocess_manager is not None:
        return _subprocess_manager
    async with _subprocess_manager_lock:
        if _subprocess_manager is None:
            _subprocess_manager = SubprocessManager(
                max_concurrency=max_concurrency,
                buffer_limit_mb=buffer_limit_mb,
            )
        return _subprocess_manager
