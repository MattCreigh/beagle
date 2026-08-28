"""Code Mode: Chained execution pattern inspired by Cloudflare Dynamic Workers.

Based on Cloudflare's approach to AI agents which achieves:
- 81% token reduction via chained API calls
- TypeScript-first tool definitions
- Lightweight sandboxing for untrusted code

Reference: Cloudflare Blog (March 24, 2026) - "Sandboxing AI agents, 100x faster"
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("Beagle.code_mode")

# Cloudflare-style tool schema in TypeScript-like format
TOOL_SCHEMA_TEMPLATE = """
interface Tool {{
  name: string;
  description: string;
  parameters: {{
    [key: string]: {{
      type: string;
      description?: string;
      required?: boolean;
    }}
  }};
}}

const tools: Tool[] = {tools_json};
"""


class ExecutionMode(Enum):
    """Code execution modes."""

    SAFE = "safe"  # Sandboxed execution with resource limits
    DIRECT = "direct"  # Direct execution (for trusted code)
    CHAINED = "chained"  # Chained API calls (Cloudflare-style)


@dataclass
class ToolDefinition:
    """TypeScript-style tool definition for LLM consumption."""

    name: str
    description: str
    parameters: dict[str, dict[str, Any]]

    def to_typescript(self) -> str:
        """Convert to TypeScript interface format."""
        props = []
        for pname, props_dict in self.parameters.items():
            ptype = props_dict.get("type", "string")
            required = props_dict.get("required", False)
            desc = props_dict.get("description", "")
            optional = "" if required else "?"
            props.append(
                f"    {pname}{optional}: {ptype}; // {desc}"
                if desc
                else f"    {pname}{optional}: {ptype};"
            )
        return f"""{{
  name: "{self.name}",
  description: "{self.description}",
  parameters: {{
{chr(10).join(props)}
  }}
}}"""

    def to_llm_format(self) -> str:
        """Convert to LLM-friendly format (no TypeScript)."""
        params = "\n".join(
            f"  - {pname}: {p.get('type', 'string')}"
            + (" (required)" if p.get("required") else "")
            + (f" - {p.get('description', '')}" if p.get("description") else "")
            for pname, p in self.parameters.items()
        )
        return f"""Tool: {self.name}
