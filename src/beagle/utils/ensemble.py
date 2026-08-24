"""Multi-model ensemble executor — GRPO-style multi-agent collaboration.

Runs the same prompt across N different Ollama Cloud models concurrently,
collects their responses, and lets a judge model select the best parts
from each to produce a superior combined result.

Designed for the i7-6700T: all execution is remote (Ollama Cloud), so
local CPU is never stressed — only network latency matters.

Usage::

    ensemble = MultiModelEnsemble(
        models=["minimax-m3:cloud", "deepseek-v4-pro:cloud", "kimi-k2.6:cloud"],
        judge_model="glm-5.1:cloud",
    )
    best_response = await ensemble.run(
        prompt="Analyze this architecture...",
        system_directive="You are a code architect.",
    )
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from .subprocess_pool import run_goose

logger = logging.getLogger("Beagle.utils.ensemble")

# ── Data structures ───────────────────────────────────────────────────────────────


@dataclass
class ModelResponse:
    """A single model's response to an ensemble prompt."""

    model: str
    final_answer: str
    raw_stdout: str
    latency_seconds: float
    quality_score: float = 0.0
    selected: bool = False


@dataclass
class EnsembleResult:
    """Result of a multi-model ensemble run."""

    responses: list[ModelResponse]
    best_response: ModelResponse
    combined_response: str
    judge_summary: str


# ── Prompt engineering helpers ───────────────────────────────────────────────────


_JUDGE_SYSTEM = """\
You are a code review judge. Rate each candidate solution on the following criteria:
1. **Correctness** (does it solve the problem correctly?) - weight 40%
2. **Type safety** (are all type hints present and correct?) - weight 20%
3. **Error handling** (are edge cases caught with specific exceptions?) - weight 15%
4. **Test coverage** (does it include a test or verification block?) - weight 15%
5. **Conciseness** (no dead code, no over-engineering) - weight 10%

For each candidate, output a JSON object with:
- candidate_index
- scores (object with each criterion 0-10)
- total_score (weighted sum)
- reason (one sentence)

Then select the candidate with the highest total_score. Output final JSON:
{"selected_index": X, "all_scores": [...]}
"""


async def _call_model(
    prompt: str,
    system_directive: str,
    model: str,
    timeout: int = 120,
) -> ModelResponse:
    """Call a single model and return its response with timing."""
    import time

    start = time.monotonic()

    try:
        final_answer, raw_stdout = await run_goose(
            prompt=prompt,
            system_directive=(
                f"{system_directive}\n\n"
                "CRITICAL: Do NOT use any tools. "
                "Reply with plain text only. "
                "Wrap your complete response in <final_answer> tags."
            ),
            node_name=f"ensemble_{model.replace(':', '_')}",
            timeout=timeout,
            model_override=model,
        )
        latency = time.monotonic() - start
        return ModelResponse(
            model=model,
            final_answer=final_answer,
            raw_stdout=raw_stdout,
            latency_seconds=latency,
        )
    except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
        latency = time.monotonic() - start
        logger.warning(f"[Ensemble] {model} failed after {latency:.1f}s: {exc}")
        return ModelResponse(
            model=model,
            final_answer="",
            raw_stdout=str(exc),
            latency_seconds=latency,
        )


def _parse_judge_json(raw: str) -> dict[str, Any] | None:
    """Extract JSON from judge output, handling any wrapper text."""
    # Try to find JSON block
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        import json

        return json.loads(match.group())  # type: ignore[no-any-return]
    except (json.JSONDecodeError, ValueError):
        return None


def _score_response(response: ModelResponse) -> float:
    """Simple heuristic scoring when judge is unavailable."""
    score = 0.0

    # Prefer longer, more detailed responses (within reason)
    if len(response.final_answer) > 200:
        score += 1.0

    # Bonus for specific code snippets or file paths (shows depth)
    if re.search(r"(def |class |\w+\.py:\d+)", response.final_answer):
        score += 1.5

    # Bonus for structured formatting
    if re.search(r"(^#{1,3} |\n- |\n\d+\. )", response.final_answer, re.MULTILINE):
        score += 1.0

    # Penalty for very short responses
    if len(response.final_answer) < 50:
        score -= 2.0

    # Penalty for obviously incomplete responses
    if response.final_answer.count(".") < 2:
        score -= 1.0

    return max(0.0, score)


# ── Main ensemble ─────────────────────────────────────────────────────────────────


