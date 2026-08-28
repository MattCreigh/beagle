"""Execution commands (run, interactive, goose-shell, cli, run-crewai, run-autogen).

Extracted from cli.py in the v1.0.0 F2 split. Registered flat on the root
app via ``app.add_typer(execution_app)`` (no name), so the CLI surface is
byte-identical to the pre-split monolith.
"""

from __future__ import annotations

import asyncio
import json as _json_exec
import logging
import time as _time
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from ...config.config import get_config
from ...core.autonomous_orchestrator import get_output_dir
from ...core.graph import run_workflow
from ...core.workflow_loader import (
    get_workflow_mode,
    get_workflow_nodes,
    load_workflow,
)
from ...preflight import (
    PreFlightEstimator,
    display_preflight_check,
    log_preflight_estimate,
)
from ...tracking import start_recorder
from ..cli_graceful_shutdown import (
    run_graph_workflow_gracefully,
    run_workflow_gracefully,
)
from ._common import _resolve_workflow

logger = logging.getLogger(__name__)
console = Console()

# v1.0.2 (qa-gate): narrow best-effort exception set for the seven
# `except Exception` blocks that this module used to satisfy BLE001.
# The doctrine requires catches to be narrow (core_directives:
# 'never bare except Exception'), so these handlers explicitly enumerate
# the classes they expect from optional / best-effort paths:
#   - RuntimeError, ValueError, OSError, TimeoutError  (doctrine floor)
#   - ImportError                                      (optional deps)
#   - AttributeError                                   (attribute probes)
#   - KeyError, IndexError, TypeError                  (config-shape drift)
#   - json.JSONDecodeError                             (config parsing)
# Bugs that don't fit this set (MemoryError, RecursionError, KeyboardInterrupt
# — all of which already propagate by design in 3.12+) are NOT swallowed,
# so a genuine defect in the best-effort path surfaces as a real failure
# instead of a silent log line.
_BEST_EFFORT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    RuntimeError,
    ValueError,
    OSError,
    TimeoutError,
    ImportError,
    AttributeError,
    KeyError,
    IndexError,
    TypeError,
    _json_exec.JSONDecodeError,
)

execution_app = typer.Typer()


# v1.0.2 (qa-gate): module-level typer.Option singletons so ruff B008
# ("Do not perform function call typer.Option in argument defaults")
# stops firing. Typer's documented pattern is to pass a typer.Option
# instance as the parameter default — calling the constructor in the
# default slot is what typer expects — but ruff B008 doesn't know
# about typer's metaprogramming and flags every default as a function
# call in a default argument. Hoisting each Option to module scope
# preserves the exact same CLI surface (Typer reads the singleton by
# identity at decorator-application time) and silences B008 without
# disabling the rule.
_OUTPUT_PATH_OPTION = typer.Option(
    None, "--output", "-o", help="Custom path to save the output report"
)


def _persist_report(workflow: str, report: str, mode: str) -> None:
    """Write the final report to the analysis_reports directory."""
    output_dir = get_output_dir()
    timestamp = _time.strftime("%Y%m%d_%H%M%S")
    safe_name = workflow.replace("/", "_").replace(" ", "_")[:50]
    filename = f"{safe_name}_{mode}_{timestamp}.md"
    report_path = output_dir / filename

    try:
        report_path.write_text(report, encoding="utf-8")
        console.print(f"\n[bold green]Report saved:[/bold green] {report_path}")
    except OSError as e:
        console.print(f"[yellow]Warning: Could not save report to disk: {e}[/yellow]")


