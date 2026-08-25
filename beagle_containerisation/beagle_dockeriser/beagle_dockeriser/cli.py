"""CLI for beagle_dockeriser — deployment orchestrator interface."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .constants import DOCKER_IMAGE_TAG, PROJECT_VERSION

app = typer.Typer(
    name="beagle-dockeriser",
    help=f"Beagle Docker Deployment Orchestrator v{PROJECT_VERSION}",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
console = Console()


@app.command()
def deploy(
    project_root: Annotated[
        Path,
        typer.Option(
            Path.cwd(),
            "--project-root",
            "-r",
            help="Path to beagle project root",
            exists=True,
            dir_okay=True,
        ),
    ],
    phase: Annotated[
        int,
        typer.Option(
            0,
            "--phase",
            "-p",
            help="Start from phase (1-5). 0 = run all phases sequentially.",
            min=0,
            max=5,
        ),
    ],
    skip_validation: Annotated[
        bool,
        typer.Option(False, "--skip-validation", help="Skip Phase 1 (Golden Master validation)"),
    ],
    skip_build: Annotated[
        bool,
        typer.Option(
            False, "--skip-build", help="Skip Phase 5 (Docker build). Just generate files."
        ),
    ],
    image_tag: Annotated[
        str,
        typer.Option(DOCKER_IMAGE_TAG, "--tag", "-t", help="Docker image tag"),
    ],
    verbose: Annotated[bool, typer.Option(False, "--verbose", "-v", help="Verbose output")],
    dry_run: Annotated[
        bool, typer.Option(False, "--dry-run", help="Show what would be done without executing")
    ],
) -> None:
    """Run the full deployment pipeline (Phases 1-5)."""
    from .pipeline import Pipeline

    pipeline = Pipeline(
        project_root=project_root,
        start_phase=phase,
        skip_validation=skip_validation,
        skip_build=skip_build,
        image_tag=image_tag,
        verbose=verbose,
        dry_run=dry_run,
    )
    pipeline.run()


@app.command()
def validate(
    project_root: Annotated[
        Path,
        typer.Option(
            Path.cwd(),
            "--project-root",
            "-r",
            help="Path to project root",
            exists=True,
        ),
    ],
) -> None:
    """Run Phase 1 only: Golden Master validation."""
    from .models import PipelineState
    from .phases.validate import run_validation

    state = PipelineState(project_root=project_root.resolve())
    state = run_validation(state)

    console.print()
    if state.phase1_passed:
        console.print("[green]✓ All validation checks passed[/green]")
    else:
        console.print("[red]✗ Validation failed[/red]")
        for err in state.errors:
            console.print(f"  [red]• {err}[/red]")


@app.command()
def generate(
    project_root: Annotated[
        Path,
        typer.Option(
            Path.cwd(),
            "--project-root",
            "-r",
            help="Path to project root",
            exists=True,
        ),
    ],
) -> None:
    """Generate Dockerfile + docker-compose.yaml + .dockerignore without building."""
    from .models import PipelineState
    from .phases.build import run_build
    from .phases.build_push import _generate_dockerignore
    from .phases.compose import run_compose_gen
    from .phases.dockerfile import run_dockerfile_gen

    state = PipelineState(project_root=project_root.resolve())
    state = run_build(state)
    if not state.phase2_passed:
        console.print("[red]✗ Wheel build failed[/red]")
        for err in state.errors:
            console.print(f"  [red]• {err}[/red]")
        return

    state = run_dockerfile_gen(state)
    state = run_compose_gen(state)
    _generate_dockerignore(project_root.resolve())

    console.print()
    if state.phase3_passed and state.phase4_passed:
        console.print("[green]✓ Dockerfile + docker-compose.yaml + .dockerignore generated[/green]")
        console.print(f"  Dockerfile:    {state.dockerfile_path}")
        console.print(f"  Compose:       {state.compose_path}")
        console.print(f"  .dockerignore: {project_root.resolve() / '.dockerignore'}")
    else:
        console.print("[red]✗ Generation failed[/red]")


@app.command()
def status(
    project_root: Annotated[
        Path,
        typer.Option(
            Path.cwd(),
            "--project-root",
            "-r",
            help="Path to project root",
            exists=True,
        ),
    ],
) -> None:
    """Show current deployment state."""
    table = Table(title="Deployment Status")
    table.add_column("Artifact", style="cyan")
    table.add_column("Status", style="bold")
    table.add_column("Path")

    # Check wheel
    dist_dir = project_root / "dist"
    wheels = list(dist_dir.glob("*.whl")) if dist_dir.exists() else []
    if wheels:
        table.add_row("Wheel", "[green]EXISTS[/green]", str(wheels[0].name))
    else:
        table.add_row("Wheel", "[dim]MISSING[/dim]", "—")

    # Check Dockerfile
    containerisation_dir = project_root / "beagle_containerisation"
    dockerfile = containerisation_dir / "Dockerfile"
    if dockerfile.exists():
        table.add_row("Dockerfile", "[green]EXISTS[/green]", str(dockerfile))
    else:
        table.add_row("Dockerfile", "[dim]MISSING[/dim]", "—")

    # Check compose
    compose = containerisation_dir / "docker-compose.yaml"
    if compose.exists():
        table.add_row("Compose", "[green]EXISTS[/green]", str(compose))
    else:
        table.add_row("Compose", "[dim]MISSING[/dim]", "—")

    # Check .dockerignore
    dockerignore = project_root / ".dockerignore"
    if dockerignore.exists():
        table.add_row(".dockerignore", "[green]EXISTS[/green]", str(dockerignore))
    else:
        table.add_row(".dockerignore", "[dim]MISSING[/dim]", "—")

    # Check docker image
    import subprocess

    try:
        result = subprocess.run(
            [
                "docker",
                "images",
                "--format",
                "{{.Repository}}:{{.Tag}} {{.Size}}",
                "beagle-factory",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.stdout.strip():
            table.add_row("Docker Image", "[green]BUILT[/green]", result.stdout.strip())
        else:
            table.add_row("Docker Image", "[dim]NOT BUILT[/dim]", "—")
    except (OSError, subprocess.TimeoutExpired):
        table.add_row("Docker Image", "[dim]UNKNOWN[/dim]", "docker not available")

    console.print(table)
