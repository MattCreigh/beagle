"""Replay engine — replays a workflow from a saved manifest.

Sets up deterministic mode and re-executes the workflow with the same inputs.
The actual orchestrator integration comes later; this module handles the
deterministic mode setup and manifest comparison.
"""

from __future__ import annotations

import logging

from .determinism import set_deterministic_mode
from .manifest import ReplayManifest

logger = logging.getLogger("Beagle.reproducibility")


class ReplayEngine:
    """Replays a workflow from a saved manifest."""

    def __init__(self, manifest: ReplayManifest) -> None:
        self._manifest = manifest

    async def replay(self) -> dict:
        """Replay the workflow with deterministic settings.

        1. Enable deterministic mode with the manifest's seed
        2. Set temperature to 0.0
        3. Execute the same workflow with same query/steering
        4. Compare outputs to original (if available)
        5. Disable deterministic mode

        NOTE: Actual workflow re-execution is not implemented here —
        the orchestrator integration comes later. This method sets up
        deterministic mode and returns metadata about the replay.
        """
        seed = self._manifest.seed
        logger.info(f"Starting replay for workflow {self._manifest.workflow_id} with seed={seed!r}")

        # Step 1: Enable deterministic mode
        set_deterministic_mode(enabled=True, seed=seed)

        try:
            # Step 2: Build replay context (actual re-execution TBD)
            context = {
                "workflow_id": self._manifest.workflow_id,
                "workflow_name": self._manifest.workflow_name,
                "query": self._manifest.query,
                "steering_prompt": self._manifest.steering_prompt,
                "mode": self._manifest.mode,
                "seed": seed,
                "node_count": len(self._manifest.node_inputs),
                "original_started_at": self._manifest.started_at,
                "original_completed_at": self._manifest.completed_at,
                "deterministic_uuid_sample": self._deterministic_uuid_sample(),
            }

            # Steps 3-4: Actual re-execution and comparison —
            # the orchestrator integration comes later.

            # Step 5: Disable deterministic mode
            set_deterministic_mode(enabled=False)

            logger.info(f"Replay setup complete for workflow {self._manifest.workflow_id}")

            result = {
                "status": "replay_setup_complete",
                "context": context,
                "note": (
                    "Deterministic mode was enabled and then disabled. "
                    "Actual workflow re-execution integration is not yet implemented."
                ),
            }

        except Exception as err:  # broad catch intentional
            # Ensure deterministic mode is disabled on error
            set_deterministic_mode(enabled=False)
            logger.error(f"Replay failed: {err}")
            raise

        return result

    def _deterministic_uuid_sample(self) -> str:
        """Generate a sample deterministic UUID to verify deterministic mode."""
        from .determinism import deterministic_uuid

        return deterministic_uuid(context="replay_verification")

    def diff(self, original_manifest: ReplayManifest, replay_manifest: ReplayManifest) -> list[str]:
        """Compare two manifests and report differences.

        Returns list of human-readable difference descriptions.
        """
        differences: list[str] = []

        if original_manifest.workflow_id != replay_manifest.workflow_id:
            differences.append(
                f"workflow_id differs: "
                f"{original_manifest.workflow_id!r} vs "
                f"{replay_manifest.workflow_id!r}"
            )

        if original_manifest.query != replay_manifest.query:
            differences.append(
                f"query differs: "
                f"{original_manifest.query[:80]!r}... vs "
                f"{replay_manifest.query[:80]!r}..."
            )

        if original_manifest.mode != replay_manifest.mode:
            differences.append(
                f"mode differs: {original_manifest.mode!r} vs {replay_manifest.mode!r}"
            )

        if original_manifest.seed != replay_manifest.seed:
            differences.append(
                f"seed differs: {original_manifest.seed!r} vs {replay_manifest.seed!r}"
            )

        # Compare node inputs
        orig_inputs = {ni.node_name: ni for ni in original_manifest.node_inputs}
        replay_inputs = {ni.node_name: ni for ni in replay_manifest.node_inputs}

        all_nodes = set(orig_inputs.keys()) | set(replay_inputs.keys())
        for node_name in sorted(all_nodes):
            if node_name not in orig_inputs:
                differences.append(f"node {node_name!r} present in replay but not original")
                continue
            if node_name not in replay_inputs:
                differences.append(f"node {node_name!r} present in original but not replay")
                continue

            orig = orig_inputs[node_name]
            replay = replay_inputs[node_name]

            if orig.prompt != replay.prompt:
                differences.append(
                    f"node {node_name!r} prompt differs: "
                    f"{orig.prompt[:40]!r} vs {replay.prompt[:40]!r}"
                )
            if orig.model != replay.model:
                differences.append(
                    f"node {node_name!r} model differs: {orig.model!r} vs {replay.model!r}"
                )
            if orig.temperature != replay.temperature:
                differences.append(
                    f"node {node_name!r} temperature differs: "
                    f"{orig.temperature} vs {replay.temperature}"
                )

        # Compare config snapshots
        if original_manifest.config_snapshot != replay_manifest.config_snapshot:
            orig_keys = set(original_manifest.config_snapshot.keys())
            replay_keys = set(replay_manifest.config_snapshot.keys())

            for key in sorted(orig_keys - replay_keys):
                differences.append(f"config key {key!r} missing in replay")
            for key in sorted(replay_keys - orig_keys):
                differences.append(f"config key {key!r} missing in original")
            for key in sorted(orig_keys & replay_keys):
                if original_manifest.config_snapshot[key] != replay_manifest.config_snapshot[key]:
                    differences.append(
                        f"config key {key!r} differs: "
                        f"{original_manifest.config_snapshot[key]!r} vs "
                        f"{replay_manifest.config_snapshot[key]!r}"
                    )

        return differences


__all__ = ["ReplayEngine"]