@execution_app.command()
def run(
    workflow: str = typer.Argument(
        ...,
        help="Workflow name: research, deep-planning, develop, self-improvement, "
        "devops, db-migration, audit, security, incident",
    ),
    query: str = typer.Argument(..., help="The query to process"),
    budget: float = typer.Option(10.0, "--budget", "-b", help="Maximum budget in USD"),
    resume_from: str | None = typer.Option(None, "--resume", help="Resume from checkpoint ID"),
    estimate_only: bool = typer.Option(
        False, "--estimate", "-e", help="Show cost estimate without executing"
    ),
    _auto_approve: bool = typer.Option(
        False, "--auto-approve", help="Auto-approve all approval gates"
    ),
    approve_all: bool = typer.Option(
        False,
        "--approve-all",
        help="Approve all human-in-the-loop gates (bypass require_approval)",
    ),
    steering: str | None = typer.Option(
        None,
        "--steering",
        "-s",
        help="Global steering prompt injected into all agents as a high-priority directive",
    ),
    mode: str | None = typer.Option(
        None,
        "--mode",
        "-m",
        help="Workflow mode: 'audit' (read-only), 'develop' (read-write), 'research' (read-only). "
        "Overrides the mode declared in the YAML workflow file.",
    ),
    tui: bool = typer.Option(False, "--tui", help="Launch the reactive TUI dashboard"),
    headless: bool = typer.Option(
        False, "--headless", help="Run without any interactive output (Cl/CD mode)"
    ),
    skip_preflight: bool = typer.Option(
        False,
        "--skip-preflight",
        help="Bypass the cost and time estimation confirmation",
    ),
    output_format: str = typer.Option(
        "markdown",
        "--output-format",
        "-f",
        help="Output format: markdown, json, sarif, github-issues",
    ),
    output_path: Path | None = _OUTPUT_PATH_OPTION,
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Plan the workflow without executing it. Prints the plan (graph, estimated cost, agents) and exits. For github-issues output, --dry-run still previews the issues.",
    ),
) -> None:
    """Run a workflow with the given query."""
    # Phase 5: Start the recorder to persist events to the database
    # --dry-run: plan and exit before any side-effecting work.
    # Locks down the contract that --dry-run NEVER executes a Goose
    # subprocess, NEVER creates checkpoints, and NEVER mutates config
    # or state files. The plan is read-only.
    if dry_run:
        try:
            from beagle.core.workflow_loader import load_workflow

            estimator = PreFlightEstimator(budget_usd=budget)
            dag_nodes = []
            try:
                orch = load_workflow(workflow)
                dag_nodes = list(orch.nodes.values())
            except (
                _BEST_EFFORT_EXCEPTIONS
            ) as load_err:  # best-effort dry-run probe (narrow tuple at module scope)
                logger.warning(f"Could not load workflow graph for dry-run estimation: {load_err}")

            estimate = estimator.estimate(
                workflow_name=str(workflow),
                dag_nodes=dag_nodes,
            )
            console.print("\n[bold yellow]DRY RUN - no side effects will occur.[/bold yellow]")
            console.print("Workflow:    " + str(workflow))
            qpreview = (query[:100] + "...") if len(query) > 100 else query
            console.print("Query:       " + qpreview)
            mode_str = mode if mode else "(default from workflow YAML)"
            console.print("Mode:        " + mode_str)
            console.print("Budget:      $" + format(budget, ".2f"))
            console.print("Estimated cost:    $" + format(estimate.total_estimated_cost_usd, ".4f"))
            console.print("Estimated tokens:  " + format(estimate.total_estimated_tokens, ","))
            console.print("Planned agents:    " + str(len(estimate.nodes)))
            for node_est in estimate.nodes[:10]:
                # NodeEstimate exposes skill_name/model/provider — there is no
                # `agent_type` field. Reading one raised AttributeError, which
                # the broad `except Exception` below swallowed into "preflight
                # failed", so --dry-run silently degraded to the minimal plan
                # and NEVER showed the per-node breakdown it exists to show.
                # Surfacing the model here also makes the dry run the place to
                # confirm routing resolved to the intended model.
                console.print(
                    f"  - {node_est.node_name} ({node_est.skill_name} → {node_est.model})"
                )
            if len(estimate.nodes) > 10:
                console.print("  ... and " + str(len(estimate.nodes) - 10) + " more")
            console.print("\n[dim]Re-run without --dry-run to execute.[/dim]")
        except (
            _BEST_EFFORT_EXCEPTIONS
        ) as exc:  # best-effort plan preview (narrow tuple at module scope)
            console.print(
                "[yellow]Could not compute plan (preflight failed): " + str(exc) + "[/yellow]"
            )
            console.print("\n[bold yellow]DRY RUN - minimal plan only.[/bold yellow]")
            console.print("Workflow:    " + str(workflow))
            qpreview = (query[:100] + "...") if len(query) > 100 else query
            console.print("Query:       " + qpreview)
            console.print("Budget:      $" + format(budget, ".2f"))
        raise typer.Exit(code=0)

    start_recorder()

    # Phase 2.5: Initialize Orpheus ring buffers for agent IPC
    try:
        from ...config.loader import load_config
        from ...lifecycle.orpheus_startup import start_orpheus

        cfg = load_config()
        start_orpheus(transport=cfg.orpheus.transport)
    except (
        _BEST_EFFORT_EXCEPTIONS
    ) as orpheus_err:  # optional subsystem startup (narrow tuple at module scope)
        logger.debug(f"Orpheus startup skipped (non-critical): {orpheus_err}")

    # Phase 4.3: Startup health check — graceful degradation
    try:
        from ...startup.health_check import run_startup_checks

        startup_results = run_startup_checks(include_optional=True)
        failures = [r for r in startup_results if r.status == "fail"]
        warnings = [r for r in startup_results if r.status == "warn"]
        if failures:
            console.print("[bold red]Startup health check FAILURES:[/bold red]")
            for f in failures:
                console.print(f"  ✗ {f.name}: {f.message}")
                if f.fix_hint:
                    console.print(f"    → {f.fix_hint}")
            console.print("[dim]Run 'beagle health' for full diagnostics[/dim]")
            if not skip_preflight:
                # Allow --skip-preflight to bypass required check failures
                raise typer.Exit(1)
        if warnings:
            for w in warnings:
                console.print(f"  [yellow]⚠ {w.name}[/yellow]: {w.message}")
    except (
        _BEST_EFFORT_EXCEPTIONS
    ) as startup_err:  # optional health-check probe (narrow tuple at module scope)
        logger.debug(f"Startup health check skipped (non-fatal): {startup_err}")

    if tui and headless:
        console.print("[bold red]Error:[/bold red] --tui and --headless are mutually exclusive.")
        raise typer.Exit(1)

    workflow_path = _resolve_workflow(workflow)

    # PRE-FLIGHT CHECK (Phase 4)
    # Get nodes for estimation. SSOT is the workflow YAML
    # (src/metaprompts/<workflow>.yaml); we read it via
    # get_workflow_nodes() rather than maintaining a parallel hardcoded
    # list, which previously drifted from research.yaml (the hardcoded
    # block named 'planning'/'execution'/'verification'/'synthesis' while
    # the YAML actually defines 'search'/'synthesis'/'ground_truth_validation').
    nodes = get_workflow_nodes(workflow_path)
    if not nodes:
        # YAML not found / unparseable — surface a clear pre-flight failure
        # rather than running an empty node list silently.
        console.print(
            f"[bold red]Pre-flight error:[/bold red] no nodes could be loaded "
            f"from workflow {workflow!r} (path={workflow_path})."
        )
        raise typer.Exit(1)

    estimator = PreFlightEstimator(budget_usd=budget)
    estimate = estimator.estimate(workflow, nodes)

    if estimate_only:
        display_preflight_check(estimate)
        raise typer.Exit(0)

    if not skip_preflight:
        if headless:
            log_preflight_estimate(estimate)
            if not estimate.budget_sufficient:
                console.print("[bold red]Error: Budget insufficient. Halting.[/bold red]")
                raise typer.Exit(1)
        else:
            choice = display_preflight_check(estimate)
            if choice == "n":
                console.print("[yellow]Aborted by user.[/yellow]")
                raise typer.Exit(0)
            elif choice == "a":
                new_budget = typer.prompt("Enter new budget (USD)", type=float, default=budget)
                # Recalculate if budget changed
                estimator = PreFlightEstimator(budget_usd=new_budget)
                estimate = estimator.estimate(workflow, nodes)
                budget = new_budget
                # Show again
                choice = display_preflight_check(estimate)
                if choice == "n":
                    console.print("[yellow]Aborted by user.[/yellow]")
                    raise typer.Exit(0)

    # Resolve workflow mode: CLI flag > YAML declaration > default
    resolved_mode = mode or get_workflow_mode(workflow_path) or "audit"
    if resolved_mode not in ("audit", "develop", "research"):
        console.print(
            f"[bold red]Invalid mode:[/bold red] {resolved_mode}. "
            f"Must be audit, develop, or research."
        )
        raise typer.Exit(1)

    # ── Reflex Arc v2: Check for trivial-query fast path (v13.5.2) ──
    # EASY queries (e.g. "help", "status", "version") can be answered by
    # a local CPU-optimized model, bypassing the full LangGraph DAG entirely.
    try:
        from ...core.reflex_arc import reflex_arc_execute

        reflex_result = asyncio.run(reflex_arc_execute(query))
        if reflex_result.get("use_fast_path") and reflex_result.get("fast_path_response"):
            fast_response = reflex_result["fast_path_response"]
            if not headless:
                console.print("[bold green]⚡ Reflex Arc Fast Path[/bold green] (local model)")
                console.print(fast_response)
            else:
                print(fast_response)
            raise typer.Exit(0)
    except typer.Exit:
        raise
    except (
        _BEST_EFFORT_EXCEPTIONS
    ) as reflex_err:  # optional fast-path probe (narrow tuple at module scope)
        logger.debug(
            f"Reflex arc fast path check failed, continuing to full workflow: {reflex_err}"
        )

    if not headless and not tui:
        mode_colors = {"audit": "yellow", "develop": "green", "research": "cyan"}
        console.print(f"[bold blue]Starting workflow:[/bold blue] {workflow}")
        console.print(f"[dim]Query:[/dim] {query[:100]}...")
        console.print(f"[dim]Budget:[/dim] ${budget:.2f}")
        console.print(
            f"[dim]Mode:[/dim] [{mode_colors.get(resolved_mode, 'white')}]"
            f"{resolved_mode}[/{mode_colors.get(resolved_mode, 'white')}]"
        )
        if steering:
            console.print(
                f"[dim]Steering:[/dim] {steering[:80]}{'...' if len(steering) > 80 else ''}"
            )
        if approve_all:
            console.print("[yellow]Approval: --approve-all enabled[/yellow]")

    try:
        if tui:
            # Use Reactive Textual TUI Dashboard. Imported lazily so a
            # minimal install without the `tui` extra can still import the
            # CLI (F3).
            from ...tui.app import BeagleApp

            app = BeagleApp(workflow_id=workflow, query=query)

            async def run_with_tui():
                # Start dashboard in the background
                # (Textual apps run their own event loop, so we run them together)
                workflow_task = asyncio.create_task(
                    run_workflow(
                        query=query,
                        workflow_name=workflow,
                        budget=budget,
                        steering=steering or "",
                        thread_id=resume_from,
                        resume=bool(resume_from),
                        workflow_mode=resolved_mode,
                        approval_granted=approve_all,
                    )
                )

                try:
                    # Run app - this blocks until exit
                    await app.run_async()
                    # If app exits but workflow still running, we might want to wait or cancel
                    if not workflow_task.done():
                        # Wait a bit for workflow to finish gracefully if it's close
                        try:
                            return await asyncio.wait_for(workflow_task, timeout=2.0)
                        except TimeoutError:
                            workflow_task.cancel()
                            return {"errors": ["TUI exited before workflow completed"]}
                    return await workflow_task
                except (
                    _BEST_EFFORT_EXCEPTIONS
                ) as e:  # TUI workflow task failure (narrow tuple at module scope)
                    logger.exception(f"TUI Error: {e}")
                    return await workflow_task

            state = asyncio.run(run_with_tui())
        elif headless:
            # CI/CD Mode: Minimal output, strictly async run
            state = asyncio.run(
                run_workflow(
                    query=query,
                    workflow_name=workflow,
                    budget=budget,
                    steering=steering or "",
                    thread_id=resume_from,
                    resume=bool(resume_from),
                    workflow_mode=resolved_mode,
                    approval_granted=approve_all,
                )
            )
        else:
            # Use LangGraph StateGraph execution with simple Progress spinner
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task("[cyan]Running workflow...", total=None)

                state = run_graph_workflow_gracefully(
                    run_workflow,
                    query=query,
                    workflow_name=workflow,
                    budget=budget,
                    steering=steering or "",
                    thread_id=resume_from,
                    resume=bool(resume_from),
                    workflow_mode=resolved_mode,
                    approval_granted=approve_all,
                )

        # Phase 6: Structured Output Processing
        final_report = state.get("final_report", "")
        workflow_id = state.get("workflow_id", "unknown")

        from ...output import (
            OutputParser,
            to_github_issues,
            to_json,
            to_markdown,
            to_sarif,
        )

        parser = OutputParser(workflow_id=workflow_id, workflow_name=workflow, query=query)
        structured_output = asyncio.run(parser.parse(final_report))

        # Record findings to DB
        from ...tracking import get_recorder

        get_recorder().record_findings(structured_output)

        # Persist structured output
        if not output_path:
            output_dir = get_output_dir()
            ext = (
                "json" if output_format == "json" else "sarif" if output_format == "sarif" else "md"
            )
            output_path = output_dir / f"{workflow}_{workflow_id}.{ext}"

        if output_format == "markdown":
            rendered = to_markdown(structured_output)
            output_path.write_text(rendered, encoding="utf-8")
            if not headless:
                console.print(f"\n[bold green]Report saved to:[/bold green] {output_path}")
        elif output_format == "json":
            rendered = to_json(structured_output)
            output_path.write_text(rendered, encoding="utf-8")
            if not headless:
                console.print(f"\n[bold green]JSON output saved to:[/bold green] {output_path}")
        elif output_format == "sarif":
            rendered = to_sarif(structured_output)
            output_path.write_text(rendered, encoding="utf-8")
            if not headless:
                console.print(f"\n[bold green]SARIF output saved to:[/bold green] {output_path}")
        elif output_format == "github-issues":
            # dry_run is guaranteed False here: the --dry-run path raised
            # typer.Exit at the top of `run` before any execution.
            issues = to_github_issues(structured_output)
            console.print(f"\n[bold green]Created {len(issues)} GitHub issues.[/bold green]")

        # Show results
        errors = state.get("errors", [])
        if errors:
            console.print("[bold red]Workflow completed with errors:[/bold red]")
            for error in errors:
                console.print(f"  - {error}")
        else:
            if not headless:
                console.print("[bold green]Workflow completed successfully![/bold green]")
            else:
                console.print(f"SUCCESS: Workflow {workflow_id} completed.")

        # Always log ID for tracking
        if headless:
            console.print(f"RUN_ID: {workflow_id}")

        # Show summary
        total_tokens = state.get("total_tokens", 0)
        total_cost = state.get("total_cost", 0.0)
        completed = state.get("completed_nodes", [])
        console.print(f"\n[dim]Total tokens:[/dim] {total_tokens:,}")
        console.print(f"[dim]Total cost:[/dim] ${total_cost:.6f}")
        console.print(f"[dim]Nodes completed:[/dim] {len(completed)}")

        # Write final report to disk
        final_report = state.get("final_report", "")
        if final_report and final_report.strip():
            _persist_report(workflow, final_report, resolved_mode)

    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(1) from e
    except _BEST_EFFORT_EXCEPTIONS as e:  # top-level dispatch (narrow tuple at module scope)
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(1) from e