Description: {self.description}
Parameters:
{params}"""


@dataclass
class ToolCall:
    """A single tool call in a chain."""

    tool_name: str
    parameters: dict[str, Any]
    call_id: str = ""
    depends_on: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.call_id:
            import uuid

            self.call_id = str(uuid.uuid4())


@dataclass
class ChainResult:
    """Result of a chained execution."""

    call_id: str
    tool_name: str
    success: bool
    result: Any
    error: str | None = None
    execution_time_ms: float = 0.0
    tokens_used: int = 0


class CodeModeExecutor:
    """Executes chained tool calls with Cloudflare-style efficiency.

    Key optimizations:
    1. Chained execution reduces LLM calls (81% token reduction)
    2. TypeScript-first schemas are more concise than OpenAPI
    3. Dependency tracking ensures correct execution order
    4. Resource limits prevent runaway execution
    """

    def __init__(
        self,
        max_concurrent: int = 10,
        timeout_seconds: float = 30.0,
        max_chain_length: int = 50,
    ):
        self.max_concurrent = max_concurrent
        self.timeout_seconds = timeout_seconds
        self.max_chain_length = max_chain_length
        self._tool_registry: dict[str, Callable] = {}
        self._execution_history: list[ChainResult] = []
        self._semaphore = asyncio.Semaphore(max_concurrent)
        # D-02/ASYNC110: one event per call id, set when its ChainResult is
        # recorded. Dependency waits block on the event (no polling busy-wait)
        # and the caller bounds the wait with self.timeout_seconds.
        self._completion_events: dict[str, asyncio.Event] = {}

    def register_tool(self, tool: ToolDefinition, handler: Callable) -> None:
        """Register a tool with its handler function."""
        self._tool_registry[tool.name] = handler
        logger.info(f"Registered tool: {tool.name}")

    def get_tools_schema(self) -> str:
        """Get TypeScript-formatted tool schema for LLM."""
        tools = [
            ToolDefinition(
                name=name,
                description="Registered tool",
                parameters={},
            )
            for name in self._tool_registry
        ]
        return TOOL_SCHEMA_TEMPLATE.format(
            tools_json=json.dumps([t.__dict__ for t in tools], indent=2)
        )

    async def execute_chain(
        self,
        calls: list[ToolCall],
        context: dict[str, Any] | None = None,
    ) -> list[ChainResult]:
        """Execute a chain of tool calls with dependency resolution.

        Args:
            calls: List of tool calls to execute
            context: Shared context passed to all tools

        Returns:
            List of ChainResult in execution order

        """
        results: dict[str, ChainResult] = {}
        if len(calls) > self.max_chain_length:
            logger.warning(
                f"Chain length {len(calls)} exceeds max {self.max_chain_length}, truncating"
            )
            # D-02: reject the dependents of any dropped call. A dependent
            # left in the chain after its dependency is dropped would wait
            # forever on a result that can never be produced.
            dropped_ids = {c.call_id for c in calls[self.max_chain_length :]}
            calls = calls[: self.max_chain_length]
            calls = [
                c
                for c in calls
                if not (set(c.depends_on) & dropped_ids) or self._fail_orphan(c, dropped_ids, results)
            ]

        context = context or {}
        execution_order, unsatisfiable = self._resolve_dependencies(calls)

        # D-02: unsatisfiable calls (cyclic or missing dependency) are
        # recorded as failed ChainResults instead of being executed with a
        # dependency that can never be satisfied — the old behaviour fed
        # them to an unbounded busy-wait.
        for call in unsatisfiable:
            failed = ChainResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                success=False,
                result=None,
                error=(
                    "Unsatisfiable dependencies "
                    f"{sorted(set(call.depends_on) - self._satisfiable_ids(call, execution_order))}: "
                    "cyclic or missing dependency in chain"
                ),
            )
            self._record_result(call.call_id, failed, results)

        for call in execution_order:
            # Wait for dependencies (bounded — see _wait_for_dependencies)
            if call.depends_on:
                try:
                    await asyncio.wait_for(
                        self._wait_for_dependencies(call.depends_on, results),
                        timeout=self.timeout_seconds,
                    )
                except TimeoutError:
                    failed = ChainResult(
                        call_id=call.call_id,
                        tool_name=call.tool_name,
                        success=False,
                        result=None,
                        error=(
                            f"Dependency wait exceeded {self.timeout_seconds}s; "
                            "dependencies never completed"
                        ),
                    )
                    self._record_result(call.call_id, failed, results)
                    continue

            # Execute with semaphore
            async with self._semaphore:
                result = await self._execute_single(call, context, results)
                self._record_result(call.call_id, result, results)

        return [results[c.call_id] for c in calls]

    def _record_result(
        self,
        call_id: str,
        result: ChainResult,
        results: dict[str, ChainResult],
    ) -> None:
        """Store a ChainResult and signal its completion event.

        Dependency waits block on the per-id event (no polling); this is the
        single place results enter the dict so the event always fires.
        """
        results[call_id] = result
        self._execution_history.append(result)
        self._completion_events.setdefault(call_id, asyncio.Event()).set()
    def _fail_orphan(
        self,
        call: ToolCall,
        dropped_ids: set[str],
        results: dict[str, ChainResult],
    ) -> bool:
        """Record a failed ChainResult for a call whose dependency was truncated away.

        Returns True (the call is removed from the execution set); the failure
        record is its receipt.

        """
        missing = sorted(set(call.depends_on) & dropped_ids)
        failed = ChainResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=False,
            result=None,
            error=f"Depends on dropped call(s) after max_chain_length truncation: {missing}",
        )
        self._record_result(call.call_id, failed, results)
        return True

    @staticmethod
    def _satisfiable_ids(call: ToolCall, execution_order: list[ToolCall]) -> set[str]:
        """Dependency ids of ``call`` that the resolved order can actually satisfy."""
        order_ids = [c.call_id for c in execution_order]
        satisfiable: set[str] = set()
        for dep in call.depends_on:
            if dep in order_ids:
                # Only deps scheduled EARLIER can be satisfied — a dep that
                # runs later in the order is a cycle by construction.
                if order_ids.index(dep) < order_ids.index(call.call_id):
                    satisfiable.add(dep)
        return satisfiable

    def _resolve_dependencies(
        self, calls: list[ToolCall]
    ) -> tuple[list[ToolCall], list[ToolCall]]:
        """Resolve execution order based on dependencies.

        Returns:
            Tuple of (executable calls in topological order, unsatisfiable
            calls). A call is unsatisfiable when its dependency graph is
            cyclic or names a dependency id that does not exist in the
            chain. Unsatisfiable calls are never executed.

        """
        resolved: list[ToolCall] = []
        unsatisfiable: list[ToolCall] = []
        remaining = list(calls)
        satisfied: set[str] = set()
        known_ids = {c.call_id for c in calls}

        while remaining:
            made_progress = False
            for call in remaining[:]:
                deps = set(call.depends_on)
                # A dependency unknown to the chain is unsatisfiable by
                # construction (D-02: missing dep_id trigger).
                if not (deps <= satisfied and deps <= known_ids):
                    continue
                resolved.append(call)
                remaining.remove(call)
                satisfied.add(call.call_id)
                made_progress = True

            if not made_progress:
                # D-02: whatever remains is cyclic or blocked on a missing
                # id — mark it failed instead of appending it to the
                # execution order (the old behaviour hung the executor).
                unsatisfiable.extend(remaining)
                break

        return resolved, unsatisfiable

    async def _wait_for_dependencies(
        self,
        depends_on: list[str],
        results: dict[str, ChainResult],
    ) -> None:
        """Wait for dependent calls to complete.

        Uses an ``asyncio.Event`` per dependency id, set when a result for
        that id is stored in ``results`` by :meth:`execute_chain`. The caller
        bounds the whole wait with ``asyncio.wait_for`` (``self.timeout_seconds``),
        so a dependency that never completes fails the wait instead of hanging
        the executor (D-02).

        Raises:
            TimeoutError: Propagated when the caller's ``asyncio.wait_for``
                budget expires because a dependency never completed.

        """
        for dep_id in depends_on:
            # An unknown dep_id is a caller error in the wait path (the
            # resolution step already failed it up); a defensive event avoids
            # an unbounded wait in every case.
            event = self._completion_events.get(dep_id)
            while event is None:
                if dep_id in results:
                    break
                await asyncio.sleep(0.01)
                event = self._completion_events.get(dep_id)
            if event is not None and dep_id not in results:
                await event.wait()

    async def _execute_single(
        self,
        call: ToolCall,
        context: dict[str, Any],
        results: dict[str, ChainResult],
    ) -> ChainResult:
        """Execute a single tool call."""
        start_time = time.monotonic()

        # Substitute dependency results into parameters
        resolved_params = {}
        for key, value in call.parameters.items():
            if isinstance(value, str) and value.startswith("$"):
                # Reference to another call's result
                ref_id = value[1:]
                if ref_id in results:
                    resolved_params[key] = results[ref_id].result
                else:
                    resolved_params[key] = value
            else:
                resolved_params[key] = value

        try:
            if call.tool_name not in self._tool_registry:
                return ChainResult(
                    call_id=call.call_id,
                    tool_name=call.tool_name,
                    success=False,
                    result=None,
                    error=f"Unknown tool: {call.tool_name}",
                    execution_time_ms=(time.monotonic() - start_time) * 1000,
                )

            handler = self._tool_registry[call.tool_name]

            # Execute with timeout
            result = await asyncio.wait_for(
                handler(resolved_params, context, results),
                timeout=self.timeout_seconds,
            )

            return ChainResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                success=True,
                result=result,
                execution_time_ms=(time.monotonic() - start_time) * 1000,
            )

        except TimeoutError:
            return ChainResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                success=False,
                result=None,
                error=f"Timeout after {self.timeout_seconds}s",
                execution_time_ms=(time.monotonic() - start_time) * 1000,
            )
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            return ChainResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                success=False,
                result=None,
                error=str(e),
                execution_time_ms=(time.monotonic() - start_time) * 1000,
            )

    def get_execution_stats(self) -> dict[str, Any]:
        """Get execution statistics."""
        if not self._execution_history:
            return {"total_calls": 0}

        successful = sum(1 for r in self._execution_history if r.success)
        failed = len(self._execution_history) - successful
        avg_time = sum(r.execution_time_ms for r in self._execution_history) / len(
            self._execution_history
        )

        return {
            "total_calls": len(self._execution_history),
            "successful": successful,
            "failed": failed,
            "success_rate": successful / len(self._execution_history),
            "avg_execution_ms": avg_time,
            "total_tokens_saved": self._estimate_tokens_saved(),
        }

    def _estimate_tokens_saved(self) -> int:
        """Estimate tokens saved by chained execution vs separate calls.

        Cloudflare reports 81% token reduction with chaining.
        """
        if len(self._execution_history) < 2:
            return 0

        # Rough estimate: each chain saves token overhead
        return len(self._execution_history) * 500  # ~500 tokens saved per chain


# ── Chained execution prompt builder ──────────────────────────────────────────


def build_chain_prompt(
    query: str,
    tools: list[ToolDefinition],
    previous_results: list[ChainResult] | None = None,
) -> str:
    """Build a prompt for chained execution (Cloudflare-style).

    Args:
        query: The user's request
        tools: Available tools
        previous_results: Previous chain results for context

    Returns:
        Prompt optimized for chained tool execution

    """
    tool_schemas = "\n\n".join(t.to_llm_format() for t in tools)

    context_parts = []
    if previous_results:
        context_parts.append("Previous results:")
        for r in previous_results[-5:]:  # Last 5 results
            status = "✓" if r.success else "✗"
            context_parts.append(f"  {status} {r.tool_name}: {r.result or r.error}")

    context = "\n".join(context_parts) if context_parts else "No previous results."

    return f"""You are executing a chained task.

