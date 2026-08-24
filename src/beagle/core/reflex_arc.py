"""Beagle v13.5.2 - Reflex Arc v2: Trivial-query detection and local fast path.

Extracted from autonomous_orchestrator.py for maintainability.
Determines whether a query can be handled by a fast cached path (EASY)
or requires full workflow execution (HARD).

v13.5.2 enhancement: EASY queries can now be answered by a local,
CPU-optimized model (e.g., phi-4-mini via Ollama on localhost:11434),
bypassing the full LangGraph DAG entirely for near-instant responses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from beagle.config._config_path import find_config_toml

logger = logging.getLogger("Beagle.reflex_arc")

# Keywords that indicate trivial/command-like queries
TRIVIAL_ONLY: frozenset[str] = frozenset(
    {
        "ping",
        "status",
        "hello",
        "hi",
        "hey",
        "bye",
        "thanks",
        "thank you",
        "help",
        "what can you do",
        "list commands",
        "show help",
        "version",
        "about",
    }
)

# Check for enhanced modes (skill library + code mode)
try:
    from beagle.core.skill_library import SkillLibrary

    ENHANCED_MODES = True
except ImportError as e:
    logger.debug(f"Skill library not available for reflex arc: {e}")
    ENHANCED_MODES = False


# ── Reflex Arc Configuration ──────────────────────────────────────────────────


@dataclass
class ReflexArcConfig:
    """Configuration for the Reflex Arc v2 local fast path.

    Loaded from config.toml [reflex_arc] section.
    """

    enabled: bool = True
    local_model: str = "phi-4-mini"
    provider: str = "ollama_local"
    timeout_seconds: int = 10

    @classmethod
    def from_toml(cls) -> ReflexArcConfig:
        """Load ReflexArcConfig from config.toml."""
        try:
            import tomllib

            config_path = find_config_toml()
            if config_path.exists():
                with open(config_path, "rb") as f:
                    data = tomllib.load(f)
                ra = data.get("reflex_arc", {})
                return cls(
                    enabled=ra.get("enabled", True),
                    local_model=ra.get("local_model", "phi-4-mini"),
                    provider=ra.get("provider", "ollama_local"),
                    timeout_seconds=ra.get("timeout_seconds", 10),
                )
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.debug(f"[reflex_arc] Failed to load config: {e}")
        return cls()


_config_cache: ReflexArcConfig | None = None


def get_reflex_arc_config() -> ReflexArcConfig:
    """Get the cached ReflexArcConfig singleton."""
    global _config_cache
    if _config_cache is None:
        _config_cache = ReflexArcConfig.from_toml()
    return _config_cache


# ── Routing ───────────────────────────────────────────────────────────────────


async def diffadapt_routing(query: str) -> str:
    """Determine task complexity for routing.

    Uses the DiffAdapt pattern: trivial queries get "EASY" (cached/bypassed),
    substantive queries get "HARD" (full workflow execution).

    Args:
        query: The user query string.

    Returns:
        "EASY" for trivial/cached queries, "HARD" for substantive queries.

    """
    ql = query.lower().strip()

    # Check for exact match or command prefix only
    is_trivial = False
    for kw in TRIVIAL_ONLY:
        if ql == kw or ql.startswith(kw + " ") or ql.startswith(kw + "\n"):
            is_trivial = True
            break

    if not is_trivial:
        logger.info("⚡ [DiffAdapt] Query requires full workflow execution")
        return "HARD"

    # For trivial queries, use skill library cache check
    if ENHANCED_MODES:
        try:
            skill_lib = SkillLibrary()
            matched = await skill_lib.search_skills(query)
            if matched and matched[0].use_count > 3:
                logger.info(
                    f"🎯 [Skill Match] Found '{matched[0].name}' (uses: {matched[0].use_count})"
                )
                logger.info("⚡ [Cached Path] Using proven skill route")
                return "EASY"
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.debug(f"Skill lookup failed: {e}")

    logger.info("⚡ [Reflex Arc] Query is trivial command")
    return "EASY"


# ── Local Model Fast Path (v13.5.2) ──────────────────────────────────────────


async def _execute_local_query(query: str) -> str | None:
    """Execute a trivial query on a local, CPU-optimized model.

    Uses the goose subprocess pool with a model override pointing to the
    configured local model (e.g., phi-4-mini via Ollama on localhost:11434).
    This bypasses the entire LangGraph DAG for near-instant responses to
    trivial queries like "help", "status", "version", etc.

    Falls back to None if the local model is unavailable or times out,
    allowing the caller to fall back to the remote workflow path.

    Args:
        query: The trivial query to execute locally.

    Returns:
        The model's response string, or None if unavailable/failed.

    """
    config = get_reflex_arc_config()
    if not config.enabled:
        logger.debug("[reflex_arc] Fast path disabled in config")
        return None

    try:
        from ..utils.subprocess_pool import run_goose

        # Map "ollama_local" to the "openai" provider with localhost host
        # Ollama exposes an OpenAI-compatible API at localhost:11434
        provider = "openai"

        # Build a minimal system directive for trivial responses
        system_directive = (
            "You are a helpful assistant. Respond briefly and concisely. "
            "Always wrap your entire response in <final_answer> tags."
        )

        logger.info(
            f"[reflex_arc] Executing local fast path: model={config.local_model}, "
            f"timeout={config.timeout_seconds}s"
        )

        final_answer, _raw_stdout = await run_goose(
            prompt=query,
            system_directive=system_directive,
            node_name="reflex_arc_fast_path",
            timeout=config.timeout_seconds,
            readonly=True,
            model_override=config.local_model,
            provider_override=provider,
        )

        if final_answer and final_answer.strip():
            logger.info("[reflex_arc] Local fast path succeeded")
            return final_answer.strip()

        logger.debug("[reflex_arc] Local model returned empty response")
        return None

    except ConnectionError as e:
        logger.debug(f"[reflex_arc] Local model unreachable: {e}")
        return None
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.warning(f"[reflex_arc] Local fast path failed unexpectedly: {e}")
        return None


async def reflex_arc_execute(
    query: str,
) -> dict[str, Any]:
    """Full Reflex Arc v2 execution: route + optional local fast path.

    1. Routes the query via diffadapt_routing (EASY/HARD).
    2. If EASY and reflex_arc is enabled, tries local model fast path.
    3. Returns a result dict indicating what action the caller should take.

    Args:
        query: The user's query.

    Returns:
        Dict with keys:
        - "routing": "EASY" or "HARD"
        - "fast_path_response": str | None — local model response if available
        - "use_fast_path": bool — whether the caller should use fast_path_response

    """
    routing = await diffadapt_routing(query)

    if routing == "HARD":
        return {
            "routing": "HARD",
            "fast_path_response": None,
            "use_fast_path": False,
        }

    # EASY query — try local fast path
    config = get_reflex_arc_config()
    if not config.enabled:
        return {
            "routing": "EASY",
            "fast_path_response": None,
            "use_fast_path": False,
        }

    response = await _execute_local_query(query)
    if response is not None:
        return {
            "routing": "EASY",
            "fast_path_response": response,
            "use_fast_path": True,
        }

    # Local model unavailable — caller should handle EASY with default behavior
    return {
        "routing": "EASY",
        "fast_path_response": None,
        "use_fast_path": False,
    }


__all__ = [
    "ENHANCED_MODES",
    "TRIVIAL_ONLY",
    "ReflexArcConfig",
    "diffadapt_routing",
    "get_reflex_arc_config",
    "reflex_arc_execute",
]