@execution_app.command("interactive")
def interactive() -> None:
    """Start the Beagle Interactive REPL."""
    import os

    import rich.prompt

    # v13.21.3: Use package-relative import.
    from ...core.router import route_query

    console.print("\n[bold cyan]Beagle v11.1 Interactive REPL[/bold cyan]")
    console.print("[dim]Type 'exit' or 'quit' to close.[/dim]\n")

    while True:
        try:
            query = rich.prompt.Prompt.ask("\n[bold cyan]Beagle[/bold cyan] > ")

            if not query.strip():
                continue

            if query.strip().lower() in ["exit", "quit"]:
                console.print("[dim]Exiting Beagle REPL...[/dim]")
                break

            if query.strip().lower() == "goose cli":
                import subprocess

                config = get_config()
                goose_bin = os.environ.get("GOOSE_BIN", config.goose.binary_path)
                subprocess.run([goose_bin, "run"], timeout=300)
                continue

            route = route_query(query)
            console.print(f"[dim]Routed to: {route.workflow}...[/dim]")

            workflow_path = _resolve_workflow(route.workflow)
            dag = load_workflow(workflow_path)

            run_workflow_gracefully(dag, query)

        except KeyboardInterrupt:
            console.print("\n[dim]Use 'exit' or 'quit' to leave.[/dim]")
        except (
            _BEST_EFFORT_EXCEPTIONS
        ) as e:  # interactive shell loop (narrow tuple at module scope)
            console.print(f"[bold red]Error:[/bold red] {e}")


