"""LangChain LLM Node — In-process LLM execution for Beagle workflows.

Phase 3 of the LangChain Ecosystem Compatibility Plan.
Provides execute_langchain_llm_node() that replaces the Goose
subprocess pattern with an in-process BaseChatModel.ainvoke() call.

This unlocks LangChain callbacks, LangSmith tracing, structured output,
and streaming — all impossible with the subprocess pattern.

Used when YAML phase specifies executor="langchain_llm".
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ..config.config import assess_task_complexity, resolve_model_for_task
from ..cost_tracker import estimate_tokens_agnostic
from ..events import NodeFailed, get_event_bus
from .chat_model import OllamaCloudChatModel

logger = logging.getLogger("Beagle.bridges.llm_node")


def _extract_final_answer(response: Any) -> str:
    """Extract the text content from an LLM response.

    Handles AIMessage, string, and other response types.

    Args:
        response: Response from BaseChatModel.ainvoke().

    Returns:
        String content of the response.

    """
    if isinstance(response, str):
        return response
    if hasattr(response, "content"):
        content = response.content
        if isinstance(content, str):
            return content
        # Some models return list of content blocks
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and "text" in block:
                    parts.append(block["text"])
            return "\n".join(parts)
    return str(response)


async def execute_langchain_llm_node(
    state: dict[str, Any],
    phase_spec: dict[str, Any],
    output_key: str,
    model_override: str | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Execute an LLM call in-process via OllamaCloudChatModel.

    Called when workflow_loader detects executor="langchain_llm"
    in the phase YAML. This replaces execute_goose_node() with
    an in-process LLM call that:
      - Fires LangChain callbacks (on_llm_start, on_llm_end)
      - Supports LangSmith tracing
      - Enables structured output
      - Reduces latency (no subprocess spawn)

    Args:
        state: Current Beagle workflow state dict.
        phase_spec: Phase specification from YAML workflow.
        output_key: Key to store result in state.
        model_override: Explicit model name override.
        timeout: Timeout in seconds.

    Returns:
        State update dict following Beagle conventions.

    """
    # Config loaded on-demand inside OllamaCloudChatModel
    skill_name = phase_spec.get("agent", phase_spec.get("name", "llm_node"))

    # Resolve prompt from phase spec (SP-7: leaf module — avoids core.nodes
    # cycle; core.nodes re-exports the same builder for other callers).
    prompt_template = phase_spec.get("prompt_template", "{query}")
    from ..utils.prompt_builder import make_prompt_builder

    prompt_builder = make_prompt_builder(prompt_template)
    prompt = prompt_builder(state)

    # Build system directive (same as execute_goose_node)
    from ..utils.env_manager import get_recipes_dir
    from ..utils.safe_file_ops import ensure_recipe_exists

    recipe_path = get_recipes_dir() / f"{skill_name}.xml"
    recipe_path = ensure_recipe_exists(recipe_path)
    recipe_content = recipe_path.read_text(encoding="utf-8")
    system_directive = recipe_content

    # Inject hydrated context
    hydrated_context = state.get("hydrated_context", "")
    if hydrated_context:
        system_directive = f"{system_directive}\n\n{hydrated_context}"

    # Inject global context + steering
    global_ctx = state.get("global_context", "")
    steering = state.get("steering_prompt", "")
    if global_ctx:
        system_directive += f"\n\n<global_project_context>\n{global_ctx}\n</global_project_context>"
    if steering:
        system_directive += f"\n\n<HIGH_PRIORITY_DIRECTIVE>\n{steering}\n</HIGH_PRIORITY_DIRECTIVE>"

    # Add output format instruction (same as Goose nodes for compatibility)
    system_directive += (
        "\n\n## OUTPUT FORMAT (MANDATORY)\n"
        "You MUST wrap your entire final response inside <final_answer> tags.\n"
        "Everything outside these tags will be DISCARDED.\n"
        "Example: <final_answer>Your complete report here</final_answer>\n"
        "If you do not use <final_answer> tags, your work will be LOST."
    )

    # Resolve model
    if model_override:
        resolved_model = model_override
    else:
        complexity = assess_task_complexity(state.get("query", ""))
        resolved_model = resolve_model_for_task(
            skill_name, query=state.get("query", ""), complexity=complexity
        )

    logger.info(f"[{skill_name}] Using model: {resolved_model} (langchain_llm executor)")

    # Build LangChain messages
    messages = []
    if system_directive:
        messages.append(SystemMessage(content=system_directive))
    messages.append(HumanMessage(content=prompt))  # type: ignore[arg-type]

    # Create chat model
    try:
        chat = OllamaCloudChatModel(model_name=resolved_model)
    except ImportError as exc:
        err_msg = f"{skill_name}: langchain-openai not installed — {exc}"
        logger.error(err_msg)
        get_event_bus().publish(
            NodeFailed(
                workflow_id=state.get("workflow_id", ""),
                node_name=skill_name,
                error=err_msg,
                attempt=1,
                error_category="system",
            )
        )
        return {"errors": [err_msg], "completed_nodes": [f"{skill_name}(failed)"]}
    except RuntimeError as exc:
        err_msg = f"{skill_name}: API key unavailable — {exc}"
        logger.error(err_msg)
        get_event_bus().publish(
            NodeFailed(
                workflow_id=state.get("workflow_id", ""),
                node_name=skill_name,
                error=err_msg,
                attempt=1,
                error_category="auth",
            )
        )
        return {"errors": [err_msg], "completed_nodes": [f"{skill_name}(failed)"]}

    # Execute with timeout
    node_timeout = timeout or 300
    try:
        response = await asyncio.wait_for(
            chat.ainvoke(messages),
            timeout=node_timeout,
        )

        # Extract text from response
        raw_text = _extract_final_answer(response)

        # Parse <final_answer> tags (same as Goose nodes)
        match = re.search(r"<final_answer>(.*?)</final_answer>", raw_text, re.DOTALL)
        if match:
            final_answer = match.group(1).strip()
        else:
            # No tags found — use entire response
            final_answer = raw_text.strip()
            logger.debug(f"[{skill_name}] No <final_answer> tags in LLM response, using full text")

        # Token estimation for cost tracking
        input_tokens = estimate_tokens_agnostic(prompt + system_directive)
        output_tokens = estimate_tokens_agnostic(raw_text)

        updates: dict[str, Any] = {
            "completed_nodes": [skill_name],
            "total_tokens": state.get("total_tokens", 0) + input_tokens + output_tokens,
            "metadata": {**state.get("metadata", {}), output_key: final_answer},
        }

        from beagle.utils.field_mapping import map_output_to_state

        target_key = map_output_to_state(output_key, skill_name=skill_name)
        if target_key:
            updates[target_key] = final_answer

        logger.info(f"[{skill_name}] LLM node completed: {output_key} ({len(final_answer)} chars)")
        return updates

    except TimeoutError:
        err_msg = f"{skill_name}: LLM call timed out after {node_timeout}s"
        logger.error(err_msg)
        get_event_bus().publish(
            NodeFailed(
                workflow_id=state.get("workflow_id", ""),
                node_name=skill_name,
                error=err_msg,
                attempt=1,
                error_category="timeout",
                duration_seconds=node_timeout,
            )
        )
        return {"errors": [err_msg], "completed_nodes": [f"{skill_name}(timeout)"]}

    except Exception as exc:  # broad catch intentional
        err_msg = f"{skill_name}: {type(exc).__name__} - {exc}"
        logger.error(f"[{skill_name}] LLM call failed: {exc}", exc_info=True)

        # Try fallback models from config.toml
        try:
            from ..config.config import get_config

            fallback_chain = get_config()._raw.get("goose", {}).get("fallback_chain", [])  # type: ignore[attr-defined]
            for fallback_model in fallback_chain:
                if fallback_model == resolved_model:
                    continue  # Skip the model that already failed
                logger.info(f"[{skill_name}] Trying fallback model: {fallback_model}")
                try:
                    fallback_chat = OllamaCloudChatModel(model_name=fallback_model)
                    fallback_response = await asyncio.wait_for(
                        fallback_chat.ainvoke(messages),
                        timeout=node_timeout,
                    )
                    raw_text = _extract_final_answer(fallback_response)
                    match = re.search(r"<final_answer>(.*?)</final_answer>", raw_text, re.DOTALL)
                    final_answer = match.group(1).strip() if match else raw_text.strip()

                    input_tokens = estimate_tokens_agnostic(prompt + system_directive)
                    output_tokens = estimate_tokens_agnostic(raw_text)

                    updates = {
                        "completed_nodes": [skill_name],
                        "total_tokens": state.get("total_tokens", 0) + input_tokens + output_tokens,
                        "metadata": {
                            **state.get("metadata", {}),
                            output_key: final_answer,
                        },
                    }
                    from beagle.utils.field_mapping import (
                        map_output_to_state,
                    )

                    target_key = map_output_to_state(output_key, skill_name=skill_name)
                    if target_key:
                        updates[target_key] = final_answer

                    logger.info(f"[{skill_name}] Fallback to {fallback_model} succeeded")
                    return updates
                except (ImportError, RuntimeError, OSError, TimeoutError) as fallback_exc:
                    logger.warning(
                        f"[{skill_name}] Fallback model {fallback_model} also failed: "
                        f"{type(fallback_exc).__name__}: {fallback_exc}"
                    )
                    continue
        except Exception as chain_exc:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.warning(f"[{skill_name}] Fallback chain lookup failed: {chain_exc}")
            pass

        get_event_bus().publish(
            NodeFailed(
                workflow_id=state.get("workflow_id", ""),
                node_name=skill_name,
                error=err_msg,
                attempt=1,
                error_category="llm_error",
            )
        )
        return {"errors": [err_msg], "completed_nodes": [f"{skill_name}(failed)"]}
