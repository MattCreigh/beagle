"""Pipeline orchestrator — sequential phase execution."""

from __future__ import annotations

import time
from pathlib import Path

from .constants import DOCKER_IMAGE_TAG, FULL_IMAGE_REF
from .models import PipelineState


class Pipeline:
    """Orchestrates the 5-phase deployment pipeline."""

    def __init__(
        self,
        project_root: Path,
        start_phase: int = 0,
        skip_validation: bool = False,
        skip_build: bool = False,
        image_tag: str = DOCKER_IMAGE_TAG,
        verbose: bool = False,
        dry_run: bool = False,
    ) -> None:
        self.state = PipelineState(project_root=project_root.resolve())
        self.start_phase = start_phase
        self.skip_validation = skip_validation
        self.skip_build = skip_build
        self.image_tag = image_tag
        self.verbose = verbose
        self.dry_run = dry_run

    def run(self) -> PipelineState:
        """Execute the pipeline phases sequentially."""
        header = f"Docker Deployment Program\nProject: {self.state.project_root}"
        print("=" * 62)
        print(f"  {header}")
        print("=" * 62)

        phases = [
            (1, "Validator", self._phase1),
            (2, "Builder", self._phase2),
            (3, "Dockerfile", self._phase3),
            (4, "Compose", self._phase4),
            (5, "Build & Push", self._phase5),
        ]

        for phase_num, phase_name, phase_fn in phases:
            if self.start_phase > phase_num:
                print(
                    f"  Phase {phase_num}: {phase_name} — SKIPPED (start_phase={self.start_phase})"
                )
                continue
            if phase_num == 1 and self.skip_validation:
                print(f"  Phase {phase_num}: {phase_name} — SKIPPED (--skip-validation)")
                self.state.phase1_passed = True
                continue
            if phase_num == 5 and self.skip_build:
                print(f"  Phase {phase_num}: {phase_name} — SKIPPED (--skip-build)")
                continue

            print(f"\n  Phase {phase_num}: {phase_name}")
            t0 = time.monotonic()

            try:
                self.state = phase_fn()
            except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional — surface phase failure and stop
                self.state.errors.append(f"Phase {phase_num} ({phase_name}): {exc}")
                print(f"    FAILED: {exc}")
                break

            elapsed = time.monotonic() - t0
            self.state.phase_details[f"phase{phase_num}_time"] = f"{elapsed:.1f}s"
            print(f"    ✓ Completed in {elapsed:.1f}s")

        self._print_summary()
        return self.state

    def _phase1(self) -> PipelineState:
        from .phases.validate import run_validation

        return run_validation(self.state)

    def _phase2(self) -> PipelineState:
        from .phases.build import run_build

        return run_build(self.state)

    def _phase3(self) -> PipelineState:
        from .phases.dockerfile import run_dockerfile_gen

        return run_dockerfile_gen(self.state)

    def _phase4(self) -> PipelineState:
        from .phases.compose import run_compose_gen

        return run_compose_gen(self.state)

    def _phase5(self) -> PipelineState:
        from .phases.build_push import run_build_push

        return run_build_push(self.state, image_tag=self.image_tag)

    def _print_summary(self) -> None:
        """Print final deployment summary table."""
        print()
        print("-" * 62)
        print("  Pipeline Summary")
        print("-" * 62)

        phases = [
            (1, "Validator", self.state.phase1_passed),
            (2, "Builder", self.state.phase2_passed),
            (3, "Dockerfile", self.state.phase3_passed),
            (4, "Compose", self.state.phase4_passed),
            (5, "Build & Push", self.state.phase5_passed),
        ]

        for num, name, passed in phases:
            status = "PASS" if passed else "SKIP/FAIL"
            detail = self.state.phase_details.get(f"phase{num}_detail", "")
            print(f"  Phase {num} {name:12s} : {status:10s} | {detail}")

        print("-" * 62)

        if self.state.errors:
            print("\n  ERRORS:")
            for err in self.state.errors:
                print(f"    • {err}")

        if self.state.warnings:
            print("\n  WARNINGS:")
            for w in self.state.warnings:
                print(f"    • {w}")

        # Print "How to Start" if Phase 5 passed
        if self.state.phase5_passed:
            from .phases.build_push import print_summary

            print_summary(self.state, FULL_IMAGE_REF)
