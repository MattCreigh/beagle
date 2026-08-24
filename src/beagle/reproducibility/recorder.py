"""Replay recorder — hooks into workflow execution to record all node inputs.

Subscribes to NodeStarted and NodeInputCaptured events from the event bus
and builds a ReplayManifest over the course of the workflow execution.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from pathlib import Path

from .manifest import ReplayManifest

logger = logging.getLogger("Beagle.reproducibility")


def _default_replay_dir() -> Path:
    """Resolve DEFAULT_REPLAY_DIR against the writable data root.

    Historical default was ``Path('.beagle/replays')`` which was relative to
    CWD at the time of the call. Under tests, library callers, or
    cron-launched daemons, CWD can be anywhere — manifests ended up
    scattered across the filesystem and tests running in shared CWD
    polluted each other. The first fix resolved against the package root
    (``Path(__file__).parent.parent.parent``), which is deterministic but
    resolves to *site-packages itself* under a wheel install — runtime
    state was written into the install tree (observed as
    ``site-packages/.beagle/replays``), defeating the paths.py contract
    that state stays writable and separate from assets.

    :func:`beagle.config.paths.get_data_root` gives both properties:
    deterministic resolution AND a location that remains writable when the
    package is installed read-only.

    Allow override via the BEAGLE_REPLAY_DIR env var so operators can
    redirect (e.g., to a faster tmpfs for benchmarking).
    """
    import os

    override = os.environ.get("BEAGLE_REPLAY_DIR")
    if override:
        return Path(override)
    from ..config.paths import get_data_root

    return get_data_root() / "replays"


# Default storage location for manifests (under get_data_root(), not
# CWD-relative and not under the package install tree).
DEFAULT_REPLAY_DIR = _default_replay_dir()

# v13.22.4 (P3-5): FIFO retention cap on the replay manifest cache.
# Without a cap, .beagle/replays/ grows unbounded. 500 manifests ≈ ~5 MB
# at the current mean manifest size (10 KB); enough headroom for typical
# research / development loops, small enough to bound disk usage. Can be
# overridden by env var BEAGLE_REPLAY_MAX_MANIFESTS.
DEFAULT_MAX_MANIFESTS: int = int(os.environ.get("BEAGLE_REPLAY_MAX_MANIFESTS", "500"))


def _enforce_replay_retention(replay_dir: Path, max_manifests: int) -> int:
    """Delete oldest manifests by mtime until the directory holds at most
    ``max_manifests`` entries. Returns the number of files removed.

    Skips files that are not ``*.json`` (defensive — the recorder only
    emits ``<workflow_id>.json``, but ad-hoc files may exist). Failures
    are logged at WARNING level and never raise — retention must not
    block the save path.
    """
    if max_manifests <= 0:
        return 0
    try:
        candidates = [p for p in replay_dir.iterdir() if p.is_file() and p.suffix == ".json"]
    except OSError as exc:
        logger.warning(f"[replay-retention] Could not list {replay_dir}: {exc}")
        return 0

    excess = len(candidates) - max_manifests
    if excess <= 0:
        return 0

    # Sort by mtime ascending — oldest first. key=os.path.getmtime
    # handles the rare case where stat().st_mtime is not directly
    # comparable.
    candidates.sort(key=lambda p: p.stat().st_mtime)
    removed = 0
    for path in candidates[:excess]:
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            logger.warning(f"[replay-retention] Failed to delete {path.name}: {exc}")
    if removed:
        logger.info(
            f"[replay-retention] Removed {removed} oldest manifests; "
            f"cap={max_manifests}, dir={replay_dir}"
        )
    return removed


class ReplayRecorder:
    """Records node inputs during workflow execution for replay."""

    def __init__(self, replay_dir: Path | None = None) -> None:
        """Initialize the replay recorder.

        Args:
            replay_dir: Where manifest JSON files are written. Defaults to
                ``DEFAULT_REPLAY_DIR`` (``get_data_root()/replays``).
                Tests can inject a tmp_path here for isolation.

        """
        self._manifest: ReplayManifest | None = None
        self._recording = False
        self._subscription_ids: list[str] = []
        self._replay_dir: Path = replay_dir if replay_dir is not None else DEFAULT_REPLAY_DIR

    def start_recording(
        self,
        workflow_id: str,
        query: str,
        steering_prompt: str = "",
        mode: str = "audit",
        seed: str = "",
        workflow_name: str = "",
        beagle_version: str = "",
        config_snapshot: dict | None = None,
    ) -> None:
        """Begin recording a new workflow execution.

        Args:
            workflow_id: Stable per-run identifier.
            query: User query verbatim.
            steering_prompt: Optional high-priority directive.
            mode: Workflow mode (``audit``, ``develop``, ``research`` …).
            seed: Deterministic seed (auto-derived if empty).
            workflow_name: Workflow filename stem (e.g. ``self-improvement``).
                Recorded for replay; an empty value degrades fidelity — a
                warning is logged if omitted by the caller.
            beagle_version: Live ``PACKAGE_VERSION`` at recording time.
                Recorded for replay; an empty value degrades fidelity.
            config_snapshot: Shallow copy of the live resolved config
                (model, feature flags, env-derived overrides) — needed to
                reproduce runs whose behaviour depends on config state.
                Empty ``{}`` is acceptable for the test-time default but
                should be populated by the orchestrator.

        Note:
            All three ``workflow_name``, ``beagle_version``,
            ``config_snapshot`` are SSOT-tracked. The orchestrator is
            responsible for populating them at workflow start.

        """
        if self._recording:
            logger.warning("ReplayRecorder already recording — stopping previous session")
            self.stop_recording()

        if not seed:
            seed = hashlib.sha256((query + workflow_id).encode()).hexdigest()[:16]

        # v13.22.4 (P2-2): surface missing fields loudly instead of
        # silently writing empty strings. The dataclass defaults remain
        # empty for backward-compat with tests that build a manifest
        # directly, but the recorder path logs a warning so silent
        # data-integrity loss becomes observable.
        if not workflow_name:
            logger.warning(
                "ReplayRecorder.start_recording: workflow_name not provided; "
                "replay fidelity degraded (cannot recover workflow file)."
            )
        if not beagle_version:
            logger.warning(
                "ReplayRecorder.start_recording: beagle_version not provided; "
                "replay fidelity degraded (cannot reconcile against PACKAGE_VERSION)."
            )
        if config_snapshot is None or not config_snapshot:
            logger.warning(
                "ReplayRecorder.start_recording: config_snapshot empty; "
                "replay fidelity degraded (config-dependent behaviour not reproducible)."
            )

        self._manifest = ReplayManifest(
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            beagle_version=beagle_version,
            query=query,
            steering_prompt=steering_prompt,
            mode=mode,
            seed=seed,
            config_snapshot=dict(config_snapshot or {}),
            started_at=time.time(),
        )
        self._recording = True
        logger.info(
            f"Recording started for workflow {workflow_id} "
            f"(workflow_name={workflow_name!r}, beagle_version={beagle_version!r}) "
            f"with seed={seed!r}"
        )

        # Subscribe to relevant events
        try:
            from beagle.events import get_event_bus

            bus = get_event_bus()

            # Subscribe to NodeInputCaptured events for full prompt data
            sub_id = bus.subscribe(
                "node.input.captured",
                self._on_node_input_captured,
            )
            self._subscription_ids.append(sub_id)
            logger.debug(f"Subscribed to node.input.captured events (sub={sub_id})")
        except (
            ImportError,
            RuntimeError,
            OSError,
            AttributeError,
            TypeError,
        ) as err:  # catch: NARROWED  # RATIONALE=import of the bus module, singleton init, lock/replay in subscribe(), and the callback running against a half-built manifest during replay — recording must start even when the bus is unavailable
            logger.warning(f"Could not subscribe to event bus: {err}")

    def _on_node_input_captured(self, event: object) -> None:
        """Handle NodeInputCaptured events from the event bus."""
        from .manifest import NodeInput

        if not self._recording or self._manifest is None:
            return

        # Extract attributes from the event dataclass
        node_name = getattr(event, "node_name", "")
        prompt_hash = getattr(event, "prompt_hash", "")
        system_directive_hash = getattr(event, "system_directive_hash", "")
        model = getattr(event, "model", "")
        temperature = getattr(event, "temperature", 0.0)
        timestamp = getattr(event, "timestamp", 0.0)

        # We store hashes rather than full prompts (prompts can be very large).
        # The full prompt data should be stored separately if needed.
        node_input = NodeInput(
            node_name=node_name,
            prompt=prompt_hash,  # SHA-256 hash of prompt
            system_directive=system_directive_hash,  # SHA-256 hash
            model=model,
            temperature=temperature,
            timestamp=timestamp,
            attempt=1,  # Default; updated on retry
        )

        self._manifest.node_inputs.append(node_input)
        logger.debug(f"Recorded input for node {node_name}")

    def record_node_input(
        self,
        node_name: str,
        prompt: str,
        system_directive: str,
        model: str,
        temperature: float,
        attempt: int = 1,
    ) -> None:
        """Record inputs for a single node execution.

        Called programmatically (e.g., from the orchestrator) in addition
        to event-driven recording.
        """
        from .manifest import NodeInput

        if not self._recording or self._manifest is None:
            logger.warning("ReplayRecorder not recording — ignoring record_node_input call")
            return

        # Hash prompts to avoid storing potentially huge text
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        directive_hash = hashlib.sha256(system_directive.encode()).hexdigest()

        node_input = NodeInput(
            node_name=node_name,
            prompt=prompt_hash,
            system_directive=directive_hash,
            model=model,
            temperature=temperature,
            timestamp=time.time(),
            attempt=attempt,
        )

        self._manifest.node_inputs.append(node_input)
        logger.debug(f"Recorded input for node {node_name} (attempt {attempt})")

    def stop_recording(self) -> ReplayManifest | None:
        """Finalize and return the manifest. Save to disk."""
        if not self._recording or self._manifest is None:
            logger.warning("ReplayRecorder not recording — nothing to stop")
            return None

        self._manifest.completed_at = time.time()
        self._recording = False

        # Unsubscribe from events
        try:
            from beagle.events import get_event_bus

            bus = get_event_bus()
            for sub_id in self._subscription_ids:
                bus.unsubscribe(sub_id)
        except (
            ImportError,
            RuntimeError,
            OSError,
            KeyError,
        ) as err:  # catch: NARROWED  # RATIONALE=cleanup path — bus import/init or a stale subscription id must not abort stop_recording
            logger.debug(f"Event bus unsubscribe skipped during stop_recording: {err}")
        self._subscription_ids.clear()

        # Save manifest to disk
        try:
            self._replay_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = self._replay_dir / f"{self._manifest.workflow_id}.json"
            self._manifest.save(manifest_path)
            # v13.22.4 (P3-5): enforce FIFO retention cap after each
            # save so the cache stays bounded even under heavy load.
            _enforce_replay_retention(
                self._replay_dir,
                DEFAULT_MAX_MANIFESTS,
            )
        except (
            OSError,
            ValueError,
            TypeError,
        ) as err:  # catch: NARROWED  # RATIONALE=save() is mkdir + tempfile + os.write + json serialisation; a failed save must not lose the in-memory manifest returned below
            logger.error(f"Failed to save manifest: {err}")

        logger.info(
            f"Recording stopped for workflow {self._manifest.workflow_id}. "
            f"Captured {len(self._manifest.node_inputs)} node inputs."
        )

        manifest = self._manifest
        self._manifest = None
        return manifest

    @property
    def is_recording(self) -> bool:
        """Check if recording is active."""
        return self._recording

    @property
    def manifest(self) -> ReplayManifest | None:
        """Get the current manifest."""
        return self._manifest


# Module-level singleton
_recorder: ReplayRecorder | None = None
_recorder_lock = threading.Lock()


def get_replay_recorder() -> ReplayRecorder:
    """Get the global ReplayRecorder singleton."""
    global _recorder
    with _recorder_lock:
        if _recorder is None:
            _recorder = ReplayRecorder()
    return _recorder


def reset_replay_recorder(replay_dir: Path | None = None) -> ReplayRecorder:
    """Reset the global recorder singleton — primarily for test isolation.

    If ``replay_dir`` is given, the new singleton is constructed with that
    directory; otherwise the project-rooted default is used.

    Returns the freshly-created recorder so callers can chain.
    """
    global _recorder
    with _recorder_lock:
        _recorder = ReplayRecorder(replay_dir=replay_dir)
    return _recorder


__all__ = [
    "DEFAULT_MAX_MANIFESTS",
    "DEFAULT_REPLAY_DIR",
    "ReplayRecorder",
    "_enforce_replay_retention",
    "get_replay_recorder",
    "reset_replay_recorder",
]