Query: {query}

Available tools:
{tool_schemas}

{context}

Respond with a JSON array of tool calls to execute in order. Each call should have:
- "tool": tool name
- "parameters": tool parameters
- "depends_on": array of call_ids this depends on (empty if no dependencies)

Example response:
[
  {{"tool": "search", "parameters": {{"query": "python tutorial"}},
   "depends_on": []}},
  {{"tool": "read_file", "parameters": {{"path": "$CALL_ID_0.result"}},
   "depends_on": ["CALL_ID_0"]}}
]

Respond with ONLY the JSON array."""


def parse_chain_response(response: str) -> list[ToolCall]:
    """Parse LLM response into ToolCall objects."""
    # Extract JSON from response
    json_match = re.search(r"\[\s*\{.*\}\s*\]", response, re.DOTALL)
    if not json_match:
        raise ValueError(f"No JSON array found in response: {response[:200]}")

    try:
        calls_data = json.loads(json_match.group())
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}") from None

    calls = []
    for data in calls_data:
        call = ToolCall(
            tool_name=data["tool"],
            parameters=data.get("parameters", {}),
            depends_on=data.get("depends_on", []),
        )
        # Substitute call IDs from context
        for _key, value in call.parameters.items():
            if isinstance(value, str) and value.startswith("$"):
                # This is a reference to a previous result
                pass  # Keep as-is, will be resolved at execution time
        calls.append(call)

    return calls


# ── Global executor ────────────────────────────────────────────────────────────

_executor: CodeModeExecutor | None = None


def get_code_mode_executor() -> CodeModeExecutor:
    """Get global CodeModeExecutor instance."""
    global _executor
    if _executor is None:
        _executor = CodeModeExecutor()
    return _executor


if __name__ == "__main__":

    async def demo():
        executor = get_code_mode_executor()

        # Register a simple tool
        async def search_handler(params, ctx, results):
            query = params.get("query", "")
            return f"Search results for: {query}"

        executor.register_tool(
            ToolDefinition(
                name="search",
                description="Search the web",
                parameters={
                    "query": {
                        "type": "string",
                        "description": "Search query",
                        "required": True,
                    }
                },
            ),
            search_handler,
        )

        # Create a chain
        calls = [
            ToolCall(tool_name="search", parameters={"query": "Python best practices"}),
        ]

        # Execute
        results = await executor.execute_chain(calls)

        logger.info("Chain results:")
        for r in results:
            status = "✓" if r.success else "✗"
            logger.info(f"  {status} {r.tool_name}: {r.result} ({r.execution_time_ms:.1f}ms)")

        logger.info(f"\nStats: {executor.get_execution_stats()}")

    asyncio.run(demo())
