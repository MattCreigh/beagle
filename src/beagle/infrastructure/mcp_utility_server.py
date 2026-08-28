"""Beagle Utility MCP Server — consolidated Code Tools + Web Search + Workflow.

See docs/mcp_consolidation.md for the full consolidation rationale
(why these three are merged; why RAG and OpenClaw remain separate;
transport security model).

Tools: 14 (3 code, 3 web, 8 workflow). Resources: 3.
Transport: stdio (default) or streamable-http (BEAGLE_EXECUTION_ENV=docker).
The streamable-http path REQUIRES bearer-token authentication per
MCP_TRUST.md — set BEAGLE_MCP_TOKEN to a high-entropy secret. Refusal
to provide the token in HTTP mode is fail-closed (RuntimeError on
startup), not a silent downgrade.
"""

from __future__ import annotations

import ast
import asyncio
import collections
import contextvars
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from collections.abc import Callable
from typing import Any

# Project root for subprocess cwd and path resolution (not for sys.path).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

logger = logging.getLogger("Beagle.mcp.utility")

# Module-level cache for the `fd` availability probe (B-21/B-22)
_HAS_FD: bool | None = None

# ── FastMCP setup ────────────────────────────────────────────────────────────

try:
    from mcp.server.fastmcp import Context, FastMCP
except ImportError:
    # Stub for environments without mcp installed.
    # v1.0.2 (qa-gate): kw / progress / total / message parameters
    # renamed to underscore-prefixed form so vulture stops flagging
    # the stub methods' intentionally-ignored arguments. Stub no-ops
    # must accept the full signature (so callers don't AttributeError
    # in test environments without mcp), but they discard everything.
    # Type hints are deliberately omitted (the pre-existing stubs
    # were untyped too) so this block stays import-clean without
    # pulling in ``from typing import Any``.

    class FastMCP:  # type: ignore[no-redef]
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def tool(self, *_a: object, **_kw: object) -> Callable[[Callable], Callable]:
            def _d(f: Callable) -> Callable:
                return f

            return _d

        def resource(self, *_a: object, **_kw: object) -> Callable[[Callable], Callable]:
            def _d(f: Callable) -> Callable:
                return f

            return _d

        def run(self, **_kw: object) -> None:
            pass

    class Context:  # type: ignore[no-redef]
        """Stub Context for environments without mcp installed."""

        async def report_progress(
            self,
            _progress: object,
            _total: object = None,
            _message: object = None,
        ) -> None:
            return None

        async def info(self, _message: object) -> None:
            return None

        async def warning(self, _message: object) -> None:
            return None

        async def error(self, _message: object) -> None:
            return None

        async def debug(self, _message: object) -> None:
            return None


mcp = FastMCP(
    "Beagle",
    instructions=(
        "Beagle Utility Server — consolidated Code Tools, Web Search, "
        "and Workflow orchestration tools."
    ),
)

# ── Rate limiting (same pattern as mcp_rag_server.py) ────────────────────────
# v13.20.3 (R3.1): `list[float]` → `collections.deque(maxlen=N)` so the
# sliding-window eviction in `_check_mcp_rate_limit` is O(1) per element
# (was O(n) via `list.pop(0)`). maxlen bounds the memory ceiling
# deterministically to the rate-limit window; old entries are evicted
# automatically on `append`, removing the need for the prior
# `while ... pop(0)` drain loop. The drain is kept as a safety net for
# the case where `now - head > WINDOW` is true at startup or after a
# long idle (audit C10 / R3.1).
#
# Note: the deque is initialised AFTER _MCP_RATE_LIMIT_MAX_CALLS so the
# maxlen value is available at evaluation time.

_MCP_RATE_LIMIT_WINDOW = 60.0
_MCP_RATE_LIMIT_MAX_CALLS = 120

_mcp_call_timestamps: collections.deque[float] = collections.deque(maxlen=_MCP_RATE_LIMIT_MAX_CALLS)


def _check_mcp_rate_limit() -> None:
    """Check rate limit for MCP tool calls. Raises RuntimeError if exceeded."""
    now = time.time()
    # Drain expired entries (the deque's maxlen handles the steady-state
    # eviction on append; this loop is the cold-start / long-idle case).
    while _mcp_call_timestamps and now - _mcp_call_timestamps[0] > _MCP_RATE_LIMIT_WINDOW:
        _mcp_call_timestamps.popleft()
    if len(_mcp_call_timestamps) >= _MCP_RATE_LIMIT_MAX_CALLS:
        raise RuntimeError(
            f"MCP rate limit exceeded: {_MCP_RATE_LIMIT_MAX_CALLS} "
            f"calls per {_MCP_RATE_LIMIT_WINDOW}s window"
        )
    _mcp_call_timestamps.append(now)


# ── Correlation ID (threading/async tracing) ─────────────────────────────────

_correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)