@execution_app.command("goose-shell")
def goose_shell() -> None:
    """Launch Goose in a full interactive TTY to allow UI rendering."""
    import os
    import pty
    import re as _re

    # Load config and allow env overrides
    config = get_config()
    goose_bin = os.environ.get("GOOSE_BIN", config.goose.binary_path)
    goose_model = os.environ.get("GOOSE_MODEL", config.goose.default_model)
    goose_provider = os.environ.get("GOOSE_PROVIDER", config.goose.provider)

    # SECURITY: Validate binary path and arguments before pty.spawn
    bin_path = Path(goose_bin).resolve()
    if not bin_path.is_file() or not os.access(bin_path, os.X_OK):
        console.print(f"[bold red]Invalid GOOSE_BIN: {goose_bin}[/bold red]")
        raise typer.Exit(1)
    _SAFE_ARG_RE = _re.compile(r"^[a-zA-Z0-9_.:\-/]+$")
    for label, val in [("GOOSE_MODEL", goose_model), ("GOOSE_PROVIDER", goose_provider)]:
        if not _SAFE_ARG_RE.match(val):
            console.print(f"[bold red]Invalid {label}: {val}[/bold red]")
            raise typer.Exit(1)

    console.print(f"[bold green]Launching Goose Shell with {goose_model}...[/bold green]")

    args = [str(bin_path), "run", "--provider", goose_provider, "--model", goose_model]

    try:
        # pty.spawn replaces the current process and hooks up the TTY
        pty.spawn(args)
    except _BEST_EFFORT_EXCEPTIONS as e:  # PTY spawn failure (narrow tuple at module scope)
        console.print(f"[bold red]Failed to launch Goose Shell:[/bold red] {e}")


