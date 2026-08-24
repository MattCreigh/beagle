"""BlockComposer — topologically sort and dispatch block agents.

Beagle v13.8.1 Phase 3: read TOML recipe, Kahn's sort, tier dispatch,
parallel via asyncio.gather bounded by GoosePool, retry via tenacity,
timeout via asyncio.wait_for, budget check per block.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    import toml as tomllib  # type: ignore[no-redef]

try:
    from tenacity import (
        Retrying,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential,
    )

    _HAS_TENACITY = True
except ImportError:
    _HAS_TENACITY = False

from .cache import AgentCache
from .context import BlockResult, BlockStatus, ExecutionContext, ExecutionResult
from .errors import BudgetExceededError
from .jinja_env import render_template
from .registry import BlockRegistry
from .schema import AgentDefinition, AgentManifest, BlockRef

logger = logging.getLogger("Beagle.blocks.engine")


@dataclass
class ComposerConfig:
    """Runtime configuration for the BlockComposer."""

    max_retries: int = 3
    timeout_seconds: float = 300.0  # Orchestrator timeout; overridden from config
    budget_usd: float = 10.0
    budget_warning: float = 0.8
    max_workers: int = 4
    schema_version: str = "1.0.0"

    def __post_init__(self):
        if self.timeout_seconds <= 0:
            try:
                from beagle.config.config import get_config

                self.timeout_seconds = get_config().orchestrator.timeout_seconds
            except (
                ImportError,
                AttributeError,
                RuntimeError,
                OSError,
            ):  # catch: NARROWED  # RATIONALE=four-tuple: lazy import of optional block lib, attribute lookup on registry, runtime dispatch errors, OS errors on block file load
                self.timeout_seconds = 300.0


class TierExecutionError(Exception):
    """Raised when a tier fails after all retries."""

    def __init__(self, tier: int, errors: list[str]):
        self.tier = tier
        self.errors = errors
        super().__init__(f"Tier {tier} failed: {'; '.join(errors)}")


class BlockComposer:
    """Orchestrate block-defined agents via topological tier execution."""

    def __init__(self, config: ComposerConfig | None = None) -> None:
        self.config = config or ComposerConfig()
        self.registry = BlockRegistry.instance()
        self.cache = AgentCache()
        self._budget_consumed: float = 0.0

    # ── Public API ──────────────────────────────────────────────────────

    def load_recipe(self, path: Path | str) -> AgentDefinition:
        """Load an agent recipe from a TOML file."""
        raw = Path(path).read_bytes()
        data = tomllib.loads(raw.decode("utf-8"))
        parsed = AgentDefinition.model_validate(data)
        assert isinstance(parsed, AgentDefinition)
        return parsed

    def compose(
        self, recipe: AgentDefinition, inputs: dict[str, Any] | None = None
    ) -> ExecutionResult:
        """Run a composed agent synchronously."""
        return asyncio.run(self.compose_async(recipe, inputs))

    async def compose_async(
        self,
        recipe: AgentDefinition,
        inputs: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        """Run a composed agent asynchronously with tier parallelism."""
        ctx = ExecutionContext(inputs=inputs or {}, depth=0)
        manifest = self._resolve_manifest(recipe)
        tiers = self._topological_sort(manifest)
        results: list[BlockResult] = []
        start = time.monotonic()

        for tier_idx, tier in enumerate(tiers, start=1):
            logger.info(f"Tier {tier_idx}/{len(tiers)}: {[b.name for b in tier]}")
            tier_results = await self._execute_tier(tier, ctx, recipe)
            for tr in tier_results:
                results.append(tr)
                self._budget_consumed += tr.cost_usd
                if tr.status == BlockStatus.FAILURE:
                    return ExecutionResult(
                        success=False,
                        final_output=None,
                        block_results=results,
                        total_duration_seconds=time.monotonic() - start,
                        total_cost_usd=self._budget_consumed,
                        errors=list(tr.errors),
                    )
                # SKIPPED is non-fatal: the block did not run and that is
                # acceptable (precondition for required=False semantics).
                if self._budget_consumed > self.config.budget_usd:
                    raise BudgetExceededError(
                        f"Budget {self._budget_consumed:.2f} > {self.config.budget_usd:.2f}",
                        block_name=tr.block_name,
                    )

        final_output = ctx.outputs.get("final", ctx.outputs)
        return ExecutionResult(
            success=True,
            final_output=final_output,
            block_results=results,
            total_duration_seconds=time.monotonic() - start,
            total_cost_usd=self._budget_consumed,
        )

    # ── Resolution & Sorting ─────────────────────────────────────────────

    def _resolve_manifest(self, recipe: AgentDefinition) -> AgentManifest:
        """Resolve recipe into a manifest with validated block refs."""
        blocks = [BlockRef(name=b) for b in recipe.blocks]
        inputs: dict[str, Any] = {}
        for v in recipe.variables:
            if v.source == "literal":
                resolved = v.value
            elif v.source == "env":
                resolved = os.getenv(v.name, v.default)
            else:
                resolved = v.default
            if resolved is None:
                if v.required:
                    raise ValueError(
                        f"Required variable '{v.name}' (source={v.source!r}) resolved to None"
                    )
                # required=False honoured: an unresolved optional binding is
                # skipped, and that is fine — it is not bound to None.
                logger.debug(f"Optional variable '{v.name}' unresolved; skipping binding")
                continue
            inputs[v.name] = resolved
        return AgentManifest(
            name=recipe.name,
            blocks=blocks,
            inputs=inputs,
            style_guides=recipe.style_guides,
        )

    @staticmethod
    def _topological_sort(manifest: AgentManifest) -> list[list[BlockRef]]:
        """Kahn's algorithm: return blocks grouped by tiers (parallelizable depth)."""
        refs = manifest.blocks
        if not refs:
            return []
        # Build adjacency from named refs (no metadata → linear order)
        in_degree: dict[str, int] = {r.name: 0 for r in refs}
        adj: dict[str, list[str]] = {r.name: [] for r in refs}
        name_to_ref: dict[str, BlockRef] = {r.name: r for r in refs}
        for i, r in enumerate(refs):
            # Default linear dependency: each block depends on previous unless explicitly stated
            if i > 0:
                prev = refs[i - 1].name
                if r.condition:
                    continue  # conditional handled at runtime
                in_degree[r.name] = in_degree.get(r.name, 0) + 1
                adj.setdefault(prev, []).append(r.name)

        queue = deque([n for n, d in in_degree.items() if d == 0])
        tiers: list[list[str]] = []
        visited: set[str] = set()
        while queue:
            tier = list(queue)
            tiers.append(tier)
            queue.clear()
            for node in tier:
                visited.add(node)
                for nb in adj.get(node, []):
                    in_degree[nb] -= 1
                    if in_degree[nb] == 0 and nb not in visited:
                        queue.append(nb)

        unresolved = [n for n in in_degree if n not in visited]
        if unresolved:
            raise ValueError(f"Cycle detected in block dependencies: {unresolved}")
        # Convert name tiers → BlockRef tiers
        return [[name_to_ref[n] for n in tier] for tier in tiers]

    # ── Tier Execution ──────────────────────────────────────────────────

    async def _execute_tier(
        self,
        tier: list[BlockRef],
        ctx: ExecutionContext,
        recipe: AgentDefinition,
    ) -> list[BlockResult]:
        """Execute all blocks in a tier with bounded concurrency."""
        sem = asyncio.Semaphore(self.config.max_workers)

        async def _run(ref: BlockRef) -> BlockResult:
            async with sem:
                return await self._execute_block(ref, ctx, recipe)

        return await asyncio.gather(*[_run(ref) for ref in tier])

    async def _execute_block(
        self,
        ref: BlockRef,
        ctx: ExecutionContext,
        recipe: AgentDefinition,
    ) -> BlockResult:
        """Execute a single block with retry, timeout, and budget guard."""
        name = ref.name
        t0 = time.monotonic()
        if not self.registry.has_block(name):
            return BlockResult(
                status=BlockStatus.FAILURE,
                block_name=name,
                errors=[f"Block '{name}' not registered"],
                duration_seconds=time.monotonic() - t0,
            )

        # Render prompt / params from Jinja if template present
        params: dict[str, Any] = dict(ctx.inputs)
        params.update(ctx.outputs)
        try:
            # Attempt to resolve a style guide as prompt template
            style_prompt = self._load_style_prompt(name, recipe)
            if style_prompt:
                params["prompt"] = render_template(style_prompt, params)
        except (RuntimeError, OSError, ValueError, ImportError) as exc:  # catch: NARROWED
            logger.debug(f"Style render skipped for {name}: {exc}")

        # Budget guard: warn at the global threshold, and again when the
        # remaining budget drops below one per-block allocation (the
        # per-block warning threshold the comment above always described
        # but the code never implemented).
        block_alloc = self.config.budget_usd / max(len(recipe.blocks), 1)
        remaining = self.config.budget_usd - self._budget_consumed
        if self._budget_consumed > self.config.budget_usd * self.config.budget_warning:
            logger.warning(f"Budget warning: {self._budget_consumed:.2f} USD consumed")
        elif remaining < block_alloc:
            logger.warning(
                f"Budget nearly exhausted: {remaining:.2f} USD remaining "
                f"(below per-block allocation {block_alloc:.2f})"
            )

        # Cache lookup
        cache_key = self._cache_key(recipe, name, params)
        cached = self.cache.get(cache_key)
        if cached is not None:
            ctx.set(name, cached)
            return BlockResult(
                status=BlockStatus.SUCCESS,
                block_name=name,
                output=cached,
                duration_seconds=time.monotonic() - t0,
            )

        # Execute with timeout and optional tenacity. Every failure mode
        # is converted into a FAILURE BlockResult rather than raised, so a
        # crashing block cannot unwind its tier or destroy sibling results
        # (block isolation, salvaged from the retired BaseBlock.run()).
        try:
            result = await asyncio.wait_for(
                self._run_with_retry(name, params),
                timeout=self.config.timeout_seconds,
            )
        except TimeoutError:
            return BlockResult(
                status=BlockStatus.FAILURE,
                block_name=name,
                errors=[f"Block '{name}' timed out after {self.config.timeout_seconds}s"],
                duration_seconds=time.monotonic() - t0,
            )
        except MemoryError:
            raise  # never swallowed — propagates by design
        except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional — block isolation
            logger.error(f"Block '{name}' crashed: {exc}")
            return BlockResult(
                status=BlockStatus.FAILURE,
                block_name=name,
                errors=[f"{type(exc).__name__}: {exc}"],
                duration_seconds=time.monotonic() - t0,
            )

        # Store result
        out_name = ref.output_as or name
        ctx.set(out_name, result)
        try:
            self.cache.set(cache_key, result)
        except (OSError, TypeError, ValueError) as exc:  # catch: NARROWED
            logger.warning(f"Cache write failed for block '{name}': {exc}")
        return BlockResult(
            status=BlockStatus.SUCCESS,
            block_name=name,
            output=result,
            duration_seconds=time.monotonic() - t0,
        )

    async def _run_with_retry(self, name: str, params: dict[str, Any]) -> Any:
        """Run block with optional tenacity retry."""
        if not _HAS_TENACITY:
            return await self._invoke_block(name, params)

        attempt = 0
        for retry in Retrying(
            stop=stop_after_attempt(self.config.max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        ):
            with retry:
                attempt += 1
                return await self._invoke_block(name, params)
        return None  # unreachable

    async def _invoke_block(self, name: str, params: dict[str, Any]) -> Any:
        """Low-level block invocation.

        For Python blocks, the callable may be sync or async.  For blocks
        returning coroutines we await them; for sync blocks we run in the
        default executor to avoid blocking the event loop.

        Schema validation is applied before invocation so malformed
        parameters from upstream phases produce clean validation errors
        rather than cryptic crashes inside the block. The same argument
        applies in the output direction: a wrong-shaped return value is
        ``ctx.set()`` and consumed by downstream blocks, so it is checked
        against the block's return annotation when one is declarable.
        """
        block = self.registry.get_python(name)

        # ── Schema validation (v13.14.6) ────────────────────────────────
        try:
            import jsonschema

            from .mcp_exposure import _build_input_schema, _build_output_schema
        except ImportError as exc:
            logger.warning(f"Block '{name}' schema validation unavailable: {exc}")
            jsonschema = None  # type: ignore[assignment]

        if jsonschema is not None:
            try:
                schema = _build_input_schema(block)
                jsonschema.validate(instance=params, schema=schema)
            except (ValueError, RuntimeError) as exc:
                logger.warning(f"Block '{name}' input schema validation unavailable: {exc}")
            except jsonschema.ValidationError as exc:
                raise ValueError(
                    f"Block '{name}' parameter validation failed: {exc.message}"
                ) from exc

        if asyncio.iscoroutinefunction(block):
            result = await block(params)
        else:
            # Sync callable — offload to thread if needed
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, block, params)

        # ── Output contract validation ──────────────────────────────────
        if jsonschema is not None:
            output_schema = _build_output_schema(block)
            if output_schema is not None:
                try:
                    jsonschema.validate(instance=result, schema=output_schema)
                except jsonschema.ValidationError as exc:
                    raise ValueError(
                        f"Block '{name}' output validation failed: {exc.message}"
                    ) from exc
        return result

    # ── Helpers ─────────────────────────────────────────────────────────

    def _load_style_prompt(self, block_name: str, recipe: AgentDefinition) -> str | None:
        """Load the first matching style guide as a Jinja template."""
        for sg in recipe.style_guides:
            p = Path(sg)
            if not p.exists():
                continue
            try:
                raw = p.read_text(encoding="utf-8")
                if block_name in raw or "{{" in raw:
                    return raw
            except OSError as exc:
                logger.warning(
                    "Cannot read candidate block file %s (%s); skipping it, so block %r "
                    "may resolve to a different file or not at all.",
                    p,
                    exc,
                    block_name,
                )
                continue
        return None

    def _cache_key(self, recipe: AgentDefinition, block_name: str, params: dict[str, Any]) -> str:
        """Deterministic SHA-256 cache key."""
        hasher = hashlib.sha256()
        hasher.update(recipe.name.encode())
        hasher.update(str(recipe.version).encode())
        hasher.update(block_name.encode())
        hasher.update(self.config.schema_version.encode())
        for sg in recipe.style_guides:
            hasher.update(sg.encode())
        for k, v in sorted(params.items()):
            hasher.update(f"{k}={v}".encode())
        return hasher.hexdigest()