class MultiModelEnsemble:
    """Ensemble of models running the same prompt, with judge-based selection.

    Args:
        models: List of Ollama Cloud model names to run concurrently.
            Defaults to the best available: deepseek-v3.2, glm-5.1:cloud, minimax-m2.7:cloud
        judge_model: Model used to judge and combine responses.
            Defaults to glm-5:cloud.
        timeout_per_model: Seconds before killing a single model call.

    """

    def __init__(
        self,
        models: list[str] | None = None,
        judge_model: str | None = None,
        timeout_per_model: int | None = None,
    ) -> None:
        from beagle.config.config import get_config

        config = get_config().ensemble

        # Default panel of coding experts — diverse architectures for solution diversity
        # Optimized based on integration testing (2026-03-26)
        self._models = models or config.panel_models
        self._judge_model = judge_model or config.judge_model
        self._timeout = (
            timeout_per_model if timeout_per_model is not None else config.timeout_per_model
        )
        logger.info(
            f"[Ensemble] Initialized with models: {self._models}, judge: {self._judge_model}"
        )

    @property
    def models(self) -> list[str]:
        """List of models in the ensemble."""
        return list(self._models)

    async def run(
        self,
        prompt: str,
        system_directive: str,
        judge_prompt: str | None = None,
    ) -> EnsembleResult:
        """Run all models concurrently and return the best combined result.

        Args:
            prompt: The user prompt (same for all models).
            system_directive: System directive applied to all models.
            judge_prompt: Optional extra instruction for the judge
                (e.g., "Focus on security implications").

        Returns:
            EnsembleResult with all responses, best, and combined answer.

        """
        logger.info(f"[Ensemble] Running {len(self._models)} models concurrently")

        # Launch all model calls concurrently
        tasks = [
            _call_model(prompt, system_directive, model, self._timeout) for model in self._models
        ]
        responses: list[ModelResponse] = await asyncio.gather(*tasks, return_exceptions=True)  # type: ignore[assignment]

        # Filter out exceptions
        valid_responses: list[ModelResponse] = []
        for r in responses:
            if isinstance(r, Exception):
                logger.error(f"[Ensemble] Model failed: {r}")
            else:
                valid_responses.append(r)

        if not valid_responses:
            raise RuntimeError(
                "All models in ensemble failed. "
                "Check: 1) model availability in config.toml [models], "
                "2) API connectivity, 3) budget limits. "
                "Try: reduce ensemble panel_models or increase timeout_per_model."
            )

        # Score responses heuristically (fallback if judge unavailable)
        for r in valid_responses:
            r.quality_score = _score_response(r)

        # Try judge-based selection
        combined = ""
        judge_summary = ""
        best_model_name = ""
        try:
            judge_result = await self._run_judge(prompt, valid_responses, judge_prompt)
            judge_summary = judge_result.get("verdict", "")
            combined = judge_result.get("combined_answer", "")
            # Use explicit selected_model field from judge result
            best_model_name = judge_result.get("selected_model", "")
        except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.warning(f"[Ensemble] Judge failed ({exc}), using heuristic selection")
            combined = ""
            best_model_name = ""

        # Fall back to heuristic selection
        if not combined:
            best_response = max(valid_responses, key=lambda r: r.quality_score)
            combined = best_response.final_answer
            best_model_name = best_response.model
            judge_summary = (
                f"Heuristic selection (scores: "
                f"{', '.join(f'{r.model}={r.quality_score:.1f}' for r in valid_responses)})"
            )

        # Mark the selected response
        for r in valid_responses:
            r.selected = r.model == best_model_name

        best = next((r for r in valid_responses if r.selected), valid_responses[0])

        logger.info(
            f"[Ensemble] Done. Best: {best.model} (score={best.quality_score:.1f}), "
            f"combined={len(combined)} chars"
        )

        return EnsembleResult(
            responses=valid_responses,
            best_response=best,
            combined_response=combined,
            judge_summary=judge_summary,
        )

    async def _run_judge(
        self,
        original_prompt: str,
        responses: list[ModelResponse],
        extra_instruction: str | None,
    ) -> dict[str, Any]:
        """Ask the judge model to rate and combine responses."""
        # Build the multi-response prompt for the judge with numbered candidates
        responses_text = "\n\n".join(
            f"=== Candidate {i} ({r.model}, latency={r.latency_seconds:.1f}s) ===\n"
            f"{r.final_answer[:2000]}"
            for i, r in enumerate(responses)
        )

        judge_prompt = (
            f"ORIGINAL PROMPT:\n{original_prompt}\n\n"
            f"{responses_text}\n\n"
            f"{extra_instruction or ''}\n\n"
            f"Rate each candidate (0-{len(responses) - 1}) using the scoring criteria. "
            f"Respond ONLY with JSON in the specified format."
        )

        judge_system = _JUDGE_SYSTEM

        final_answer, _raw = await run_goose(
            prompt=judge_prompt,
            system_directive=judge_system,
            node_name=f"ensemble_judge_{self._judge_model.replace(':', '_')}",
            timeout=180,
            model_override=self._judge_model,
        )

        result = _parse_judge_json(final_answer)
        if not result:
            logger.warning(f"[Ensemble] Judge output was not valid JSON: {final_answer[:200]}")
            raise ValueError("Judge did not return valid JSON")

        # Parse new format: selected_index + all_scores
        selected_idx = result.get("selected_index", 0)
        best_model_name = (
            responses[selected_idx].model
            if 0 <= selected_idx < len(responses)
            else responses[0].model
        )

        return {
            "verdict": (
                f"Selected candidate {selected_idx} ({best_model_name}) "
                f"with score {result.get('total_score', 0):.1f}"
            ),
            "combined_answer": responses[selected_idx].final_answer
            if 0 <= selected_idx < len(responses)
            else responses[0].final_answer,
            "selected_model": best_model_name,  # Add explicit selected model field
            "best_parts": [
                {"from_model": r.model, "best_excerpt": r.final_answer[:500]} for r in responses
            ],
            "ratings": result.get("all_scores", []),
        }
