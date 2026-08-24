"""Workflow authoring/inspection commands (new-workflow, list, info, validate, visualize).

Extracted from cli.py in the v1.0.0 F2 split. Registered flat on the root
app via ``app.add_typer(workflows_app)`` (no name), so the CLI surface is
byte-identical to the pre-split monolith.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import click
import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ...config._config_path import find_metaprompts_dir
from ...core.templating import WorkflowGenerator
from ...core.workflow_loader import (
    list_workflows,
    load_workflow,
    validate_workflow,
)
from ._common import _resolve_workflow

console = Console()

workflows_app = typer.Typer()


@workflows_app.command("new-workflow")
def new_workflow(
    description: str = typer.Option(
        ...,
        "--description",
        "-d",
        help="Natural language description of the workflow goal",
    ),
    mode: str = typer.Option(
        "audit", "--mode", "-m", help="Workflow mode: audit, develop, research"
    ),
    save_as: str | None = typer.Option(
        None,
        "--save-as",
        "-s",
        help="Filename to save the workflow as (e.g. perf-audit.yaml)",
    ),
) -> None:
    """Generate a new YAML workflow from a description."""
    generator = WorkflowGenerator()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress.add_task("[cyan]Generating workflow...", total=None)
        try:
            yaml_content = asyncio.run(generator.generate(description, mode))
        except Exception as e:  # broad catch intentional
            console.print(f"[bold red]Generation failed:[/bold red] {e}")
            raise typer.Exit(1) from e

    # Review loop
    current_content = yaml_content
    while True:
        console.print("\n[bold cyan]--- Proposed Workflow ---[/bold cyan]")
        from rich.syntax import Syntax

        syntax = Syntax(current_content, "yaml", theme="monokai", line_numbers=True)
        console.print(syntax)

        choice = typer.prompt(
            "\nActions: [a]ccept and save, [e]dit, [r]egenerate, [q]uit", default="a"
        ).lower()

        if choice == "q":
            raise typer.Exit(0)
        elif choice == "r":
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task("[cyan]Regenerating workflow...", total=None)
                current_content = asyncio.run(generator.generate(description, mode))
            continue
        elif choice == "e":
            current_content = click.edit(current_content, extension=".yaml") or current_content
            continue
        elif choice == "a":
            # Save the file
            if not save_as:
                save_as = typer.prompt("Save as filename", default="new_workflow.yaml")

            if not save_as.endswith(".yaml") and not save_as.endswith(".yml"):
                save_as += ".yaml"

            # S5/S6: write to the canonical config-root metaprompts, not the
            # workspace/package dir (which is read-only under a wheel install).
            save_path = find_metaprompts_dir() / save_as

            # Final validation check
            try:
                # Save to temp for validation
                import tempfile

                tmp_path: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
                    ) as tmp:
                        tmp.write(current_content)
                        tmp_path = Path(tmp.name)

                    errors = validate_workflow(tmp_path)
                finally:
                    if tmp_path is not None:
                        tmp_path.unlink(missing_ok=True)

                if errors:
                    console.print(
                        "[bold red]Validation errors found in generated workflow:[/bold red]"
                    )
                    for err in errors:
                        console.print(f"  - {err}")
                    if not typer.confirm("Save anyway?"):
                        continue
            except ImportError as e:
                console.print(f"[yellow]Warning: Could not validate workflow: {e}[/yellow]")

            try:
                save_path.parent.mkdir(parents=True, exist_ok=True)
                save_path.write_text(current_content, encoding="utf-8")
                console.print(f"[bold green]Workflow saved to:[/bold green] {save_path}")
                break
            except OSError as e:
                console.print(f"[bold red]Failed to save workflow:[/bold red] {e}")
                save_as = None  # Ask again
                continue


@workflows_app.command("list")
def list_cmd(
    verbose: bool = typer.Option(
        False,
        "-v",
        "--verbose",
        help="Show detailed workflow information including agents",
    ),
) -> None:
    """List available workflows."""
    workflows = list_workflows()

    if not workflows:
        console.print("[yellow]No workflows found in metaprompts/[/yellow]")
        return

    if verbose:
        # Detailed mode with agents
        for wf in workflows:
            # Use the path directly from workflows dict
            workflow_path = Path(wf["path"])
            try:
                import yaml  # type: ignore[import-untyped]

                # Load YAML to get agent info directly (faster than loading DAG)
                with open(workflow_path, encoding="utf-8") as f:
                    spec = yaml.safe_load(f)

                # Extract agents from phases
                agents = set()
                for phase in spec.get("phases", []):
                    agent = phase.get("agent")
                    if agent:
                        agents.add(agent)

                console.print(f"\n[bold cyan]{wf['name']}[/bold cyan]")
                console.print(f"  [dim]{wf.get('description', 'No description')}[/dim]")
                console.print(f"  [yellow]Phases:[/yellow] {wf['phases']}")
                console.print(f"  [yellow]Mode:[/yellow] {spec.get('mode', 'N/A')}")
                console.print(f"  [yellow]Budget:[/yellow] ${spec.get('budget_usd', 10.0)}")
                console.print(
                    f"  [yellow]Agents:[/yellow] {', '.join(sorted(agents)) if agents else 'N/A'}"
                )
                console.print(f"  [dim]Path:[/dim] {workflow_path}")
            except OSError as e:
                console.print(f"\n[bold cyan]{wf['name']}[/bold cyan]")
                console.print(f"  [dim]{wf.get('description', 'No description')}[/dim]")
                console.print(f"  [yellow]Phases:[/yellow] {wf['phases']}")
                console.print(f"  [red]Error loading details: {e}[/red]")
    else:
        # Compact table mode
        table = Table(title="Available Workflows")
        table.add_column("Name", style="cyan")
        table.add_column("Phases", justify="right")
        table.add_column("Description")

        for wf in workflows:
            table.add_row(
                wf["name"],
                str(wf["phases"]),
                wf["description"][:60] + "..."
                if len(wf.get("description", "")) > 60
                else wf.get("description", ""),
            )

        console.print(table)

    console.print("\n[dim]Use 'goose run list -v' for detailed workflow information[/dim]")
    console.print("[dim]Use 'goose run info <workflow>' for full workflow details[/dim]")


@workflows_app.command()
def info(
    workflow: str = typer.Argument(..., help="Workflow name to inspect"),
) -> None:
    """Show detailed information about a workflow including agents, phases, and dependencies."""
    workflow_path = _resolve_workflow(workflow)

    try:
        # v13.21.3: Was `from workflow_loader import load_workflow_graph` —
        # a bare top-level import that only worked when `core/` was on
        # sys.path (true for wheel installs where the package's top-level
        # directories leaked into sys.path, false for proper editable
        # installs). Use the package-relative form to match the imports
        # at the top of this module.
        from ...core.workflow_loader import load_workflow_graph

        dag = load_workflow_graph(workflow_path, workflow_query="")

        # Read the YAML to get metadata
        import yaml

        with open(workflow_path, encoding="utf-8") as f:
            spec = yaml.safe_load(f)

        # Basic info
        console.print(f"\n[bold cyan]Workflow:[/bold cyan] {spec.get('name', workflow)}")
        console.print(f"[dim]Description:[/dim] {spec.get('description', 'No description')}")
        console.print(f"[dim]Version:[/dim] {spec.get('version', 'N/A')}")
        console.print(f"[dim]Mode:[/dim] {spec.get('mode', 'N/A')}")
        console.print(f"[dim]Budget:[/dim] ${spec.get('budget_usd', 10.0)}")

        # Agents table
        agents = {}  # type: ignore[var-annotated]
        for node_name, node_info in dag.nodes.items():
            if hasattr(node_info, "metadata") and node_info.metadata:
                agent = node_info.metadata.get("agent", "unknown")
                if agent not in agents:
                    agents[agent] = []
                agents[agent].append(node_name)

        if agents:
            console.print(f"\n[bold]Agents ({len(agents)}):[/bold]")
            agent_table = Table(show_header=True, header_style="bold magenta")
            agent_table.add_column("Agent", style="cyan")
            agent_table.add_column("Nodes")

            for agent in sorted(agents.keys()):
                agent_table.add_row(agent, ", ".join(agents[agent]))

            console.print(agent_table)

        # Phases table
        phases = spec.get("phases", [])
        if phases:
            console.print(f"\n[bold]Phases ({len(phases)}):[/bold]")
            phases_table = Table(show_header=True, header_style="bold green")
            phases_table.add_column("Phase", style="green")
            phases_table.add_column("Agent", style="cyan")
            phases_table.add_column("Required")
            phases_table.add_column("Dependencies")

            for phase in phases:
                phase_name = phase.get("name", "unknown")
                phase_agent = phase.get("agent", "N/A")
                required = (
                    "[red]No[/red]"
                    if phase.get("required", True) is False
                    else "[green]Yes[/green]"
                )
                deps = ", ".join(phase.get("depends_on", []))

                phases_table.add_row(phase_name, phase_agent, required, deps if deps else "-")

            console.print(phases_table)

        # Success criteria
        criteria = spec.get("success_criteria", [])
        if criteria:
            console.print("\n[bold]Success Criteria:[/bold]")
            for i, criterion in enumerate(criteria, 1):
                console.print(f"  {i}. [dim]{criterion}[/dim]")

        # Dependencies graph (simple tree representation)
        console.print("\n[bold]Execution Flow:[/bold]")
        for i, phase in enumerate(phases):
            deps = phase.get("depends_on", [])
            prefix = "  ╰─» " if i > 0 and deps else "  ├─» " if i > 0 else "  •  "
            console.print(
                f"{prefix}[green]{phase.get('name', 'unknown')}[/green] "
                f"({phase.get('agent', 'N/A')})"
            )
            if deps:
                console.print(f"      [dim]depends on: {', '.join(deps)}[/dim]")

        console.print(f"\n[dim]Path:[/dim] {workflow_path}")

    except FileNotFoundError:
        console.print(f"[bold red]Workflow not found:[/bold red] {workflow}")
        raise typer.Exit(1) from None
    except Exception as e:  # broad catch intentional
        console.print(f"[bold red]Error loading workflow:[/bold red] {e}")
        raise typer.Exit(1) from e


@workflows_app.command()
def validate(
    workflow: str = typer.Argument(..., help="Workflow to validate"),
) -> None:
    """Validate a workflow without executing."""
    workflow_path = _resolve_workflow(workflow)

    errors = validate_workflow(workflow_path)

    if errors:
        console.print(f"[bold red]Validation failed for {workflow}:[/bold red]")
        for error in errors:
            console.print(f"  [red]- {error}[/red]")
        raise typer.Exit(1)
    else:
        console.print(f"[bold green]Workflow {workflow} is valid![/bold green]")


@workflows_app.command()
def visualize(
    workflow: str = typer.Argument(..., help="Workflow to visualize"),
) -> None:
    """Visualize workflow as ASCII diagram."""
    workflow_path = _resolve_workflow(workflow)

    try:
        dag = load_workflow(workflow_path)
    except Exception as e:  # broad catch intentional
        console.print(f"[bold red]Error loading workflow:[/bold red] {e}")
        raise typer.Exit(1) from e

    # Build ASCII diagram
    console.print(f"\n[bold]Workflow: {workflow}[/bold]\n")

    nodes = list(dag.nodes.keys())
    for i, node_name in enumerate(nodes):
        # Box top
        box_width = max(len(node_name), 13)
        console.print(f"{'':>4}+-{'-' * box_width}-+")
        console.print(f"{'':>4}| {node_name:^{box_width}} |")
        console.print(f"{'':>4}+-{'-' * box_width}-+")

        # Arrow to next node
        if i < len(nodes) - 1:
            console.print(f"{'':>4}{'':^{box_width // 2 + 2}}|")
            console.print(f"{'':>4}{'':^{box_width // 2 + 2}}v")

    console.print()


# ============================================================================
# Checkpoint Commands
# ============================================================================

# History & Stats Commands
# ============================================================================
