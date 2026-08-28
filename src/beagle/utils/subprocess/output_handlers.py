"""Streaming output handling, early termination, and truncation.

Decomposed from utils/subprocess_pool.py (2026-06-03).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal

from beagle.config.config import get_config

logger = logging.getLogger("Beagle.utils.subprocess.output_handlers")

TRUNCATION_HEADER_LINES = 10  # First N lines to show
TRUNCATION_FOOTER_LINES = 10  # Last N lines to show
_STREAMING_FINAL_ANSWER_PATTERN = re.compile(r"</final_answer\s*>")
_MAX_EARLY_DRAIN_SECONDS = 0.5  # Max total time to drain trailing lines after early termination

# v13.16: C0 control characters (except \t, \n, \r) and ANSI escape sequences
# that can corrupt <final_answer>/CDATA framing (GB-4 failure B).
_C0_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][0-9;]*[A-Za-z]|\x1b[()][AB012]|\x9b[0-9;]*[A-Za-z]"
)


def _terminate_process_group(process: object, sig: int) -> None:
    """Signal a spawned child's whole process group, defensively.

    The child is launched with ``start_new_session=True`` so it is a group
    leader; ``os.killpg(getpgid(pid), sig)`` reaps orphaned goose grandchildren
    too. Every precondition is guarded so this can NEVER signal the wrong group:

    - ``pid`` must be a real positive int (> 1). A non-int (e.g. a unittest
      Mock whose ``__index__`` returns 1) or a pid of 0/1 is rejected. Passing
      a Mock here previously resolved to ``os.killpg(getpgid(1), SIGKILL)``
      which, running as root, killed the operator's own login session.
    - the resolved pgid must not be the init group (<= 1) or our OWN group
      (e.g. if ``start_new_session`` ever failed and the child shares it).
    """
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return
    try:
        pgid = os.getpgid(pid)
        own_pgid = os.getpgid(0)
    except (OSError, ProcessLookupError):
        return
    if pgid <= 1 or pgid == own_pgid:
        return
    with contextlib.suppress(OSError, ProcessLookupError, RuntimeError):
        os.killpg(pgid, sig)


def _sanitize_output(text: str) -> str:
    """Strip C0 controls and ANSI escapes from subprocess output.

    These can corrupt <final_answer>/CDATA framing and produce garbled
    output like ``<^[[B![CDATA[`` (GB-4 Failure A, orig log line 481).
    Called on every decoded line in the streaming read path BEFORE
    final_answer detection or text assembly.
    """
    text = _C0_CONTROL_RE.sub("", text)
    text = _ANSI_ESCAPE_RE.sub("", text)
    return text


def truncate_large_output(
    output: str,
    header_lines: int = TRUNCATION_HEADER_LINES,
    footer_lines: int = TRUNCATION_FOOTER_LINES,
) -> str:
    """Truncate large outputs with header/footer pattern.

    H-MEM v13: Implements KV Cache Efficiency Optimization.
    For outputs >10,000 tokens, show first 10 lines and last 10 lines
    with a truncation marker in the middle.

    Args:
        output: Output string to potentially truncate
        header_lines: Number of lines to show at start
        footer_lines: Number of lines to show at end

    Returns:
        Truncated output with header/footer pattern if large,
        otherwise original output

    """
    truncation_threshold = get_config().output.truncation_threshold
    if len(output) <= truncation_threshold:
        return output

    lines = output.splitlines()
    total_lines = len(lines)

    # Don't truncate if already short enough
    if total_lines <= header_lines + footer_lines:
        return output

    # Extract header and footer
    header = "\n".join(lines[:header_lines])
    footer = "\n".join(lines[-footer_lines:])

    # Calculate hidden lines
    hidden_lines = total_lines - header_lines - footer_lines

    # Build truncated output
    truncated = f"""{header}

... [TRUNCATED: {hidden_lines} lines hidden - {len(output) // 4} tokens]
... Use VFS archive to retrieve full output if needed ...

{footer}"""

    logger.info(
        f"Truncated large output: {total_lines} lines -> {header_lines + footer_lines} lines "
        f"({len(output) // 4} tokens -> {len(truncated) // 4} tokens)"
    )

    return truncated


# --- Streaming read helpers ---


async def _drain_remaining(stdout: asyncio.StreamReader) -> list[str]:
    """Read remaining available lines from stdout until EOF."""
    result: list[str] = []
    while True:
        line = await stdout.readline()
        if not line:
            break
        result.append(line.decode("utf-8", errors="replace"))
    return result


async def _streaming_read(
    process: asyncio.subprocess.Process,
    prompt: str,
    timeout: int,
    node_name: str,
) -> tuple[bytes, bytes]:
    """Read subprocess output with early termination on </final_answer>.

    Returns (stdout_bytes, stderr_bytes) — same as process.communicate().
    If </final_answer> is detected in stdout, terminates early.
    """
    # Write prompt to stdin and close it so goose can start processing.
    # Without this, the subprocess blocks forever waiting for stdin.
    stdin = process.stdin
    stdout = process.stdout
    stderr = process.stderr
    if stdin is None or stdout is None or stderr is None:
        raise RuntimeError(f"[{node_name}] subprocess streams not initialized")
    try:
        stdin.write(prompt.encode("utf-8"))
        await stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError) as exc:
        logger.warning(f"[{node_name}] Failed to write prompt to stdin: {exc}")
        raise
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            stdin.close()

    final_answer_detected = asyncio.Event()
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    async def _read_stdout() -> None:
        while True:
            line = await stdout.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace")
            decoded = _sanitize_output(decoded)
            stdout_lines.append(decoded)
            if _STREAMING_FINAL_ANSWER_PATTERN.search(decoded):
                final_answer_detected.set()
                # Single bounded drain for trailing context (0.5s total, not 50 x 0.1s)
                try:
                    trailing = await asyncio.wait_for(
                        _drain_remaining(stdout), timeout=_MAX_EARLY_DRAIN_SECONDS
                    )
                    stdout_lines.extend(trailing)
                except TimeoutError:
                    logger.warning(
                        "Trailing stdout drain did not finish within %ss; the captured "
                        "output may be missing its final lines.",
                        _MAX_EARLY_DRAIN_SECONDS,
                    )
                return

    async def _read_stderr() -> None:
        while True:
            line = await stderr.readline()
            if not line:
                break
            stderr_lines.append(line.decode("utf-8", errors="replace"))

    stdout_task = asyncio.create_task(_read_stdout())
    stderr_task = asyncio.create_task(_read_stderr())

    # Wait for either early termination or process exit
    process_done = asyncio.create_task(process.wait())
    _done, _pending = await asyncio.wait(
        [process_done, asyncio.create_task(final_answer_detected.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # If early-terminated, give process a moment then kill
    if final_answer_detected.is_set():
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except TimeoutError:
            # Signal the whole process group (start_new_session=True child) so
            # orphaned goose grandchildren are reaped too. Restores the Phase 4.2
            # process-group teardown dropped during the Phase 5 decomposition.
            _terminate_process_group(process, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=3.0)
            except TimeoutError:
                _terminate_process_group(process, signal.SIGKILL)
                with contextlib.suppress(OSError, RuntimeError):
                    await process.wait()
        logger.debug(f"[{node_name}] Early-terminated after </final_answer>")
    else:
        # Normal completion
        await asyncio.gather(stdout_task, stderr_task)

    # Clean up tasks
    for t in [stdout_task, stderr_task]:
        t.cancel()
    with contextlib.suppress(OSError, RuntimeError):
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

    # v13.15.1: tolerant fallback. If the process exited without emitting
    # </final_answer> but stdout contains an *opening* <final_answer> tag,
    # treat whatever follows as the answer and synthesise a closure. This is
    # a defensive backstop for models that occasionally drop the closing tag;
    # the prompt-level CRITICAL OUTPUT CONTRACT should make this rare.
    stdout_text = "".join(stdout_lines)
    if "<final_answer>" in stdout_text and "</final_answer>" not in stdout_text:
        logger.warning(
            f"[{node_name}] Subprocess exited without </final_answer>; "
            "synthesising closure from <final_answer> position to EOF."
        )
        stdout_text = stdout_text + "\n</final_answer>"
        stdout_lines = [stdout_text]
    stdout_bytes = "".join(stdout_lines).encode("utf-8", errors="replace")
    stderr_bytes = "".join(stderr_lines).encode("utf-8", errors="replace")
    return stdout_bytes, stderr_bytes
