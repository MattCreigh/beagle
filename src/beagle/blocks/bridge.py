"""Block Bridge — connect AgentState to ExecutionContext.

Beagle v13.8.1 Phase 5: execute_block_agent() bridging AgentState
↔ ExecutionContext.
"""

from __future__ import annotations

import logging
from typing import Any

from .context import ExecutionContext, ExecutionResult
from .engine import BlockComposer, ComposerConfig
from .errors import BlockError
from .schema import AgentDefinition

try:
    _HAS_STATE = True
except (ImportError, AttributeError):  # pragma: no cover — Optional dependency
    _HAS_STATE = False

logger = logging.getLogger("Beagle.blocks.bridge")


def agent_state_to_context(state: Any) -> ExecutionContext:
    """Convert AgentState into ExecutionContext for block composition."""
    ctx = ExecutionContext(inputs={}, depth=0)
    ctx.inputs["query"] = getattr(state, "query", "")
    for k, v in getattr(state, "metadata", {}).items():
        ctx.inputs[k] = v
    return ctx


def execution_result_to_state(result: ExecutionResult, state: Any) -> Any:
    """Merge ExecutionResult back into AgentState."""
    if not _HAS_STATE:
        return state
    if result.success:
        state.raw_execution_context = str(result.final_output) if result.final_output else ""
        state.metadata["block_results"] = [r.output for r in result.block_results]
        state.metadata["block_cost_usd"] = result.total_cost_usd
    else:
        state.errors.extend(result.errors)
        state.metadata["block_errors"] = result.errors
    return state


async def execute_block_agent(
    recipe_spec: dict[str, Any],
    state: Any,
    composer: BlockComposer | None = None,
) -> Any:
    """Execute a block-composed agent and merge results back into AgentState.

    This is the primary bridge entry-point called from DAG nodes when the
    skill_name resolves to a block-recipe (e.g. ``block://python-backend``).
    """
    recipe = AgentDefinition.model_validate(recipe_spec)
    ctx = agent_state_to_context(state)
    comp = composer or BlockComposer(config=ComposerConfig())
    try:
        result = await comp.compose_async(recipe, inputs=ctx.inputs)
    except BlockError:
        raise
    except (RuntimeError, OSError, TimeoutError, ValueError) as exc:  # catch: NARROWED
        logger.exception("Block agent execution failed")
        result = ExecutionResult(
            success=False,
            errors=[str(exc)],
        )
    return execution_result_to_state(result, state)