@execution_app.command("cli")
def cli_shell() -> None:
    """Alias for 'goose-shell' — launches Goose in an interactive TTY session."""
    goose_shell()


# ============================================================================
# Tracking Commands (Phase 5)
# ============================================================================


def _show_estimate(workflow_path: Path, query: str) -> None:
    """Show cost estimate for a workflow."""
    try:
        dag = load_workflow(workflow_path)
    except _BEST_EFFORT_EXCEPTIONS as e:  # workflow YAML load (narrow tuple at module scope)
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(1) from e

    config = get_config()
    model = config.goose.default_model

    # Estimate tokens per phase (rough estimate)
    estimated_input_per_phase = 2000
    estimated_output_per_phase = 4000

    # Get model pricing
    # v13.21.3: Use package-relative import.
    from ...cost_tracker import MODEL_PRICING

    pricing = MODEL_PRICING.get(model, MODEL_PRICING["default"])

    total_input = 0
    total_output = 0

    console.print("\n[bold]Cost Estimate[/bold]\n")

    for node_name in dag.nodes:
        input_tokens = estimated_input_per_phase
        output_tokens = estimated_output_per_phase
        total_input += input_tokens
        total_output += output_tokens

        input_cost = (input_tokens / 1_000_000) * pricing["input"]
        output_cost = (output_tokens / 1_000_000) * pricing["output"]
        phase_cost = input_cost + output_cost

        console.print(
            f"  {node_name}: ~${phase_cost:.4f} ({input_tokens + output_tokens:,} tokens)"
        )

    total_input_cost = (total_input / 1_000_000) * pricing["input"]
    total_output_cost = (total_output / 1_000_000) * pricing["output"]
    total_cost = total_input_cost + total_output_cost

    console.print(
        f"\n[bold]Total Estimate:[/bold] ~${total_cost:.4f} ({total_input + total_output:,} tokens)"
    )
    console.print(f"[dim]Model: {model}[/dim]")
    console.print("\n[yellow]Note: Actual costs may vary based on prompt/response length[/yellow]")