class CorrelationIdFilter(logging.Filter):
    """Inject correlation_id into log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = _correlation_id_var.get("")
        return True


def set_correlation_id() -> str:
    """Create and store a correlation ID; return it."""
    # Beagle doctrine: full uuid.uuid4(); never truncated (122-bit entropy required).
    # The 12-hex (48-bit) truncation collided on high-traffic MCP sessions; the
    # correlation_id is the only thread tying log lines across the stdio boundary.
    cid = str(uuid.uuid4())
    _correlation_id_var.set(cid)
    return cid


def get_correlation_id() -> str:
    """Return the current correlation ID (or empty string)."""
    return _correlation_id_var.get("")


# ── Workflow dependency imports ──────────────────────────────────────────────

from beagle.config.config import get_config  # ruff: ignore[E402]
from beagle.core.graph import run_workflow  # ruff: ignore[E402]
from beagle.core.router import (  # ruff: ignore[E402]
    list_routable_workflows,
    route_query,
)
from beagle.core.workflow_loader import (  # ruff: ignore[E402]
    list_workflows,
    validate_workflow,
)
from beagle.cost_tracker import (  # ruff: ignore[E402]
    _get_pricing,
    estimate_tokens_agnostic,
)
from beagle.infrastructure.mcp_common import (  # ruff: ignore[E402]
    _metrics,
    get_metrics_summary,
    record_metric,
)
from beagle.security import (  # ruff: ignore[E402]
    scrub_secrets,
    validate_file_path,
    validate_query,
)
from beagle.utils.env_manager import get_workspace_root  # ruff: ignore[E402]

# ══════════════════════════════════════════════════════════════════════════════
# Code Tools — file_discovery, code_context
# Migrated from mcp_code_tools_server.py
# ══════════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def file_discovery(
    pattern: str = "",
    path: str = ".",
    file_type: str = "file",
    extension: str = "",
    max_depth: int = 5,
    max_results: int = 100,
) -> str:
    """Discover files matching criteria with structured results.

    Args:
        pattern: Optional pattern in filename.
        path: Root directory to start search.
        file_type: "file", "directory", or "symlink".
        extension: Filter by extension (e.g. .py).
        max_depth: Max directory depth.
        max_results: Max number of files to return.

    """
    _check_mcp_rate_limit()

    is_valid, error = validate_file_path(str(path))
    if not is_valid:
        return json.dumps({"status": "error", "message": f"Invalid path: {error}"})

    allowed_types = {"file", "directory", "symlink"}
    if file_type not in allowed_types:
        return json.dumps(
            {
                "status": "error",
                "message": f"Invalid file_type: {file_type!r}. Allowed: {sorted(allowed_types)}",
            }
        )

    # Prefer 'fd' if available, else 'find'. Cache the probe at module scope
    # so every find_files call does not spawn a subprocess.
    # v1.0.2 (qa-gate): wrap the blocking subprocess.run in asyncio.to_thread
    # (ASYNC221) and resolve 'fd' / 'find' via shutil.which to a full path
    # (S607 — partial executable path is a security smell). If the binary
    # is not on PATH, fall through to the find-fallback branch.
    global _HAS_FD
    fd_path: str | None = None
    if _HAS_FD is None:
        fd_path = shutil.which("fd")
        if fd_path is not None:
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    [fd_path, "--version"],
                    capture_output=True,
                    check=False,
                    timeout=5,
                )
                _HAS_FD = True
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                _HAS_FD = False
        else:
            _HAS_FD = False

    if _HAS_FD:
        # fd type mapping: f=file, d=directory, l=symlink
        # v1.0.2: use the resolved fd_path (full executable) instead of
        # the bare 'fd' string so S607 stays quiet. fd_path is set by
        # the probe above or by a previous cached call.
        if fd_path is None:
            fd_path = shutil.which("fd") or "fd"  # best-effort fallback
        fd_type = {"file": "f", "directory": "d", "symlink": "l"}[file_type]
        cmd = [fd_path, "--max-depth", str(max_depth), "--type", fd_type]
        if extension:
            cmd.extend(["-e", extension.lstrip(".")])
        if pattern:
            cmd.append(pattern)
        cmd.append(path)
    else:
        # find fallback — also resolve to a full path for S607.
        find_path = shutil.which("find") or "find"
        cmd = [find_path, path, "-maxdepth", str(max_depth)]
        if file_type == "file":
            cmd.extend(["-type", "f"])
        elif file_type == "directory":
            cmd.extend(["-type", "d"])
        elif file_type == "symlink":
            cmd.extend(["-type", "l"])
        if extension:
            cmd.extend(["-name", f"*{extension}"])
        if pattern:
            cmd.extend(["-name", f"*{pattern}*"])

    try:
        process = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            cwd=str(_PROJECT_ROOT),
            timeout=max(30, max_depth * 10),
        )
        files = []
        lines = process.stdout.splitlines()

        for line in lines[:max_results]:
            fpath = Path(line)
            full_path = _PROJECT_ROOT / fpath
            if full_path.exists():
                stats = full_path.stat()
                files.append(
                    {
                        "path": str(fpath),
                        "size_bytes": stats.st_size,
                        "modified": stats.st_mtime,
                        "type": "directory" if full_path.is_dir() else "file",
                    }
                )

        return json.dumps(
            {
                "status": "ok",
                "files": files,
                "total_found": len(lines),
                "truncated": len(lines) > max_results,
            }
        )
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as e:
        logger.error(f"file_discovery failed: {e}", exc_info=True)
        error_msg = scrub_secrets(str(e))[:500]
        return json.dumps({"status": "error", "message": f"Discovery failed: {error_msg}"})


@mcp.tool()
async def code_context(
    file_path: str,
    query_type: str = "overview",
    symbol: str = "",
) -> str:
    """Get structured code context for a file or symbol using AST.

    Args:
        file_path: Path to the source file.
        query_type: "overview", "function", "class", "imports".
        symbol: Optional name of function/class to extract.

    """
    _check_mcp_rate_limit()

    is_valid, error = validate_file_path(str(file_path))
    if not is_valid:
        return json.dumps({"status": "error", "message": f"Invalid path: {error}"})

    full_path = _PROJECT_ROOT / file_path
    if not full_path.exists() or not full_path.is_file():
        return json.dumps({"status": "error", "message": "File not found"})

    if not file_path.endswith(".py"):
        # Minimal fallback for non-python
        return json.dumps({"status": "ok", "content": full_path.read_text()[:5000]})

    try:
        tree = ast.parse(full_path.read_text())

        if query_type == "overview":
            defs = []
            for node in tree.body:
                if isinstance(node, ast.FunctionDef):
                    defs.append({"type": "function", "name": node.name, "line": node.lineno})
                elif isinstance(node, ast.ClassDef):
                    defs.append({"type": "class", "name": node.name, "line": node.lineno})
            return json.dumps({"status": "ok", "definitions": defs})

        if query_type == "imports":
            imports = []
            for node in ast.walk(tree):  # type: ignore[assignment]
                if isinstance(node, ast.Import):
                    for n in node.names:
                        imports.append(n.name)
                elif isinstance(node, ast.ImportFrom):
                    imports.append(f"{node.module}.*")
            return json.dumps({"status": "ok", "imports": imports})

        if query_type in ("function", "class") and symbol:
            for node in ast.walk(tree):  # type: ignore[assignment]
                if isinstance(node, ast.FunctionDef | ast.ClassDef) and node.name == symbol:
                    return json.dumps(
                        {
                            "status": "ok",
                            "name": node.name,
                            "content": ast.get_source_segment(full_path.read_text(), node),
                        }
                    )
            return json.dumps({"status": "error", "message": f"Symbol '{symbol}' not found"})

        return json.dumps({"status": "error", "message": "Invalid query type or missing symbol"})

    except (OSError, ValueError, RuntimeError, SyntaxError) as e:
        logger.error(f"code_context failed: {e}", exc_info=True)
        error_msg = scrub_secrets(str(e))[:500]
        return json.dumps({"status": "error", "message": f"Context extraction failed: {error_msg}"})


@mcp.tool()
async def list_available_workflows() -> str:
    """List all available Beagle workflows with their descriptions and phase counts.

    Returns a JSON array of workflow objects with name, description, and phase count.
    """
    _check_mcp_rate_limit()
    correlation_id = set_correlation_id()
    start_time = time.monotonic()
    try:
        workflows = list_workflows()
        result = json.dumps(workflows, indent=2)
        duration = time.monotonic() - start_time
        record_metric("list_available_workflows", duration, success=True)
        logger.debug(f"[{correlation_id}] list_available_workflows completed in {duration:.4f}s")
        return result
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as e:
        duration = time.monotonic() - start_time
        record_metric("list_available_workflows", duration, success=False)
        logger.exception(f"[{correlation_id}] list_available_workflows failed: {e}")
        raise


@mcp.tool()
async def route_query_to_workflow(query: str) -> str:
    """Analyze a query and recommend the best workflow to handle it.

    Uses keyword matching and optional LLM-based semantic classification
    to route queries to the most appropriate workflow.

    Args:
        query: The user's query or task description

    Returns:
        JSON with recommended workflow, confidence score, reasoning, and alternatives.

    """
    _check_mcp_rate_limit()
    correlation_id = set_correlation_id()
    start_time = time.monotonic()
    try:
        result = route_query(query)
        duration = time.monotonic() - start_time
        record_metric("route_query_to_workflow", duration, success=True)
        logger.debug(f"[{correlation_id}] route_query_to_workflow completed in {duration:.4f}s")
        return json.dumps(
            {
                "workflow": result.workflow,
                "confidence": result.confidence,
                "reasoning": result.reasoning,
                "alternatives": [
                    {"name": a[0], "score": round(a[1], 2)} for a in (result.alternatives or [])
                ],
            },
            indent=2,
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as e:
        duration = time.monotonic() - start_time
        record_metric("route_query_to_workflow", duration, success=False)
        logger.exception(f"[{correlation_id}] route_query_to_workflow failed: {e}")
        raise


@mcp.tool()
async def validate_workflow_file(workflow_name: str) -> str:
    """Validate a workflow YAML file without executing it.

    Checks for structural correctness: required fields, valid dependencies,
    no duplicate phases, no circular dependencies.

    Args:
        workflow_name: Workflow filename (e.g., 'research.yaml')

    Returns:
        JSON with validation status and any errors found.

    """
    _check_mcp_rate_limit()
    correlation_id = set_correlation_id()
    start_time = time.monotonic()
    try:
        errors = validate_workflow(workflow_name)
        duration = time.monotonic() - start_time
        record_metric("validate_workflow_file", duration, success=True)
        logger.debug(f"[{correlation_id}] validate_workflow_file completed in {duration:.4f}s")
        return json.dumps(
            {
                "valid": len(errors) == 0,
                "errors": errors,
            },
            indent=2,
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as e:
        duration = time.monotonic() - start_time
        record_metric("validate_workflow_file", duration, success=False)
        logger.exception(f"[{correlation_id}] validate_workflow_file failed: {e}")
        raise


@mcp.tool()
async def estimate_workflow_cost(query: str, workflow_name: str = "research") -> str:
    """Estimate the token usage and cost for running a workflow.

    Provides a rough estimate based on query length, number of phases,
    and typical agent token usage patterns.

    Args:
        query: The query to estimate for
        workflow_name: Name of the workflow to estimate

    Returns:
        JSON with estimated tokens, cost, and per-phase breakdown.

    """
    _check_mcp_rate_limit()
    correlation_id = set_correlation_id()
    start_time = time.monotonic()
    try:
        config = get_config()
        workflows = list_workflows()
        workflow_info = next((w for w in workflows if workflow_name in w["name"]), None)

        num_phases = workflow_info["phases"] if workflow_info else 4
        query_tokens = estimate_tokens_agnostic(query)

        # Rough estimates per phase
        avg_input_per_phase = query_tokens + 2000  # query + recipe + context
        avg_output_per_phase = 3000
        total_input = avg_input_per_phase * num_phases
        total_output = avg_output_per_phase * num_phases

        # Use default model pricing
        model = config.goose.default_model
        pricing = _get_pricing(model)
        estimated_cost = (total_input / 1_000_000) * pricing["input"] + (
            total_output / 1_000_000
        ) * pricing["output"]

        duration = time.monotonic() - start_time
        record_metric("estimate_workflow_cost", duration, success=True)
        logger.debug(f"[{correlation_id}] estimate_workflow_cost completed in {duration:.4f}s")

        return json.dumps(
            {
                "workflow": workflow_name,
                "model": model,
                "phases": num_phases,
                "estimated_input_tokens": total_input,
                "estimated_output_tokens": total_output,
                "estimated_total_tokens": total_input + total_output,
                "estimated_cost_usd": round(estimated_cost, 4),
                "note": "Rough estimate. Actual usage depends on agent output length.",
            },
            indent=2,
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as e:
        duration = time.monotonic() - start_time
        record_metric("estimate_workflow_cost", duration, success=False)
        logger.exception(f"[{correlation_id}] estimate_workflow_cost failed: {e}")
        raise


# ── EventBus → MCP progress bridge (v13.19.1) ────────────────────────────────
# Forwards Beagle's internal EventBus events to the calling MCP client (goose)
# as `notifications/progress` (Tier 1) and `notifications/message` (Tier 2)
# so the frontend sees workflow progression in real time. Purely additive
# over the existing request/response contract; no-op when no MCP session
# is attached (e.g., direct unit-test invocation).

import contextlib  # ruff: ignore[E402]

# Field names that suggest credentials. The bridge redacts these in
# log payloads to honour the v13.19.1 "Do NOT log secrets" rule.
_SECRET_FIELD_NAMES = frozenset(
    {"api_key", "token", "password", "bearer", "secret", "authorization"}
)

# Event types (the `event_type` string field) whose Tier 1 progress
# numerator should advance — i.e. terminal node lifecycle events.
_NODE_TERMINAL_TYPES = frozenset({"node.completed", "node.failed", "node.skipped"})

# Event-type → severity for the Tier 2 `notifications/message` call.
# Anything not listed is treated as "info". The MCP Context surface is
# ctx.{info,warning,error,debug}; we look the method up dynamically.
_EVENT_SEVERITY = {
    "budget.warning": "warning",
    "budget.exhausted": "error",
    "context.warning": "warning",
    "node.failed": "error",
    "validation.regression": "error",
    "tool.escalated": "warning",
    "health.degraded": "warning",
    "health.critical": "error",
}


class _EventBridge:
    """Forwards Beagle EventBus events to an MCP Context as progress / messages.

    Lifecycle:
        bridge = _EventBridge(ctx, total_nodes_hint=N)
        bridge.attach()           # subscribe on the bus
        try:
            ... run workflow ...
        finally:
            bridge.detach()       # ALWAYS, even on exceptions

    Tier 1 (progress): advances on node.completed / node.failed / node.skipped
    Tier 2 (messages): every event also fans out as a log line so the client
        gets a chronological transcript even without a progressToken.

    Notifications are best-effort — a flaky client transport must not break
    workflow execution. Every MCP call is wrapped in `contextlib.suppress`.
    """

    def __init__(
        self,
        ctx: Context | None,
        *,
        total_nodes_hint: int = 0,
        workflow_id: str = "",
    ) -> None:
        self._ctx = ctx
        self._total_nodes = max(0, int(total_nodes_hint))
        self._workflow_id = workflow_id
        self._completed = 0
        self._messages_emitted = 0
        self._progress_emitted = 0
        self._sub_id: str | None = None
        # Holds in-flight dispatch tasks so they aren't GC'd mid-flight
        # (RUF006). Each task removes itself from this set on completion.
        self._inflight: set[asyncio.Task] = set()
        # v13.21.13: Events created before attach() are ring-buffer replays
        # from a previous workflow — never forward them (see _on_event).
        self._attached_at = 0.0
        # v13.21.13: node.output line throttle. The subprocess pool now
        # publishes every stdout/stderr line; forwarding each as a JSON-RPC
        # notification would flood the client transport. One line per
        # interval is plenty — the heartbeat carries the latest line anyway.
        self._output_min_interval = float(
            os.environ.get("BEAGLE_BRIDGE_OUTPUT_MIN_INTERVAL", "0.5")
        )
        self._last_output_forward = 0.0

    # --- lifecycle --------------------------------------------------------

    def attach(self) -> None:
        """Subscribe to all events on the bus. No-op when ctx is None."""
        if self._ctx is None:
            return
        from beagle.events.bus import get_event_bus

        self._attached_at = time.time()
        self._sub_id = get_event_bus().subscribe("*", self._on_event)

    def detach(self) -> None:
        """Unsubscribe. Always safe to call (idempotent + suppress)."""
        if self._sub_id is None:
            return
        from beagle.events.bus import get_event_bus

        with contextlib.suppress(Exception):
            get_event_bus().unsubscribe(self._sub_id)
        self._sub_id = None

    # --- instrumentation (test seam) --------------------------------------

    @property
    def messages_emitted(self) -> int:
        return self._messages_emitted

    @property
    def progress_emitted(self) -> int:
        return self._progress_emitted

    @property
    def subscription_id(self) -> str | None:
        return self._sub_id

    # --- bus callback -----------------------------------------------------

    def _on_event(self, event: Any) -> None:
        """Bus callback. Schedules the async dispatch on the running loop.

        The bus may invoke us with either a sync or async event loop
        present. We always dispatch the work to a task so the bus's
        5-second per-callback timeout is not our bottleneck.
        """
        if self._ctx is None:
            return
        # v13.21.13: Replay gate. EventBus.subscribe() replays up to 1000
        # ring-buffered events to new subscribers; without this gate the
        # previous workflow's transcript would be re-forwarded at the start
        # of every run.
        ev_ts = getattr(event, "timestamp", None)
        if ev_ts is not None and self._attached_at and ev_ts < self._attached_at:
            return
        # Filter: only forward events for our workflow if a workflow_id
        # was supplied. This prevents cross-talk when concurrent workflows
        # are running.
        if self._workflow_id:
            ev_wf = getattr(event, "workflow_id", "")
            if ev_wf and ev_wf != self._workflow_id:
                return
        # v13.21.13: Throttle raw output lines (heartbeat is exempt — it is
        # already paced and carries the enriched status message).
        if getattr(event, "event_type", "") == "node.output" and (
            getattr(event, "node_name", "") != "_workflow_heartbeat"
        ):
            now = time.monotonic()
            if now - self._last_output_forward < self._output_min_interval:
                return
            self._last_output_forward = now
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop — should not happen in normal MCP path
        with contextlib.suppress(Exception):
            task = loop.create_task(self._dispatch(event))
            # Hold a reference so the task isn't GC'd before it runs.
            # RUF006: `create_task` returns a Task that must be retained.
            # Cleared in _dispatch() once the coroutine completes.
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

    async def _dispatch(self, event: Any) -> None:
        """Forward a single event to the MCP Context."""
        if self._ctx is None:
            return
        ev_type = getattr(event, "event_type", "") or type(event).__name__
        node_name = getattr(event, "node_name", "") or ""

        # Tier 1: progress numerator on node terminal events.
        # v13.21.13: Also fire on node.started (same numerator, new message)
        # so progress-token clients see each phase begin, not only end; and
        # report even without a denominator (total=None is valid MCP) so an
        # unknown phase count no longer silences Tier 1 entirely.
        if ev_type in _NODE_TERMINAL_TYPES or ev_type == "node.started":
            if ev_type in _NODE_TERMINAL_TYPES:
                self._completed += 1
            with contextlib.suppress(Exception):
                await self._ctx.report_progress(
                    progress=self._completed,
                    total=self._total_nodes if self._total_nodes > 0 else None,
                    message=f"{ev_type}: {node_name or '?'}",
                )
                self._progress_emitted += 1

        # Tier 2: log line. Always emit so the client gets a transcript
        # even without a progressToken.
        severity = _EVENT_SEVERITY.get(ev_type, "info")
        payload = self._summarize(event)
        msg = f"[Beagle] {ev_type} {payload}"
        method = getattr(self._ctx, severity, None) or self._ctx.info
        with contextlib.suppress(Exception):
            await method(msg)
            self._messages_emitted += 1

    @staticmethod
    def _summarize(event: Any) -> str:
        """Render a compact, secret-redacted payload for a log line.

        Capped at ~300 chars per event to avoid spamming the client.
        Recursively redacts any value whose field/key name suggests a
        credential — important because most events stash their
        parameters in a nested dict (e.g. ToolCallEvent.parameters).
        """
        fields: list[str] = []
        for f in (
            "node_name",
            "workflow_id",
            "stream_type",
            "tool_name",
            "model",
            "status",
            "tokens",
            "cost",
            "cost_usd",
            "current_cost",
            "total_tokens",
            "duration_seconds",
            "error",
            "details",
            "health_score",
            "utilization",
            "current_tokens",
            "max_tokens",
        ):
            v = getattr(event, f, None)
            if v is None:
                continue
            # Secret redaction by field name
            if f.lower() in _SECRET_FIELD_NAMES:
                v = "<redacted>"
            s = str(v)
            if len(s) > 80:
                s = s[:77] + "..."
            fields.append(f"{f}={s}")

        # Walk nested parameters dict, if present, and redact any
        # entry whose key matches a secret-name pattern.
        params = getattr(event, "parameters", None)
        if isinstance(params, dict) and params:
            rendered_items: list[str] = []
            for k, v in params.items():
                key_lower = str(k).lower()
                if key_lower in _SECRET_FIELD_NAMES or (
                    "key" in key_lower and "id" not in key_lower
                ):
                    rendered_items.append(f"{k}=<redacted>")
                else:
                    sv = str(v)
                    if len(sv) > 80:
                        sv = sv[:77] + "..."
                    rendered_items.append(f"{k}={sv}")
            # Cap to 5 entries to avoid log bloat
            if len(rendered_items) > 5:
                rendered_items = [*rendered_items[:5], "…(truncated)"]
            fields.append("parameters=" + " ".join(rendered_items))

        # v13.21.13: Content-bearing fields (NodeOutput.content,
        # NodeCompleted.result) ARE the granular signal — previously they
        # were dropped entirely, reducing every node.output line (including
        # the heartbeat's status message) to `node.output node_name=X`.
        # Larger cap than scalar fields; secret-scrubbed because this is
        # raw subprocess output.
        for f in ("result", "content"):
            v = getattr(event, f, None)
            if not v:
                continue
            s = scrub_secrets(str(v).strip())
            if len(s) > 200:
                s = s[:197] + "..."
            fields.append(f'{f}="{s}"')

        out = " ".join(fields)
        if len(out) > 500:
            out = out[:497] + "..."
        return out


def _count_workflow_phases_safe(workflow_name: str) -> int:
    """Best-effort: count phases (DAG nodes) for the progress denominator.

    Uses the cheap `list_workflows()` path (parses YAML front-matter only,
    no full DAG construction). Returns 0 if unavailable — the bridge
    will skip numeric progress in that case but still emit Tier 2 logs.
    """
    try:
        from beagle.core.workflow_loader import list_workflows

        for meta in list_workflows():
            if meta.get("name") == workflow_name:
                return int(meta.get("phases", 0) or 0)
        return 0
    except (OSError, ValueError, RuntimeError, ImportError):
        return 0


# ══════════════════════════════════════════════════════════════════════════════
# Workflow Tools (Part 2) — run_beagle_workflow, get_agent_recipe, list_agents
# Migrated from mcp_workflow_server.py
# ══════════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def run_beagle_workflow(
    query: str,
    workflow_name: str = "research",
    budget_usd: float = 10.0,
    steering_prompt: str = "",
    ctx: Context | None = None,  # auto-injected by FastMCP when present
) -> str:
    """Execute an Beagle multi-agent workflow.

    Runs the specified workflow through the LangGraph orchestrator,
    coordinating multiple specialized agents to process the query.

    Available workflows: audit, research, develop, incident,
    security, db-migration, deep-planning, devops, self-improvement, verify.

    Args:
        query: The query or task to process
        workflow_name: Which workflow to run (default: research)
        budget_usd: Maximum budget in USD (default: 10.0)
        steering_prompt: Optional high-priority directive injected into all agents
        ctx: FastMCP context (auto-injected). When present, workflow
            progression is forwarded as MCP progress / message
            notifications. Optional for unit-test invocation.

    Returns:
        JSON with final report, completed nodes, cost summary, and any errors.

    """
    _check_mcp_rate_limit()
    correlation_id = set_correlation_id()
    start_time = time.monotonic()
    logger.info(f"[{correlation_id}] Starting workflow: {workflow_name}")

    # ── MCP ingress firewall (Phase 4.5) ───────────────────────────────
    # Hoist validate_query to the MCP tool boundary for defense-in-depth.
    # This catches injection payloads BEFORE the orchestrator spins up,
    # complementing the existing validate_query call inside
    # _run_workflow_impl (line ~933). Both query and steering_prompt are
    # untrusted user-facing strings from the JSON-RPC arguments dict.
    for field_name, field_value in (
        ("query", query),
        ("steering_prompt", steering_prompt),
    ):
        if not field_value:
            continue
        ok, err = validate_query(field_value, mock_firewall=True)
        if not ok:
            logger.warning(
                "[%s] MCP firewall rejected %s: %s",
                correlation_id,
                field_name,
                err,
            )
            return json.dumps(
                {
                    "status": "error",
                    "code": "FIREWALL_REJECTED",
                    "error": f"Input rejected by semantic firewall: {err}",
                }
            )

    # ── Validate workflow exists before spinning up the orchestrator ─────
    # Prevents silent fallback to research graph for genuinely invalid names
    # and avoids expensive cold-start imports when the caller typo'd.
    from beagle.core.workflow_loader import validate_workflow

    validation_errors = validate_workflow(workflow_name)
    if validation_errors:
        duration = time.monotonic() - start_time
        record_metric("workflow_run", duration, success=False)
        return json.dumps(
            {
                "status": "error",
                "code": "WORKFLOW_NOT_FOUND",
                "error": f"Workflow '{workflow_name}' is not available: {validation_errors[0]}",
            }
        )

    # ── EventBridge (v13.19.1) — forward workflow events to MCP client ─
    # attach() is a no-op when ctx is None (e.g. direct unit-test call).
    # detach() is guaranteed by the try/finally below — no subscriber leak
    # even on exceptions, cancellation, or timeout.
    total_nodes = _count_workflow_phases_safe(workflow_name)
    bridge = _EventBridge(ctx, total_nodes_hint=total_nodes)
    bridge.attach()
    try:
        result = await _run_workflow_impl(query, workflow_name, budget_usd, steering_prompt)
        duration = time.monotonic() - start_time
        record_metric("workflow_run", duration, success=True)
        logger.info(f"[{correlation_id}] Workflow completed in {duration:.2f}s")
        return result
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as e:
        duration = time.monotonic() - start_time
        record_metric("workflow_run", duration, success=False)
        logger.error(f"[{correlation_id}] Workflow failed: {e}", exc_info=True)
        raise
    finally:
        bridge.detach()


async def _run_workflow_impl(
    query: str,
    workflow_name: str,
    budget_usd: float,
    steering_prompt: str,
) -> str:
    """Internal implementation of workflow execution."""
    # Validate query
    is_valid, error = validate_query(query, mock_firewall=True)
    if not is_valid:
        return json.dumps(
            {
                "status": "error",
                "code": "INVALID_INPUT",
                "error": f"Query validation failed: {error}",
            }
        )

    # v13.19.5: Heartbeat + structured timeout (P0 fix for silent hangs).
    #
    # The previous implementation wrapped the entire workflow in a single
    # `asyncio.wait_for(..., 600)`. When the discovery sub-agent took longer
    # than 600s (model cold-start + langgraph + retries), the wrapper fired
    # TimeoutError with NO progress markers and NO partial output — the
    # client saw nothing for 10 minutes, then a flat error string. This was
    # the #1 cause of "the audit workflow just hung" reports.
    #
    # The fix:
    #   1. A heartbeat task that publishes a NodeOutput event every 15s
    #      via the EventBus — clients with a progressToken see liveness.
    #   2. The same `asyncio.wait_for` cap (configurable via env), but
    #      on timeout we capture the elapsed seconds, cancel cleanly,
    #      and return a structured `code: TIMEOUT` JSON with diagnostic
    #      detail (workflow, query prefix, elapsed, hint) so the caller
    #      can surface a useful message instead of a blank.
    #   3. The heartbeat is `try/finally`'d so a timeout cancels it.
    #
    # v13.21: Two refinements over the v13.19.5 fix:
    #   4. Default cap raised from 600s to 1800s. Empirically 4/8 multi-phase
    #      workflows (audit, security, deep-planning, db-migration) cold-start
    #      the default model and take 8-15 min even on a $0.02 budget. 600s
    #      was a hair too tight. 1800s gives cold-start + 5+ phases room.
    #   5. Heartbeat messages are buffered locally to `_heartbeat_messages`
    #      so the TimeoutError JSON can return `partial_progress` (last 50
    #      messages) instead of a bare error — clients see what the
    #      workflow had reported doing before it was killed.
    # v13.21.13: Heartbeat default lowered 15s → 5s. Delegating clients
    # (goose) render the latest notification as their live status line;
    # 15s gaps read as a stall. 5s keeps the line moving and the per-tick
    # cost is one in-process event + one small JSON-RPC notification.
    _WORKFLOW_TIMEOUT_SECONDS = int(os.environ.get("BEAGLE_WORKFLOW_TIMEOUT", "1800"))
    _HEARTBEAT_INTERVAL_SECONDS = float(os.environ.get("BEAGLE_WORKFLOW_HEARTBEAT", "5"))
    workflow_start = time.monotonic()
    # v13.21: Local buffer (last 50 messages) for the TimeoutError path.
    # The heartbeat task appends to this; on timeout the buffered messages
    # become `partial_progress` in the error JSON so callers see what the
    # workflow had reported doing.
    _heartbeat_messages: list[str] = []

    async def _workflow_heartbeat() -> None:
        """Emit a liveness marker every N seconds so the client sees progress.

        Best-effort: a failing publish must never abort the workflow.
        v13.21: Also appends each message to the local _heartbeat_messages
        buffer (capped at 50) so the TimeoutError path can return them as
        `partial_progress` to the caller.
        v13.21.13: No longer a bare "still running" — the heartbeat now
        subscribes to the bus itself and reports WHAT is running: the
        active node, phases done/total, failures, accrued cost, and the
        last output line the agent produced.
        """
        try:
            from beagle.events.bus import get_event_bus
            from beagle.events.events import NodeOutput
        except ImportError:
            # events module is optional; absence is non-fatal, we just skip live tracking
            return

        # v13.21.13: Live activity tracker. Ring-buffer replays from before
        # this workflow are filtered by timestamp.
        subscribed_at = time.time()
        activity: dict[str, Any] = {
            "node": "",
            "started": 0,
            "done": 0,
            "failed": 0,
            "cost": 0.0,
            "last_line": "",
        }

        def _track(ev: Any) -> None:
            if getattr(ev, "timestamp", subscribed_at) < subscribed_at:
                return  # replayed event from a previous workflow
            et = getattr(ev, "event_type", "")
            if et == "node.started":
                activity["node"] = getattr(ev, "node_name", "") or ""
                activity["started"] += 1
            elif et in ("node.completed", "node.skipped"):
                activity["done"] += 1
                activity["cost"] += float(getattr(ev, "cost", 0.0) or 0.0)
            elif et == "node.failed":
                activity["failed"] += 1
            elif et == "node.output":
                if getattr(ev, "node_name", "") != "_workflow_heartbeat":
                    line = (getattr(ev, "content", "") or "").strip()
                    if line:
                        activity["last_line"] = line[:160]

        bus = get_event_bus()
        sub_id = bus.subscribe("node.*", _track)
        total_nodes = _count_workflow_phases_safe(workflow_name)
        interval = max(1.0, _HEARTBEAT_INTERVAL_SECONDS)
        try:
            while True:
                await asyncio.sleep(interval)
                elapsed = time.monotonic() - workflow_start
                if elapsed >= _WORKFLOW_TIMEOUT_SECONDS:
                    return
                denom = f"/{total_nodes}" if total_nodes else ""
                node_part = f" node={activity['node']!r}" if activity["node"] else " starting"
                fail_part = f" failed={activity['failed']}" if activity["failed"] else ""
                last_part = ""
                if activity["last_line"]:
                    last_part = f' last="{scrub_secrets(str(activity["last_line"]))}"'
                msg = (
                    f"[heartbeat t={int(elapsed)}s] workflow={workflow_name!r}"
                    f"{node_part} phases={activity['done']}{denom}{fail_part} "
                    f"cost≈${activity['cost']:.2f}/{budget_usd:.2f}{last_part}"
                )
                # v13.21: Append to the local buffer (capped at 50) so the
                # TimeoutError path can return partial_progress.
                _heartbeat_messages.append(msg)
                if len(_heartbeat_messages) > 50:
                    _heartbeat_messages.pop(0)
                with contextlib.suppress(Exception):
                    bus.publish(
                        NodeOutput(
                            workflow_id=workflow_name,
                            node_name="_workflow_heartbeat",
                            stream_type="stderr",
                            content=msg,
                        )
                    )
        except asyncio.CancelledError:
            # Expected on workflow completion or timeout — exit silently.
            return
        finally:
            with contextlib.suppress(Exception):
                bus.unsubscribe(sub_id)

    # v13.21.13: Explicit start/end markers on the bus so the delegating
    # client sees clean workflow boundaries instead of inferring them from
    # the first heartbeat. Best-effort telemetry, like the heartbeat.
    def _publish_workflow_marker(event: Any) -> None:
        with contextlib.suppress(Exception):
            from beagle.events.bus import get_event_bus

            get_event_bus().publish(event)

    heartbeat_task: asyncio.Task | None = None
    try:
        with contextlib.suppress(ImportError):
            from beagle.events.events import WorkflowStarted

            _publish_workflow_marker(
                WorkflowStarted(
                    workflow_id=workflow_name,
                    query=query[:200],
                    budget_usd=budget_usd,
                )
            )
        heartbeat_task = asyncio.create_task(
            _workflow_heartbeat(),
            name=f"beagle.workflow_heartbeat.{workflow_name}",
        )
        result = await asyncio.wait_for(
            run_workflow(
                query=query,
                workflow_name=workflow_name,
                budget=budget_usd,
                steering=steering_prompt,
            ),
            timeout=_WORKFLOW_TIMEOUT_SECONDS,
        )
        with contextlib.suppress(ImportError):
            from beagle.events.events import WorkflowCompleted

            _publish_workflow_marker(
                WorkflowCompleted(
                    workflow_id=workflow_name,
                    success=not result.get("errors"),
                    total_cost_usd=float(result.get("total_cost", 0.0) or 0.0),
                    total_tokens=int(result.get("total_tokens", 0) or 0),
                    duration_seconds=time.monotonic() - workflow_start,
                    completed_nodes=len(result.get("completed_nodes", []) or []),
                    errors=len(result.get("errors", []) or []),
                )
            )
        # Scrub secrets from output
        final_report = scrub_secrets(result.get("final_report", ""))
        execution_ctx = scrub_secrets(result.get("raw_execution_context", ""))

        # v1.0.2 (P-fix2): 3-way terminal status. The previous logic
        # was a 2-way 'completed' | 'completed_with_errors' that
        # conflated synthesis-failure with secondary-pass failure.
        # synthesis_failed is a distinct terminal status (the artifact
        # is unusable) so downstream consumers can react without
        # parsing the error strings. The synthesis-failure structural
        # guard matches the one at infrastructure/tools/_impl.py:882-907
        # (the other MCP boundary) so both code paths report the same
        # verdict.
        import re as _re_pfix2

        _nonempty_lines = [ln for ln in final_report.splitlines() if ln.strip()]
        _has_citation = bool(
            _re_pfix2.search(
                r"[A-Za-z0-9_./-]+\.(?:py|yaml|yml|md|json|toml):\d+",
                final_report,
            )
        )
        _synthesis_has_content = len(_nonempty_lines) >= 3 and (
            len(final_report) > 200 or _has_citation
        )
        _synthesis_failed = bool(final_report.strip()) and not _synthesis_has_content
        # Respect the orchestrator's own flag if it set one.
        if result.get("synthesis_failed"):
            _synthesis_failed = True

        if _synthesis_failed:
            _status = "synthesis_failed"
        elif result.get("errors"):
            _status = "completed_with_errors"
        else:
            _status = "completed"

        return json.dumps(
            {
                "status": _status,
                "synthesis_failed": _synthesis_failed,
                "final_report": final_report,
                "research_plan": result.get("research_plan", ""),
                "verified_facts": result.get("verified_facts", ""),
                "execution_context_preview": execution_ctx[:2000],
                "completed_nodes": result.get("completed_nodes", []),
                "total_tokens": result.get("total_tokens", 0),
                "total_cost_usd": result.get("total_cost", 0.0),
                "errors": result.get("errors", []),
            },
            indent=2,
        )
    except TimeoutError:
        elapsed = time.monotonic() - workflow_start
        logger.error(
            f"Workflow timed out after {elapsed:.1f}s "
            f"(cap={_WORKFLOW_TIMEOUT_SECONDS}s): "
            f"query={query[:100]!r}, workflow={workflow_name}"
        )
        # v13.21: Surface partial progress on timeout. Previously the timeout
        # error returned a bare JSON with no context about what the workflow
        # had accomplished. Now we return the buffered heartbeat messages so
        # callers can see where the workflow was when it timed out. This
        # surfaces the "cold-start" signal that previously made timeouts
        # look like silent failures.
        return json.dumps(
            {
                "status": "error",
                "code": "TIMEOUT",
                "error": (
                    f"Workflow execution timed out after "
                    f"{int(elapsed)}s (cap {_WORKFLOW_TIMEOUT_SECONDS}s). "
                    f"The orchestrator did not produce a partial result — "
                    f"likely cause: model cold-start > timeout cap, or a "
                    f"sub-phase blocked on an external HTTP call. "
                    f"Try raising BEAGLE_WORKFLOW_TIMEOUT (currently "
                    f"{_WORKFLOW_TIMEOUT_SECONDS}) or running a smaller "
                    f"workflow (e.g. 'verify' instead of 'audit')."
                ),
                "elapsed_seconds": int(elapsed),
                "timeout_cap_seconds": _WORKFLOW_TIMEOUT_SECONDS,
                "query": query[:200],
                "workflow": workflow_name,
                # v13.21: Buffered heartbeat messages — what the workflow
                # had reported doing before the timeout fired. Empty if the
                # workflow failed before the first heartbeat (e.g. cold-start
                # exceeded the heartbeat interval).
                "partial_progress": list(_heartbeat_messages),
                "partial_progress_count": len(_heartbeat_messages),
            }
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as e:
        logger.error(f"Workflow execution failed: {e}", exc_info=True)
        # Security: Don't leak internal error details to client
        error_msg = str(e)
        # Scrub any potential secrets from error message
        error_msg = scrub_secrets(error_msg)
        # Truncate long error messages to prevent information leakage
        if len(error_msg) > 500:
            error_msg = error_msg[:500] + "... (truncated)"
        return json.dumps(
            {
                "status": "error",
                "code": "EXECUTION_FAILED",
                "error": "Workflow execution failed",
                "details": error_msg,
            }
        )
    finally:
        # Heartbeat cleanup — runs on success, TimeoutError, and any other
        # exception. Cancels the liveness task and awaits its cancellation.
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task


@mcp.tool()
async def get_agent_recipe(agent_name: str) -> str:
    """Retrieve the recipe/prompt template for a specific Beagle agent.

    Useful for understanding what each agent does and how it's configured.

    Args:
        agent_name: Agent name (e.g., 'research-planner', 'synthesis-writer')

    Returns:
        The agent recipe content or an error message.

    """
    _check_mcp_rate_limit()
    # Validate agent_name — prevent path traversal
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,63}$", agent_name):
        return json.dumps(
            {
                "status": "error",
                "code": "INVALID_INPUT",
                "error": (
                    f"Invalid agent_name: {agent_name!r} "
                    "(alphanumeric, hyphens, underscores only, max 64 chars)"
                ),
            }
        )

    workspace = get_workspace_root()
    recipe_path = (workspace / "recipes" / f"{agent_name}.xml").resolve()

    # Ensure resolved path is still under recipes/ (prevent traversal via symlinks).
    # ``Path.relative_to`` raises ``ValueError`` if ``recipe_path`` is outside
    # ``recipes_dir`` (audit S3, v13.17.0) — the previous ``str.startswith`` check
    # had a symlink-bypass vector where a path of the form ``recipes/../etc/passwd``
    # could be coerced to start with the un-resolved ``recipes/`` prefix.
    recipes_dir = (workspace / "recipes").resolve()
    try:
        recipe_path.relative_to(recipes_dir)
    except ValueError:
        return json.dumps(
            {
                "status": "error",
                "code": "INVALID_PATH",
                "error": f"Unauthorized path for agent: {agent_name}",
            }
        )

    if not recipe_path.exists():
        available = [p.stem for p in (workspace / "recipes").glob("*.xml")]
        return json.dumps(
            {
                "status": "error",
                "code": "NOT_FOUND",
                "error": f"Recipe not found: {agent_name}",
                "available_agents": sorted(available),
            },
            indent=2,
        )

    content = recipe_path.read_text(encoding="utf-8")
    return json.dumps(
        {
            "agent": agent_name,
            "recipe": content,
        },
        indent=2,
    )


@mcp.tool()
async def list_agents() -> str:
    """List all available Beagle agents with their descriptions.

    Returns:
        JSON array of agent objects with name and description.

    """
    _check_mcp_rate_limit()
    correlation_id = set_correlation_id()
    start_time = time.monotonic()
    try:
        result = _list_agents_impl()
        duration = time.monotonic() - start_time
        record_metric("list_agents", duration, success=True)
        logger.debug(f"[{correlation_id}] list_agents completed in {duration:.4f}s")
        return result
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as e:
        duration = time.monotonic() - start_time
        record_metric("list_agents", duration, success=False)
        logger.exception(f"[{correlation_id}] list_agents failed: {e}")
        raise


def _list_agents_impl() -> str:
    """Internal implementation of agent listing.

    Returns agents from BOTH the agents.toml configuration AND the recipes
    directory, combining them with deduplication. Agents from agents.toml
    take priority (they have model/temperature overrides).
    """
    agents_by_name: dict[str, dict] = {}

    # 1. Load agents from agents.toml (has model/provider/temp overrides)
    try:
        from beagle.config.agent_config import list_agents as list_config_agents

        config_agents = list_config_agents()
        for name, profile in config_agents.items():
            agents_by_name[name] = {
                "name": profile.name,
                "description": profile.description or f"Agent: {profile.name}",
                "model": profile.model,
                "provider": profile.provider,
                "temperature": profile.temperature,
                "source": "agents.toml",
            }
    except (OSError, ValueError, RuntimeError, ImportError) as e:
        logger.debug(f"Could not load agents from config: {e}")

    # 2. Load agents from recipes/ (supplementary, doesn't override agents.toml)
    workspace = get_workspace_root()
    recipes_dir = workspace / "recipes"

    for recipe_path in sorted(recipes_dir.glob("*.xml")):
        name = recipe_path.stem
        if name in agents_by_name:
            # Already covered by agents.toml — add recipe source tag
            agents_by_name[name]["source"] = "agents.toml+recipe"
            continue

        content = recipe_path.read_text(encoding="utf-8")
        # Extract description from YAML frontmatter
        description = ""
        if content.startswith("---"):
            try:
                import yaml

                end = content.index("---", 3)
                frontmatter = yaml.safe_load(content[3:end])
                description = frontmatter.get("description", "")
            except ImportError as exc:
                logger.warning(
                    "PyYAML is not installed (%s); the recipe front matter cannot be "
                    "parsed and the description is left empty.",
                    exc,
                )

        # Infer model from recipe name/phase
        try:
            from beagle.context.recipe_agent_bridge import (
                _infer_model_for_phase,
                _infer_phase_from_name,
                _infer_temperature_for_phase,
            )

            phase = _infer_phase_from_name(name)
            model = _infer_model_for_phase(phase)
            temperature = _infer_temperature_for_phase(phase)
        except ImportError:
            model = "glm-5.1:cloud"
            temperature = 0.4

        agents_by_name[name] = {
            "name": name,
            "description": description or f"Agent: {name}",
            "model": model,
            "provider": "ollama_cloud",
            "temperature": temperature,
            "source": "recipe",
        }

    agents = list(agents_by_name.values())
    return json.dumps(agents, indent=2)


# ── Observability Tools ──────────────────────────────────────────────────────


@mcp.tool()
async def get_metrics() -> str:
    """Return server metrics including request counts and latency statistics.

    Returns:
        JSON with request totals, success/error rates, and latency percentiles.

    """
    _check_mcp_rate_limit()
    correlation_id = set_correlation_id()
    logger.debug(f"[{correlation_id}] Metrics requested")
    return json.dumps(get_metrics_summary(), indent=2)


@mcp.tool()
async def health_check() -> str:
    """Perform a comprehensive health check of the workflow server.

    Checks:
    - MCP server connectivity
    - Workflow loader availability
    - Config accessibility
    - Memory usage

    Returns:
        JSON with health status for each component.

    """
    _check_mcp_rate_limit()
    correlation_id = set_correlation_id()
    start_time = time.monotonic()
    logger.info(f"[{correlation_id}] Health check initiated")

    health = {
        "status": "healthy",
        "timestamp": time.time(),
        "correlation_id": correlation_id,
        "checks": {},
    }

    # Check 1: Config loading
    try:
        config = get_config()
        health["checks"]["config"] = {  # type: ignore[index]
            "status": "ok",
            "provider": config.goose.provider,
            "model": config.goose.default_model,
        }
    except (OSError, ValueError, RuntimeError, ImportError) as e:
        health["checks"]["config"] = {"status": "error", "message": str(e)}  # type: ignore[index]
        health["status"] = "degraded"

    # Check 2: Workflow loader
    try:
        workflows = list_workflows()
        health["checks"]["workflows"] = {  # type: ignore[index]
            "status": "ok",
            "count": len(workflows),
        }
    except (OSError, ValueError, RuntimeError, ImportError) as e:
        health["checks"]["workflows"] = {"status": "error", "message": str(e)}  # type: ignore[index]
        health["status"] = "degraded"

    # Check 3: Router
    try:
        routes = list_routable_workflows()
        health["checks"]["router"] = {  # type: ignore[index]
            "status": "ok",
            "routable_workflows": len(routes),
        }
    except (OSError, ValueError, RuntimeError, ImportError) as e:
        health["checks"]["router"] = {"status": "error", "message": str(e)}  # type: ignore[index]
        health["status"] = "degraded"

    # Check 4: Memory usage
    try:
        import resource

        mem_usage = resource.getrusage(resource.RUSAGE_SELF)
        health["checks"]["memory"] = {  # type: ignore[index]
            "status": "ok",
            "max_rss_mb": round(mem_usage.ru_maxrss / 1024, 2),
            "shared_mb": round(mem_usage.ru_ixrss / 1024, 2),
            "unshared_mb": round(mem_usage.ru_idrss / 1024, 2),
        }
    except (OSError, ValueError, ImportError) as e:
        health["checks"]["memory"] = {"status": "unavailable", "message": str(e)}  # type: ignore[index]

    # Check 5: Metrics
    health["checks"]["metrics"] = {  # type: ignore[index]
        "status": "ok",
        "total_requests": _metrics["requests"]["total"],
        "success_rate": round(
            _metrics["requests"]["success"] / max(_metrics["requests"]["total"], 1) * 100,
            2,
        ),
    }

    duration = time.monotonic() - start_time
    health["checks"]["health_check_latency"] = f"{duration:.4f}s"  # type: ignore[index]
    record_metric("health_check", duration, success=True)
    logger.info(f"[{correlation_id}] Health check completed in {duration:.4f}s: {health['status']}")

    return json.dumps(health, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# Meta-process tools (D1) — read and steer the self-regulating loops.
# ══════════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def meta_list() -> str:
    """List the registered meta-processes.

    Returns:
        JSON array of process names.

    """
    _check_mcp_rate_limit()
    from beagle.meta.builtin import register_builtin_processes
    from beagle.meta.process import list_processes

    register_builtin_processes()
    return json.dumps(list_processes(), indent=2)


@mcp.tool()
async def meta_observe(process: str) -> str:
    """Return the current state of a meta-process.

    Args:
        process: The process name (context_folding, memory_consolidation,
            budget_enforcement, routing, verification).

    Returns:
        JSON with the process's metrics, last run, and decisions.

    """
    _check_mcp_rate_limit()
    from beagle.meta.builtin import register_builtin_processes
    from beagle.meta.process import get_process

    register_builtin_processes()
    obs = get_process(process).observe()
    return json.dumps(
        {
            "process": obs.process,
            "healthy": obs.healthy,
            "metrics": obs.metrics,
            "last_run": obs.last_run,
            "decisions": obs.decisions,
        },
        indent=2,
    )


@mcp.tool()
async def meta_tune(process: str, knob: str, value: str) -> str:
    """Adjust a tuning knob on a meta-process.

    Args:
        process: The process name.
        knob: The knob name (e.g. threshold, cadence_seconds, budget_usd).
        value: The new value as a string (parsed to the knob's type).

    Returns:
        JSON confirming the new value.

    """
    _check_mcp_rate_limit()
    from beagle.meta.builtin import register_builtin_processes
    from beagle.meta.process import get_process

    register_builtin_processes()
    proc = get_process(process)
    # Parse the value to the knob's current type so a float knob stays float.
    current = proc.observe().metrics.get(knob)
    if isinstance(current, bool):
        parsed: object = value.lower() in ("1", "true", "yes")
    elif isinstance(current, float):
        parsed = float(value)
    elif isinstance(current, int):
        parsed = int(value)
    else:
        parsed = value
    proc.tune(knob, parsed)
    return json.dumps({"process": process, "knob": knob, "value": parsed}, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# Governance tools (D4) — submit_code, validate_security.
# ══════════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def submit_code(code: str, language: str = "python") -> str:
    """Submit code for security validation.

    Args:
        code: The code to validate.
        language: The language (only ``python`` is supported).

    Returns:
        JSON with a verdict and structured findings.

    """
    _check_mcp_rate_limit()
    from beagle.security.ast_validator import validate_python_code_ast

    if language != "python":
        return json.dumps(
            {
                "verdict": "rejected",
                "findings": [
                    {
                        "severity": "error",
                        "message": f"unsupported language {language!r}; only python is supported",
                    }
                ],
            },
            indent=2,
        )
    is_valid, reason = validate_python_code_ast(code)
    if is_valid:
        return json.dumps(
            {"verdict": "approved", "findings": []},
            indent=2,
        )
    return json.dumps(
        {
            "verdict": "rejected",
            "findings": [
                {
                    "severity": "error",
                    "message": reason,
                    "suggested_fix": "Remove the dangerous construct and resubmit.",
                }
            ],
        },
        indent=2,
    )


@mcp.tool()
async def validate_security(code: str) -> str:
    """Validate code against the bundled security baseline.

    Runs the AST validator and reports structured findings with a severity,
    a file/line reference, and a suggested fix.

    Args:
        code: The code to validate.

    Returns:
        JSON with a verdict and structured findings.

    """
    _check_mcp_rate_limit()
    from beagle.security.ast_validator import validate_python_code_ast

    is_valid, reason = validate_python_code_ast(code)
    if is_valid:
        return json.dumps(
            {
                "verdict": "approved",
                "baseline": "security_baseline.toml",
                "findings": [],
            },
            indent=2,
        )
    return json.dumps(
        {
            "verdict": "rejected",
            "baseline": "security_baseline.toml",
            "findings": [
                {
                    "severity": "error",
                    "file": "<submitted>",
                    "line": 0,
                    "message": reason,
                    "suggested_fix": "Remove the dangerous construct and resubmit.",
                }
            ],
        },
        indent=2,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Resources — beagle://config, beagle://workflows, beagle://routing-rules
# Migrated from mcp_workflow_server.py
# ══════════════════════════════════════════════════════════════════════════════


@mcp.resource("beagle://config")
async def get_beagle_config() -> str:
    """Current Beagle configuration."""
    config = get_config()
    return json.dumps(
        {
            "version": "12.0.0",
            "goose_binary": config.goose.binary_path,
            "default_model": config.goose.default_model,
            "provider": config.goose.provider,
            "default_budget_usd": config.budget.default_usd,
            "hard_limit_usd": config.budget.hard_limit_usd,
            "cache_enabled": config.cache.enabled,
        },
        indent=2,
    )


@mcp.resource("beagle://workflows")
async def get_workflows_resource() -> str:
    """Available workflow definitions."""
    return json.dumps(list_workflows(), indent=2)


@mcp.resource("beagle://routing-rules")
async def get_routing_rules() -> str:
    """Workflow routing keywords and patterns."""
    return json.dumps(list_routable_workflows(), indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# Session Continuity & Context Tools (v13.16.5 — FIX: registered from _impl.py)
# Migrated from infrastructure/tools/_impl.py
# ══════════════════════════════════════════════════════════════════════════════

from beagle.infrastructure.tools._impl import (  # ruff: ignore[E402]
    arxiv_search,
    beagle_progress_update,
    beagle_session_bootstrap,
    check_and_fold_context,
    code_search,
    enforce_post_final_answer_fold,
    post_compaction_rehydrate,
    query_fold,
    read_session_state,
    report_context_usage,
    web_research,
    web_search,
)

mcp.tool()(beagle_session_bootstrap)
mcp.tool()(beagle_progress_update)
mcp.tool()(query_fold)
mcp.tool()(report_context_usage)
mcp.tool()(check_and_fold_context)
mcp.tool()(enforce_post_final_answer_fold)
mcp.tool()(post_compaction_rehydrate)
mcp.tool()(read_session_state)
mcp.tool()(code_search)
mcp.tool()(web_search)
mcp.tool()(arxiv_search)
mcp.tool()(web_research)

# ── Server-side token counter (v13.22.0) ────────────────────────────────────
# Auto-subscribes to context.warning events on the EventBus and fires
# the WatchdogActor at pre_compact (0.58) / critical (0.85) thresholds.
# This is the in-process companion to the hourly cron watchdog — the
# cron is the safety net for stale state, this is the real-time trigger.
try:
    from beagle.context.token_counter_subscriber import (
        get_token_counter,
    )

    _token_counter = get_token_counter()  # idempotent, auto-subscribes
    logger.info(
        f"[MCP-Utility] Server-side token counter active "
        f"(sub_id={_token_counter._subscription_id[:8] or 'pending'}…)"
    )
except (ImportError, RuntimeError, OSError, ValueError) as exc:
    # The subscriber is best-effort: if events.bus or context.trigger
    # is unavailable (e.g. broken Docker container with no DNS), the
    # LLM-initiative check_and_fold_context tool still works.
    logger.warning(f"[MCP-Utility] Token counter init failed (non-fatal): {exc}")


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Consistent --version across dev-tool entry points.
    from .mcp_common import maybe_print_version

    if maybe_print_version():
        raise SystemExit(0)

    # Transport selection. The factory image runs the MCP server over the
    # network so the orchestrator (goose) can connect without a `docker exec`
    # stdio pipe. Selection logic:
    #   1. MCP_TRANSPORT env var (explicit override)
    #   2. BEAGLE_EXECUTION_ENV == "docker" → streamable-http on FASTMCP_PORT (8420)
    #   3. default → stdio (preserves dev ergonomics for local CLI use)
    #
    # SECURITY (B-1, audit v13.22.0): the streamable-http path requires
    # bearer-token authentication per MCP_TRUST.md. We enforce it by
    # (a) requiring BEAGLE_MCP_TOKEN at startup (fail-closed RuntimeError
    # if missing) and (b) installing a Starlette middleware that rejects
    # every request whose Authorization header is missing or doesn't
    # match the token. The middleware uses hmac.compare_digest for
    # constant-time comparison to prevent timing attacks.
    _transport = os.environ.get("MCP_TRANSPORT")
    if not _transport:
        _transport = (
            "streamable-http"
            if os.environ.get("BEAGLE_EXECUTION_ENV", "").lower() == "docker"
            else "stdio"
        )
    _host = os.environ.get(
        "MCP_HOST", "127.0.0.1"
    )  # audit L11: loopback is the safer default; exposure closed by token gate
    _port = int(os.environ.get("MCP_PORT", os.environ.get("FASTMCP_PORT", "8420")))
    logger.info(
        f"Starting Beagle MCP Utility Server (transport={_transport}, host={_host}, port={_port})"
    )
    if _transport == "stdio":
        mcp.run(transport="stdio")
    else:
        # SECURITY (B-1): HTTP transport MUST require authentication.
        # Refuse to bind to a non-loopback host without a token. This is
        # fail-closed on purpose — silently downgrading to unauthenticated
        # HTTP would expose the entire utility-tool surface to any host
        # that can reach the container.
        import hmac

        _expected_token = os.environ.get("BEAGLE_MCP_TOKEN", "")
        if not _expected_token:
            raise RuntimeError(
                "BEAGLE_MCP_TOKEN environment variable is REQUIRED for streamable-http "
                "transport. See MCP_TRUST.md — HTTP transports always require "
                "authentication regardless of trust label. Generate a token with: "
                "python -c 'import secrets; print(secrets.token_hex(32))'"
            )

        mcp.settings.host = _host
        mcp.settings.port = _port
        logger.info(
            f"[MCP-Utility-Auth] Bearer-token auth ENABLED for streamable-http on {_host}:{_port}"
        )
        # We run uvicorn directly with the FastMCP Starlette app wrapped in
        # an ASGI middleware that enforces bearer-token auth. FastMCP 1.27's
        # internal ``AuthenticationMiddleware`` is wired to OAuth 2.0
        # Resource Server semantics, which is heavier than we need for a
        # shared-secret deployment. Our middleware is the simplest correct
        # thing: every non-health request must carry
        # ``Authorization: Bearer <BEAGLE_MCP_TOKEN>``; comparison is
        # constant-time via hmac.compare_digest.
        import uvicorn

        starlette_app = mcp.streamable_http_app()

        class BearerAuthMiddleware:
            """ASGI 3 middleware enforcing ``Authorization: Bearer <token>``.

            Per MCP_TRUST.md: HTTP transports must require authentication
            regardless of trust label. This middleware wraps every request
            before it reaches FastMCP's internal router.
            """

            def __init__(self, inner_app: Any) -> None:
                self.inner_app = inner_app

            async def __call__(self, scope: Any, receive: Any, send: Any) -> Any:
                if scope["type"] != "http":
                    return await self.inner_app(scope, receive, send)
                # Unauthenticated paths: health + root probe.
                path = scope.get("path", "/")
                if path in ("/", "/health", "/healthz"):
                    return await self.inner_app(scope, receive, send)
                # Extract Authorization header.
                headers = dict(scope.get("headers") or [])
                auth = headers.get(b"authorization", b"").decode("latin-1", errors="replace")
                if not auth.startswith("Bearer "):
                    await self._reject(
                        send,
                        401,
                        "Bearer token required",
                    )
                    return
                token = auth[7:].strip().encode()
                expected = _expected_token.encode()
                if not hmac.compare_digest(token, expected):
                    await self._reject(
                        send,
                        403,
                        "Invalid bearer token",
                    )
                    return
                return await self.inner_app(scope, receive, send)

            @staticmethod
            async def _reject(send: Any, status: int, detail: str) -> None:
                await send(
                    {
                        "type": "http.response.start",
                        "status": status,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (
                                b"www-authenticate",
                                b'Bearer realm="beagle-utility"',
                            ),
                        ],
                    }
                )
                body = b'{"error":"unauthorized","detail":"' + detail.encode() + b'"}'
                await send({"type": "http.response.body", "body": body})

        wrapped_app = BearerAuthMiddleware(starlette_app)
        uvicorn.run(
            wrapped_app,
            host=_host,
            port=_port,
            log_level=mcp.settings.log_level.lower()
            if isinstance(mcp.settings.log_level, str)
            else "info",
        )
