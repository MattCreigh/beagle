"""Context compaction trigger: monitoring and status signalling.

AUTO-GENERATED from context_compaction_hook.py decomposition — DO NOT HAND-EDIT.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from beagle.config.config import get_config

from ..utils.atomic import atomic_write_text
from .checkpoint import CompactionCheckpoint

logger = logging.getLogger("Beagle.context.trigger")

_monitor: ContextMonitor | None = None

_CONSTRAINT_EXTRACTOR = None
_constraint_extractor_lock = threading.Lock()


def _get_constraint_extractor():
    """Lazily initialize constraint extractor to avoid import issues."""
    global _CONSTRAINT_EXTRACTOR
    if _CONSTRAINT_EXTRACTOR is not None:
        return _CONSTRAINT_EXTRACTOR
    with _constraint_extractor_lock:
        if _CONSTRAINT_EXTRACTOR is None:
            try:
                from beagle.infrastructure.constraint_extractor import (
                    create_extractor,
                )

                _CONSTRAINT_EXTRACTOR = create_extractor(project=str(Path.cwd()))
            except ImportError as e:
                logger.debug(f"Constraint extractor not available: {e}")
    return _CONSTRAINT_EXTRACTOR


@dataclass
class ContextStatus:
    """Current context window status."""

    used_tokens: int
    max_tokens: int
    percentage: float
    warning_level: str  # "ok", "warning", "compact", "critical"

    @property
    def remaining_tokens(self) -> int:
        """Return the number of tokens still available in the context window."""
        return self.max_tokens - self.used_tokens

    @property
    def should_pre_compact(self) -> bool:
        """Return True if Beagle should fold context BEFORE goose's internal compaction.

        v13.15.6: This fires at a lower threshold (default 0.58) than goose's
        own compaction. Beagle gets to build a controlled TurboQuant fold and
        deliver the rehydration prompt while the LLM still has working memory.
        This prevents the "goose compacts → agent wakes up amnesic" cycle.
        """
        return self.percentage >= get_config().context_threshold.pre_compact

    @property
    def should_compact(self) -> bool:
        """Return True if context usage exceeds the compaction threshold.

        v13.16: This is a DETERMINISTIC harness decision, never a per-tick
        model judgment.  Sub-threshold (< effective_compact) always returns
        False — no "maybe fold now" ambiguity.  The model should NOT be
        asked to decide whether to fold; the harness fires compaction only
        at/above threshold.  (GB-4, root faults 2, 3, 5)

        Honours the ``GOOSE_AUTO_COMPACT_THRESHOLD`` env-var override via
        :attr:`ContextThresholdConfig.effective_compact` (per project doctrine
        in CLAUDE.md — env var is the canonical knob, TOML is fallback).
        """
        return self.percentage >= get_config().context_threshold.effective_compact

    @property
    def is_critical(self) -> bool:
        """Return True if context usage exceeds the critical threshold."""
        return self.percentage >= get_config().context_threshold.critical


@dataclass
class ContextMonitor:
    """Monitors context usage and provides proactive compaction hooks.

    Usage in self-improvement loop:
        monitor = ContextMonitor(total_iterations=25)

        for iteration in range(1, 26):
            # Check before starting work
            status = monitor.check_before_work(iteration, "iteration_{iteration}")

            if status.is_critical:
                # Compaction URGENT - save state and compact
                monitor.save_checkpoint()
                # Goose will auto-compact when percentage > 70%

            # ... do work ...

            # Mark safe compaction point after each iteration
            monitor.mark_safe_point("completed_iteration_{iteration}")

            if status.should_compact:
                monitor.save_checkpoint()
                logger.info("[COMPACT] Context at {status.percentage:.1%} - safe to compact now")

    ENHANCED v12.1:
    - Constraint extraction from session messages
    - Knowledge extraction for semantic persistence
    """

    def __init__(
        self,
        total_iterations: int = 25,
        checkpoint_dir: Path | None = None,
        on_compact: Callable[[], None] | None = None,
        session_id: str = "",
        extract_constraints: bool = True,
        extract_knowledge: bool = True,
    ):
        self.total_iterations = total_iterations
        self.current_iteration = 0
        self.current_task = "initializing"
        self.checkpoint_dir = (
            checkpoint_dir or Path.home() / ".cache" / "goose" / "compaction_checkpoints"
        )
        self.on_compact = on_compact
        self.session_id = session_id or datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        self.extract_constraints = extract_constraints
        self.extract_knowledge = extract_knowledge

        self._files_modified: list[str] = []
        self._pending_commits: list[str] = []
        self._safe_points: list[str] = []
        self._session_messages: list[dict[str, str]] = []  # For extraction

        # v13.7.0: Tool routing state — populated by register_orchestrator_state()
        self._tool_preferences: dict[str, str] = {}
        self._model_overrides: dict[str, str] = {}
        self._fallback_directives: list[str] = []
        self._tool_failure_history: list[dict] = []

        # Adaptive chunking: track compaction frequency to instruct agent
        # to work in progressively smaller units
        self._compaction_count: int = 0
        self._compaction_history: list[dict[str, Any]] = []

        # Load previous compaction state if recent (within 1 hour)
        try:
            _state_path = Path.home() / ".beagle" / "compaction_state.json"
            if _state_path.exists():
                _state = json.loads(_state_path.read_text())
                # wall-clock-ok: last_compaction is a PERSISTED timestamp from
                # compaction_state.json, possibly written by a previous
                # process. time.monotonic() is not comparable across restarts,
                # so wall clock is the correct clock here. (Read into a named
                # variable: the aeca-walltime-for-interval rule flags inline
                # `time.time() - x` subtractions, which are for durations.)
                _now_wall = time.time()
                if _now_wall - _state.get("last_compaction", 0) < 3600:
                    self._compaction_count = _state.get("compaction_count", 0)
                    self._compaction_history = _state.get("history", [])
                    logger.info(
                        f"[ContextMonitor] Loaded compaction state: count={self._compaction_count}"
                    )
        except (OSError, ValueError, RuntimeError) as exc:  # catch: NARROWED
            logger.warning(
                "Cannot load persisted compaction state (%s); the compaction count "
                "restarts at 0, so adaptive chunking loses its history.",
                exc,
            )

    def record_compaction(self, context_size: int, node_name: str = "") -> None:
        """Record a compaction event for adaptive chunking.

        Each compaction increments the counter and persists to disk.
        The rehydration system uses this count to generate progressively
        stricter chunking instructions so the agent learns to work in
        smaller units.
        """
        self._compaction_count += 1
        self._compaction_history.append(
            {
                "count": self._compaction_count,
                "context_size_chars": context_size,
                "node_name": node_name,
                "timestamp": time.time(),
            }
        )

        # Persist to disk so it survives process restarts
        try:
            state_path = Path.home() / ".beagle" / "compaction_state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "compaction_count": self._compaction_count,
                "last_compaction": time.time(),
                "history": self._compaction_history[-10:],
            }
            # Atomic write: rehydration reads this file after restarts; a
            # partial write would corrupt the persisted fold state.
            atomic_write_text(state_path, json.dumps(state, indent=2), mode=0o644)
        except (OSError, RuntimeError, ValueError):  # catch: NARROWED
            logger.debug("Failed to persist compaction state", exc_info=True)

        logger.info(
            f"[ContextMonitor] Compaction #{self._compaction_count} recorded: "
            f"context_size={context_size}, node={node_name}"
        )

    def fold_and_surrender(self, node_name: str = "") -> str | None:
        """v13.16.3: Pre-emptive sovereignty fold — Beagle seizes control before Goose.

        Called by execute_goose_node when context usage reaches or exceeds
        HARD_SOVEREIGN_THRESHOLD (0.80).  This:
          1. Saves a checkpoint with the current session state
          2. Builds the TurboQuant fold from accumulated context
          3. Writes the rehydration sidecar
          4. Returns the fold_id for the rehydration pointer

        The caller must NOT spawn a subprocess that will hit context limits
        after this returns — it should instead emit a rehydration instruction
        to the subprocess that maps to this fold.

        Args:
            node_name: Name of the node that triggered the fold.

        Returns:
            fold_id string (12 hex chars) if fold was successful, None
            if folding was skipped (no accumulated context or system error).

        """
        import json

        # Increment the compaction counter for adaptive chunking
        self._compaction_count += 1
        self._compaction_history.append(
            {
                "count": self._compaction_count,
                "context_size_chars": 0,
                "node_name": node_name,
                "timestamp": time.time(),
                "type": "sovereignty_fold",
            }
        )

        # Persist compaction state to disk
        try:
            state_path = Path.home() / ".beagle" / "compaction_state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "compaction_count": self._compaction_count,
                "last_compaction": time.time(),
                "last_fold_type": "sovereignty",
                "history": self._compaction_history[-10:],
            }
            # Atomic write, same rationale as the compaction-state site above.
            atomic_write_text(state_path, json.dumps(state, indent=2), mode=0o644)
        except (OSError, RuntimeError, ValueError):  # catch: NARROWED
            logger.debug("Failed to persist sovereignty-fold state", exc_info=True)

        # Build accumulated context for fold input
        accumulated = self._build_accumulated_context()
        if not accumulated:
            logger.debug("[SovereigntyFold] No accumulated context — skipping fold")
            return None

        try:
            from .context_integration import get_context_integration

            integration = get_context_integration()
            # Apply running-loop guard (same pattern as save_checkpoint)
            try:
                asyncio.get_running_loop()
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as pool:
                    pool.submit(
                        asyncio.run,
                        integration.enhanced_context_fold(
                            data_to_fold=accumulated,
                            fold_mode="turboquant",
                        ),
                    ).result(timeout=30)
            except RuntimeError:
                asyncio.run(
                    integration.enhanced_context_fold(
                        data_to_fold=accumulated,
                        fold_mode="turboquant",
                    )
                )

            # Find the most recent fold_id from the sidecar directory.
            # Resolved via get_data_root() so readers and writers agree:
            # the writers (compressed_store / context_integration) anchor
            # there too — Path.home() bypassed BEAGLE_DATA_ROOT overrides.
            from beagle.config.paths import get_data_root

            sidecar_dir = get_data_root() / "context_folds"
            sidecars = sorted(
                sidecar_dir.glob("*_manifest.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if sidecars:
                with open(sidecars[0], encoding="utf-8") as sf:
                    sidecar = json.load(sf)
                fold_id = sidecar.get("fold_id", "")
                if fold_id:
                    logger.info(
                        f"[SovereigntyFold] Beagle seized context at "
                        f"{self._compaction_count}x compactions — "
                        f"fold_id={fold_id}, node={node_name}"
                    )
                    return str(fold_id) if fold_id is not None else None
        except ImportError:
            logger.debug("[SovereigntyFold] context_integration not available")
        except (RuntimeError, OSError, ValueError) as e:  # catch: NARROWED
            logger.warning(f"[SovereigntyFold] TurboQuant fold failed: {e}")

        return None

    def mark_fold_complete(self, node_name: str, output_len: int) -> None:
        """v13.16.3: Mark the post-execution context baseline as clean.

        Called by execute_goose_node AFTER the subprocess returns successfully.
        Registers the node's memory-store operation so the context baseline
        reflects the just-completed work (preventing the next node from
        immediately hitting the sovereignty threshold due to stale metrics).

        Args:
            node_name: Name of the completed node.
            output_len: Length of the node's output in characters.

        """
        self.current_iteration += 1
        self._safe_points.append(f"completed_{node_name}")
        logger.debug(
            f"[ContextMonitor] Node {node_name} completed — "
            f"output={output_len} chars, baseline updated (iteration {self.current_iteration})"
        )

    def _get_context_usage(self) -> tuple[int, int]:
        """Estimate context usage from the best available source.

        Source priority:
        1. read_session_usage() — the live goose CLI session's total_tokens
           over the declared GOOSE_CONTEXT_LIMIT.  This is the number the
           harness itself displays.
        2. Context reporter file (~/.beagle/context_report.json) — written
           by the goose agent via system instruction after each tool call.
           Contains actual context utilization from goose's runtime.
        3. GOOSE_CONTEXT_PERCENTAGE env var — set by goose runtime in
           headless mode (rarely available).
        4. Iteration-count heuristic — last resort, rough estimate.

        Returns:
            Tuple of (used_tokens, max_tokens)

        """
        # Each source that cannot answer records why. The chain is expected to
        # fall through — an unavailable source is not itself a fault — but if
        # every source fails the pessimistic branch at the end needs to say what
        # went wrong, otherwise the operator sees only "all sources unavailable".
        source_failures: list[str] = []

        # Source 1: the live goose session store (most accurate).
        try:
            from .session_usage import read_session_usage

            usage = read_session_usage()
            if usage is not None:
                return usage.used_tokens, usage.max_tokens
            source_failures.append("read_session_usage() returned None")
        except ImportError as exc:
            source_failures.append(f"session_usage import: {exc}")

        # Source 2: Context reporter file
        try:
            from .context_reporter import get_context_usage_from_report

            report_usage = get_context_usage_from_report()
            if report_usage is not None:
                return report_usage
        except ImportError as exc:
            source_failures.append(f"context_reporter import: {exc}")

        # Source 3: Environment variable.  No released goose version sets
        # GOOSE_CONTEXT_PERCENTAGE or GOOSE_CONTEXT_MAX, so this branch is an
        # override, not a primary source.
        percentage_str = os.environ.get("GOOSE_CONTEXT_PERCENTAGE", "")
        max_tokens_str = os.environ.get(
            "GOOSE_CONTEXT_MAX", str(get_config().context_threshold.max_tokens)
        )

        max_tokens = int(max_tokens_str)

        if percentage_str:
            try:
                percentage = float(percentage_str.rstrip("%")) / 100
                used = int(max_tokens * percentage)
                return used, max_tokens
            except ValueError as exc:
                source_failures.append(f"GOOSE_CONTEXT_PERCENTAGE={percentage_str!r}: {exc}")

        # Source 2.5: cost_tracker context_status.current_tokens (v13.14.6)
        # ContextAwareCostTracker tracks tokens currently in context via
        # record_usage(), which counts tokens added this turn and decays
        # expired tokens. This is more accurate than total_tokens for
        # context window monitoring.
        try:
            from beagle.cost_tracker import get_cost_tracker

            tracker = get_cost_tracker()
            if tracker is not None and tracker.context_status.current_tokens > 0:
                return tracker.context_status.current_tokens, max_tokens
        except (
            ImportError,
            RuntimeError,
            OSError,
            AttributeError,
        ) as exc:  # catch: NARROWED  # RATIONALE=four-tuple: lazy loader import, attribute lookup on token_tracker, runtime guard failure, OS path/file errors
            source_failures.append(f"cost_tracker: {exc}")

        # Source 3: Iteration-count heuristic (fallback)
        # Returning (0, max_tokens) caused the monitor to never detect high usage.
        if self.current_iteration > 0:
            estimated = self.current_iteration * get_config().context_threshold.tokens_per_iteration
            return min(estimated, max_tokens), max_tokens

        # FINAL FALLBACK: Pessimistic estimate — never return 0%.
        # If all measurement sources fail, assume 70% (at the trigger line).
        # Rationale (af5fa02, "harden compaction triggers against silent
        # session stops"): a false-positive compaction is recoverable; a
        # false-negative (returning < trigger when actually ≥ trigger) means
        # the session runs out of context silently with no recovery path.
        # The "infinite-compaction loop" concern is addressed by the
        # one-shot guard in check_and_fold_context, not by sitting below
        # the trigger — that path is what disables the safety net.
        pessimistic = int(max_tokens * 0.70)
        logger.warning(
            "All context-usage sources unavailable (%s) — returning pessimistic "
            "estimate of %d/%d (70%%). A false-positive compaction is recoverable; "
            "a false-negative is not.",
            "; ".join(source_failures) or "no source reported a reason",
            pessimistic,
            max_tokens,
        )
        return pessimistic, max_tokens

    def get_status(self) -> ContextStatus:
        """Get current context window status."""
        used, max_tokens = self._get_context_usage()

        # If we can't determine actual usage, estimate based on iteration
        if used == 0 and self.current_iteration > 0:
            used = self.current_iteration * get_config().context_threshold.tokens_per_iteration

        percentage = used / max_tokens if max_tokens > 0 else 0

        thresholds = get_config().context_threshold
        if percentage >= thresholds.critical:
            level = "critical"
        elif percentage >= thresholds.effective_compact:
            level = "compact"
        elif percentage >= thresholds.pre_compact:
            level = "pre_compact"  # v13.15.5: Beagle-fold-first zone
        elif percentage >= thresholds.warning:
            level = "warning"
        else:
            level = "ok"

        return ContextStatus(
            used_tokens=used,
            max_tokens=max_tokens,
            percentage=percentage,
            warning_level=level,
        )

    def check_before_work(
        self,
        iteration: int,
        task_name: str,
    ) -> ContextStatus:
        """Check context status before starting work on a task.

        Call this at the START of each iteration to get awareness
        of whether compaction is imminent.

        Args:
            iteration: Current iteration number
            task_name: Name of task about to be started

        Returns:
            ContextStatus with usage info and recommendations

        """
        self.current_iteration = iteration
        self.current_task = task_name

        status = self.get_status()

        # Emit telemetry for OTel observability
        self._emit_telemetry(status, iteration, task_name)

        return status

    def _emit_telemetry(
        self,
        status: ContextStatus,
        iteration: int,
        task: str,
    ) -> None:
        """Emit OTel-compliant telemetry for context usage."""
        # This would integrate with OpenTelemetry if available
        # For now, emit structured log
        timestamp = datetime.now(UTC).isoformat()
        log_entry = {
            "timestamp": timestamp,
            "level": "INFO",
            "component": "ContextMonitor",
            "iteration": iteration,
            "task": task,
            "context_used_tokens": status.used_tokens,
            "context_max_tokens": status.max_tokens,
            "context_percentage": f"{status.percentage:.2%}",
            "warning_level": status.warning_level,
        }

        # Write to monitoring log
        log_path = self.checkpoint_dir / "context_telemetry.jsonl"
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry) + "\n")

    def mark_safe_point(self, point_name: str) -> None:
        """Mark that we've reached a safe point for compaction.

        Safe points are boundaries where work is complete and
        state has been saved - ideal for compaction triggers.
        """
        self._safe_points.append(point_name)

        # Update iteration tracking
        self._safe_points[-1] if self._safe_points else ""
        if "iteration" in point_name.lower():
            # Extract iteration number from point name
            try:
                num = int("".join(filter(str.isdigit, point_name)))
                self.current_iteration = num
            except ValueError as exc:
                logger.warning(
                    "Safe point %r names an iteration but carries no parseable number "
                    "(%s); current_iteration stays at %d.",
                    point_name,
                    exc,
                    self.current_iteration,
                )

    def register_file_modified(self, path: str | Path) -> None:
        """Register a file modification for checkpoint tracking."""
        self._files_modified.append(str(path))

    def register_commit(self, commit_hash: str) -> None:
        """Register a commit for checkpoint tracking."""
        self._pending_commits.append(commit_hash)

    def register_message(self, role: str, content: str) -> None:
        """Register a message for constraint extraction.

        Messages are collected during the session and processed
        during save_checkpoint() to extract constraints.

        Args:
            role: Message role (user/assistant)
            content: Message content

        """
        self._session_messages.append({"role": role, "content": content})

    def register_orchestrator_state(self, orchestrator: Any) -> None:
        """Capture tool routing state from the orchestrator for checkpoint persistence.

        Called by the orchestrator loop so that save_checkpoint() can include
        tool preferences, model overrides, fallback directives, and tool
        failure history in the checkpoint — enabling full rehydration continuity.

        Args:
            orchestrator: DAGOrchestrator instance (or any object with
                state, config, and _node_specs attributes).

        """
        # Tool preferences: non-default executor assignments per node
        self._tool_preferences = {
            spec.get("name", ""): spec["executor"]
            for spec in getattr(orchestrator, "_node_specs", [])
            if spec.get("executor") and spec.get("executor") != "goose"
        }

        # Model overrides: per-node model assignments
        self._model_overrides = {
            spec.get("name", ""): spec["model"]
            for spec in getattr(orchestrator, "_node_specs", [])
            if spec.get("model")
        }

        # Fallback chain from config
        self._fallback_directives = list(
            getattr(
                getattr(orchestrator, "config", None),
                "fallback_chain",
                [],
            )
        )

        # Tool failure history from agent state
        state = orchestrator.state
        if hasattr(state, "tool_failure_history"):
            self._tool_failure_history = list(state.tool_failure_history[-10:])
        elif isinstance(state, dict):
            self._tool_failure_history = list(state.get("tool_failure_history", [])[-10:])

    def save_checkpoint(self) -> Path:
        """Save current state to checkpoint file.

        This should be called BEFORE compaction to preserve state
        that will be needed for resume after context is cleared.

        Also extracts and persists:
        - User constraints from session messages
        - Knowledge entries from assistant responses
        - Session episodes for episodic memory (Phase 3)
        - Memory trace: semantic summary of reasoning chain (H-MEM v13)
        - VFS archival: large tool outputs archived (H-MEM v13)

        Returns:
            Path to saved checkpoint file

        """
        # Extract constraints from session messages before compaction
        extracted_constraints = self._extract_constraints()

        # Extract knowledge from session messages
        extracted_knowledge = self._extract_knowledge()

        # Archive session episodes for episodic memory (Phase 3)
        session_episodes = self._archive_session_episodes()

        # H-MEM v13: Generate memory trace (semantic summary of reasoning chain)
        memory_trace = self._generate_memory_trace()

        # H-MEM v13: Archive large tool outputs to VFS
        archived_outputs = self._archive_large_outputs()

        checkpoint = CompactionCheckpoint(
            timestamp=datetime.now(UTC),
            current_task=self.current_task,
            iteration=self.current_iteration,
            total_iterations=self.total_iterations,
            files_modified=self._files_modified.copy(),
            pending_commits=self._pending_commits.copy(),
            next_steps=self._get_next_steps(),
            extracted_constraints=[
                c.to_json() if hasattr(c, "to_json") else c for c in extracted_constraints
            ],
            extracted_knowledge=[
                k.to_json() if hasattr(k, "to_json") else k for k in extracted_knowledge
            ],
            session_episodes=[
                e.to_json() if hasattr(e, "to_json") else e for e in session_episodes
            ],
            session_id=self.session_id,
            memory_trace=memory_trace,
            archived_outputs=archived_outputs,
            # v13.7.0: Tool routing state for rehydration continuity
            tool_preferences=self._tool_preferences.copy(),
            model_overrides=self._model_overrides.copy(),
            fallback_directives=self._fallback_directives.copy(),
            tool_failure_history=self._tool_failure_history.copy(),
            # Adaptive chunking state
            compaction_count=self._compaction_count,
            compaction_history=self._compaction_history[-10:],
        )

        self.checkpoint_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        checkpoint_path = self.checkpoint_dir / f"checkpoint_{self.current_iteration:02d}.json"

        with open(checkpoint_path, "w", encoding="utf-8") as f:
            f.write(checkpoint.to_json())
        # Restrict to owner-only — checkpoints contain session data
        os.chmod(checkpoint_path, 0o600)

        # ── Mark RAG as stale after saving compaction checkpoint ──
        # Context is about to be truncated — the next hydration cycle
        # must reingest to ensure fresh RAG data.
        try:
            from .rag_staleness import get_staleness_tracker

            staleness = get_staleness_tracker()
            if not staleness.is_stale:
                staleness.mark_stale(reason="context_compaction")
                logger.info(
                    "[CompactionHook] RAG marked stale — next hydration "
                    "will trigger hot-swap reingestion"
                )
        except ImportError:
            logger.debug("[CompactionHook] rag_staleness module not available")
        except (RuntimeError, ValueError, OSError) as e:  # catch: NARROWED
            logger.debug(f"[CompactionHook] RAG staleness mark failed: {e}")

        # ── TurboQuant context fold ──
        # Compress accumulated context into a structural skeleton + 3-bit
        # embedding sidecar. The fold_id is stored on the checkpoint for
        # rehydration to emit a <FoldPointer> instead of raw dump.
        try:
            accumulated = self._build_accumulated_context()
            if accumulated:
                from .context_integration import get_context_integration

                integration = get_context_integration()
                # Apply running-loop guard (same pattern as _build_rag_section)
                try:
                    asyncio.get_running_loop()
                    # Running loop exists — use thread pool
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        pool.submit(
                            asyncio.run,
                            integration.enhanced_context_fold(
                                data_to_fold=accumulated,
                                fold_mode="turboquant",
                            ),
                        ).result(timeout=30)
                except RuntimeError:
                    # No running loop — safe to call asyncio.run() directly
                    asyncio.run(
                        integration.enhanced_context_fold(
                            data_to_fold=accumulated,
                            fold_mode="turboquant",
                        )
                    )
                # enhanced_context_fold returns str; the sidecar stores
                # the TurboFoldResult. We need the fold_id from the most
                # recent sidecar written by _apply_turboquant_fold.
                # get_data_root(): see the sibling branch above — readers
                # and writers must anchor to the same root.
                from beagle.config.paths import get_data_root

                sidecar_dir = get_data_root() / "context_folds"
                sidecars = sorted(
                    sidecar_dir.glob("*_manifest.json"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if sidecars:
                    with open(sidecars[0], encoding="utf-8") as sf:
                        sidecar = json.load(sf)
                    fold_id = sidecar.get("fold_id", "")
                    if fold_id:
                        checkpoint.fold_id = fold_id
                        # Rewrite checkpoint with fold_id included
                        with open(checkpoint_path, "w", encoding="utf-8") as f:
                            f.write(checkpoint.to_json())
                        os.chmod(checkpoint_path, 0o600)
                        logger.info(
                            f"[CompactionHook] TurboQuant fold #{fold_id} "
                            f"applied to checkpoint {checkpoint_path.name}"
                        )
        except ImportError:
            logger.debug("[CompactionHook] context_integration not available for fold")
        except (RuntimeError, OSError, ValueError) as e:  # catch: NARROWED
            logger.debug(f"[CompactionHook] TurboQuant fold skipped: {e}")

        # v0.3.0: Clear pre-compaction metadata to prevent memory leak
        # The data is persisted to the checkpoint file; keeping it in memory
        # causes unbounded growth across compaction cycles.
        self._session_messages.clear()
        self._files_modified.clear()
        self._pending_commits.clear()

        return checkpoint_path

    def _build_accumulated_context(self) -> str:
        """Build accumulated context from session messages for fold input.

        Concatenates session messages into a single text blob, structured
        so the TurboQuant fold can extract a meaningful skeleton.

        Sections are separated by clear delimiters so _build_skeleton can
        identify structural boundaries.

        Returns:
            Multi-section text suitable for enhanced_context_fold(),
            or empty string if no session messages exist.

        """
        if not self._session_messages:
            return ""

        sections: list[str] = []

        # Header with session metadata
        sections.append(
            f"=== COMPACTION FOLD INPUT ===\n"
            f"session_id: {self.session_id}\n"
            f"iteration: {self.current_iteration}/{self.total_iterations}\n"
            f"compaction_count: {self._compaction_count}\n"
            f"files_modified: {len(self._files_modified)}\n"
            f"=== END HEADER ===\n"
        )

        # Message transcript
        sections.append("=== SESSION TRANSCRIPT ===")
        for msg in self._session_messages[-200:]:  # Last 200 messages only
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            # Truncate very long messages to keep fold input manageable
            if len(content) > 4000:
                content = content[:4000] + "\n... [truncated for fold]"
            sections.append(f"[{role}]: {content}")

        sections.append("=== END TRANSCRIPT ===")

        # Files modified summary
        if self._files_modified:
            sections.append("=== FILES MODIFIED ===")
            for f in self._files_modified[-50:]:
                sections.append(f"  - {f}")
            sections.append("=== END FILES MODIFIED ===")

        return "\n".join(sections)

    def _get_next_steps(self) -> list[str]:
        """Generate next steps for resume after compaction."""
        remaining = self.total_iterations - self.current_iteration
        steps = []

        if self.current_iteration < self.total_iterations:
            steps.append(f"Continue from iteration {self.current_iteration + 1}")
            steps.append(f"Remaining iterations: {remaining}")

        if self._files_modified:
            files_str = ", ".join(self._files_modified[-3:])
            steps.append(f"Verify last modified files: {files_str}")

        if self._pending_commits:
            steps.append(f"Last commit: {self._pending_commits[-1]}")

        return steps

    def _extract_constraints(self) -> list:
        """Extract constraints from session messages before compaction.

        Uses ConstraintExtractor to identify and persist user constraints
        from the conversation history. This ensures constraints survive  # noqa: E402
        the compaction boundary.

        Returns:
            List of extracted Constraint objects

        """
        if not self.extract_constraints or not self._session_messages:
            return []

        extractor = _get_constraint_extractor()
        if extractor is None:
            logger.debug("Constraint extractor not available, skipping extraction")
            return []

        try:
            # Extract constraints from session messages
            constraints = extractor.extract_from_session(
                messages=self._session_messages,
                session_id=self.session_id,
            )

            if constraints:
                logger.info(f"Extracted {len(constraints)} constraints before compaction")
                for c in constraints[:5]:  # Log first 5
                    logger.debug(f"  - {c}")

            return cast(list, constraints)

        except (TypeError, ValueError, AttributeError) as e:  # catch: NARROWED
            logger.error(f"Constraint extraction failed: {e}")
            return []

    def _extract_knowledge(self) -> list:
        """Extract knowledge from session messages before compaction.

        Uses KnowledgeExtractor to identify and persist learned knowledge
        from assistant responses. This ensures insights survive compaction.  # noqa: E402

        Returns:
            List of extracted KnowledgeEntry objects

        """
        if not self.extract_knowledge or not self._session_messages:
            return []

        try:
            from beagle.config.paths import get_workspace_root
            from beagle.infrastructure.knowledge_extractor import (
                create_extractor,
            )

            extractor = create_extractor(project=str(get_workspace_root()))

            # Extract knowledge from session messages
            knowledge = extractor.extract_for_compaction(
                session_messages=self._session_messages,
                session_id=self.session_id,
            )

            if knowledge:
                logger.info(f"Extracted {len(knowledge)} knowledge entries before compaction")
                for k in knowledge[:5]:  # Log first 5
                    logger.debug(f"  - {k.title}")

            return knowledge

        except (TypeError, ValueError, AttributeError) as e:  # catch: NARROWED
            logger.error(f"Knowledge extraction failed: {e}")
            return []

    def _archive_session_episodes(self) -> list:
        """Archive session messages as episodes for episodic memory.

        Phase 3: Converts session messages into episodic memory entries
        for later retrieval and context reconstruction.

        Returns:
            List of episode dicts

        """
        if not self._session_messages:
            return []

        try:
            from beagle.infrastructure.session_memory import (
                SessionMemory,
            )

            # Create session memory instance
            session_memory = SessionMemory(session_id=self.session_id)

            # Add messages from this session
            for msg in self._session_messages:
                raw_meta: Any = msg.get("metadata", {})
                meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
                session_memory.add_message(
                    role=msg.get("role", "user"),
                    content=msg.get("content", ""),
                    metadata=meta,
                )

            # End current episode to archive it
            episode = session_memory.end_episode(
                metadata={
                    "iteration": self.current_iteration,
                    "task": self.current_task,
                    "checkpoint": True,
                }
            )

            if episode:
                logger.info(f"Archived session episode: {episode.id[:8]}")
                return [episode.to_json()]

            return []

        except (OSError, RuntimeError, ValueError, ImportError) as e:  # catch: NARROWED
            logger.error(f"Session episode archival failed: {e}")
            return []

    def _generate_memory_trace(self) -> str:
        """Generate a semantic summary of the reasoning chain.

        H-MEM v13: Creates a condensed trace of the reasoning process
        that can survive compaction boundaries and be used to reconstruct
        context understanding.

        Returns:
            Memory trace string (semantic summary)

        """
        if not self._session_messages:
            return ""

        # Build reasoning trace from session messages
        trace_lines: list[str] = []
        trace_lines.append("## Memory Trace (Reasoning Summary)")
        trace_lines.append(f"Task: {self.current_task}")
        trace_lines.append(f"Iteration: {self.current_iteration}/{self.total_iterations}")
        trace_lines.append("")

        # Extract key insights from messages
        user_messages = [m for m in self._session_messages if m.get("role") == "user"]
        assistant_messages = [m for m in self._session_messages if m.get("role") == "assistant"]

        trace_lines.append("### Session Overview")
        trace_lines.append(f"- User messages: {len(user_messages)}")
        trace_lines.append(f"- Assistant responses: {len(assistant_messages)}")
        trace_lines.append("")

        # Summarize user intent
        if user_messages:
            trace_lines.append("### User Intent")
            for _i, msg in enumerate(user_messages[:3]):  # First 3 user messages
                content = msg.get("content", "")
                # Extract first line or key phrase
                first_line = content.split("\n")[0][:150]
                if first_line:
                    trace_lines.append(f"- {first_line}")
            trace_lines.append("")

        # Summarize assistant actions
        if assistant_messages:
            trace_lines.append("### Actions Taken")
            for _i, msg in enumerate(assistant_messages[:5]):  # Last 5 assistant messages
                content = msg.get("content", "")
                # Look for action indicators
                if "implemented" in content.lower() or "created" in content.lower():
                    trace_lines.append("- Action: Implementation completed")
                elif "error" in content.lower():
                    trace_lines.append("- Action: Error encountered and handled")
                elif "success" in content.lower():
                    trace_lines.append("- Action: Task completed successfully")
                else:
                    # Summarize first line
                    first_line = content.split("\n")[0][:100]
                    if first_line and not first_line.startswith("<"):
                        trace_lines.append(f"- Action: {first_line}")
            trace_lines.append("")

        # Add files modified summary
        if self._files_modified:
            trace_lines.append("### Files Modified")
            for f in self._files_modified[-5:]:
                trace_lines.append(f"- {f}")
            trace_lines.append("")

        # Add constraints found
        if self._session_messages:
            # Check for constraint-like statements
            constraint_patterns = ["NO ", "NEVER ", "MUST ", "ALWAYS ", "DO NOT "]
            found_constraints = []
            for msg in user_messages:
                content = msg.get("content", "")
                for pattern in constraint_patterns:
                    if pattern in content.upper():
                        # Extract the constraint
                        lines = content.split("\n")
                        for line in lines:
                            if pattern in line.upper():
                                # Truncate to reasonable length
                                constraint = line.strip()[:200]
                                if constraint and constraint not in found_constraints:
                                    found_constraints.append(constraint)

            if found_constraints:
                trace_lines.append("### Constraints Extracted")
                for c in found_constraints[:5]:
                    trace_lines.append(f"- {c}")
                trace_lines.append("")

        trace_lines.append("---")
        return "\n".join(trace_lines)

    def _archive_large_outputs(self) -> dict[str, str]:
        """Archive large outputs to VFS.

        H-MEM v13: Archives any tool outputs stored in session messages
        that exceed the token threshold, replacing them with URI pointers.

        Returns:
            Dict mapping URIs to output keys for retrieval

        """
        from beagle.infrastructure.vfs_archive import (
            get_vfs_archive,
        )

        try:
            archive = get_vfs_archive()
            archived: dict[str, str] = {}

            # Process session messages for large outputs
            for i, msg in enumerate(self._session_messages):
                content = msg.get("content", "")

                # Skip small content
                if len(content) < 8000:  # ~2000 tokens
                    continue

                # Check if this is a tool output (long content)
                if msg.get("role") == "assistant" and len(content) > 8000:
                    # Archive large output
                    uri = archive.archive(
                        content=content,
                        content_type="tool_output",
                        session_id=self.session_id,
                        workflow_id=self.current_task,
                        tags=["compaction", f"iteration_{self.current_iteration}"],
                        metadata={"message_index": i},
                    )

                    if uri:
                        # Store mapping
                        key = f"output_{i}"
                        archived[uri] = key
                        logger.info(f"Archived large output ({len(content) // 4} tokens) to {uri}")

            return archived

        except (
            OSError,
            RuntimeError,
            ValueError,
            ImportError,
        ) as e:  # catch: NARROWED — archival is best-effort
            logger.warning(f"Failed to archive large outputs: {e}")
            return {}

    @staticmethod
    def load_latest_checkpoint(
        checkpoint_dir: Path | None = None,
    ) -> CompactionCheckpoint | None:
        """Load the most recent checkpoint for resume.

        (``@staticmethod``, not ``@classmethod``: the body never used ``cls``
        — it always constructs :class:`CompactionCheckpoint` directly.)

        Args:
            checkpoint_dir: Directory containing checkpoints

        Returns:
            Most recent checkpoint or None if not found

        """
        checkpoint_dir = (
            checkpoint_dir or Path.home() / ".cache" / "goose" / "compaction_checkpoints"
        )

        if not checkpoint_dir.exists():
            return None

        checkpoints = sorted(checkpoint_dir.glob("checkpoint_*.json"), reverse=True)

        if not checkpoints:
            return None

        with open(checkpoints[0], encoding="utf-8") as f:
            return CompactionCheckpoint.from_json(f.read())


# Context file discovery for post-compaction loading


def get_monitor(total_iterations: int = 25) -> ContextMonitor:
    """Get or create singleton monitor instance."""
    global _monitor
    if _monitor is None:
        _monitor = ContextMonitor(total_iterations=total_iterations)
    return _monitor


def pre_compact_check(iteration: int, task: str) -> ContextStatus:
    """Convenience function for pre-work context check.

    Call at the start of each iteration:
        status = pre_compact_check(6, "refactoring iteration")
        if status.is_critical:
            get_monitor().save_checkpoint()
            # Compaction will happen at 70%
        if status.should_compact:
            logger.info("Context at {status.percentage:.1%}, compact soon")
    """
    monitor = get_monitor()
    return monitor.check_before_work(iteration, task)