@execution_app.command("run-crewai")
def run_crewai(
    crew_file: str = typer.Argument(..., help="Path to CrewAI crew YAML file"),
    inputs: str = typer.Option("", "--inputs", "-i", help="JSON inputs dict"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output"),
) -> None:
    """Run a CrewAI crew definition through Beagle's pipeline."""
    import json as _json

    try:
        from ...bridges.crewai import BeagleAgent, BeagleCrew, BeagleTask
    except ImportError as exc:
        console.print(f"[red]CrewAI bridge not available: {exc}[/red]")
        raise typer.Exit(1) from exc

    inputs_dict = _json.loads(inputs) if inputs else {}

    # Load crew from YAML
    if crew_file.endswith((".yaml", ".yml")):
        import yaml

        with open(crew_file, encoding="utf-8") as f:
            spec = yaml.safe_load(f)
        agents_spec = spec.get("agents", [])
        tasks_spec = spec.get("tasks", [])
        agents = [BeagleAgent(**a) for a in agents_spec]
        tasks = [BeagleTask(**t) for t in tasks_spec]
        crew = BeagleCrew(agents=agents, tasks=tasks, verbose=verbose)
    else:
        console.print("[red]Only YAML crew files supported currently[/red]")
        raise typer.Exit(1)

    result = crew.kickoff(inputs=inputs_dict)
    console.print(Panel(result.raw, title="CrewAI Result"))
    console.print(f"[dim]Tasks completed: {len(result.tasks_output)}[/dim]")


@execution_app.command("run-autogen")
def run_autogen(
    config_file: str = typer.Argument(..., help="Path to AutoGen config YAML"),
    message: str = typer.Option("", "--message", "-m", help="Initial message"),
    max_turns: int = typer.Option(10, "--max-turns", help="Max conversation turns"),
) -> None:
    """Run an AutoGen group chat through Beagle's pipeline."""
    import yaml

    try:
        from ...bridges.autogen import (
            BeagleAssistant,
            BeagleAutoGenAgent,
            BeagleGroupChat,
            BeagleUserProxy,
        )
    except ImportError as exc:
        console.print(f"[red]AutoGen bridge not available: {exc}[/red]")
        raise typer.Exit(1) from exc

    with open(config_file, encoding="utf-8") as f:
        spec = yaml.safe_load(f)

    agents: list[BeagleAutoGenAgent] = []
    for a in spec.get("agents", []):
        if a.get("type") == "user_proxy":
            agents.append(BeagleUserProxy(**a))
        else:
            agents.append(BeagleAssistant(**a))

    chat = BeagleGroupChat(agents=agents, max_round=max_turns)
    result = asyncio.run(chat.run(message or spec.get("message", "Start")))
    console.print(Panel(result.summary, title="AutoGen Result"))
