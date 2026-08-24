"""Configuration management commands."""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from ...config.config import get_config

console = Console()
config_app = typer.Typer(help="Configuration management")


@config_app.command("show")
def config_show() -> None:
    """Show current configuration."""
    config = get_config()
    from dataclasses import asdict

    console.print_json(json.dumps(asdict(config), indent=2))


@config_app.command("validate")
def config_validate() -> None:
    """Validate the configuration by loading it."""
    try:
        get_config()
    except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        # broad catch intentional -- CLI must surface any config error
        console.print(f"[red]Configuration is INVALID:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print("[green]Configuration is valid.[/green]")


@config_app.command("cards")
def config_cards() -> None:
    """List the preset cards in the presets/ directory and their contents."""
    from ...config import registry
    from ...config._config_path import find_preset_cards

    cards = find_preset_cards()
    if not cards:
        console.print("[yellow]No preset cards found.[/yellow]")
        return

    table = Table(title="Preset Cards (load order)", show_lines=True)
    table.add_column("Card file", style="bold cyan")
    table.add_column("Role presets", style="white")
    table.add_column("Bundles", style="white")

    active = registry.active_bundle()
    for card in cards:
        try:
            import tomllib

            with open(card, "rb") as f:
                data = tomllib.load(f)
            roles = ", ".join(sorted((data.get("presets") or {}).keys())) or "—"
            bundles = ", ".join(sorted((data.get("bundles") or {}).keys())) or "—"
        except (OSError, ValueError):
            roles = "(unreadable)"
            bundles = "(unreadable)"
        table.add_row(card.name, roles, bundles)

    console.print(table)
    if active:
        console.print(f"[green]Active bundle:[/green] {active}")


@config_app.command("schema")
def config_schema(
    output_format: str = typer.Option(
        "table", "--format", "-f", help="Output format: table, json, toml"
    ),
) -> None:
    """Display the configuration schema with types, defaults, and constraints."""
    from dataclasses import asdict, fields

    from ...config.config import (
        BudgetConfig,
        ContextThresholdConfig,
        GooseConfig,
        LLMConfig,
        MemoryConfig,
        OrchestratorConfig,
        PoolConfig,
        SecurityConfig,
    )

    # Schema metadata for each config section
    schema: dict[str, dict[str, Any]] = {
        "orchestrator": {
            "class": OrchestratorConfig,
            "description": "Workflow orchestration settings",
            "constraints": {
                "timeout_seconds": "Must be > 0",
                "max_retries": "Must be >= 0",
            },
        },
        "goose": {
            "class": GooseConfig,
            "description": "Goose binary and model settings",
            "constraints": {
                "binary_path": "Must be executable",
                "default_model": "Must be a valid model name",
            },
        },
        "llm": {
            "class": LLMConfig,
            "description": "Global LLM defaults — fallback when agents.toml is missing",
            "constraints": {
                "default_provider": "Must match a supported provider",
                "default_model": "Must be a valid model identifier for the provider",
                "cheap_model": "Used for security firewall and utility tasks",
                "cheap_provider": "Must match a supported provider",
            },
        },
        "budget": {
            "class": BudgetConfig,
            "description": "Cost and budget limits",
            "constraints": {
                "default_usd": "Must be > 0",
                "hard_limit_usd": "Must be > default_usd",
                "warn_threshold": "Between 0 and 1",
            },
        },
        "memory": {
            "class": MemoryConfig,
            "description": "Memory index and hierarchy settings",
            "constraints": {
                # nosec B105 - a constraint description keyed by a config name that
                # happens to contain "token"; not a credential.
                "index_token_budget": "Must be >= 500 (enforced minimum)",  # nosec B105
                "index_prune_strategy": "One of: oldest_first, relevance_weighted, hybrid",
                "working_memory_ttl": "Must be > 0 (seconds)",
            },
        },
        "security": {
            "class": SecurityConfig,
            "description": "Security and input validation settings",
            "constraints": {
                "max_query_length": "Must be > 0",
                "strict_code_validation": "Boolean",
            },
        },
        "pool": {
            "class": PoolConfig,
            "description": "Subprocess pool settings",
            "constraints": {
                "max_workers": "Must be > 0",
            },
        },
        "context_threshold": {
            "class": ContextThresholdConfig,
            "description": "Context window utilization thresholds",
            "constraints": {
                "warning": "Between 0 and 1 (fraction)",
                "compact": "Between 0 and 1, should be > warning",
                "critical": "Between 0 and 1, should be > compact",
            },
        },
    }

    config = get_config()

    if output_format == "json":
        result: dict[str, Any] = {}
        for section_name, section_info in schema.items():
            cfg_section = getattr(config, section_name, None)
            if cfg_section is not None:
                result[section_name] = {
                    "description": section_info["description"],
                    "fields": {
                        f.name: {
                            "type": f.type.__name__ if hasattr(f.type, "__name__") else str(f.type),
                            "default": getattr(cfg_section, f.name, None),
                        }
                        for f in fields(cfg_section)  # type: ignore[arg-type]
                    },
                    "constraints": section_info.get("constraints", {}),
                }
        console.print_json(json.dumps(result, indent=2))

    elif output_format == "toml":
        console.print("# Beagle Configuration Schema")
        console.print("# Generated by 'beagle config schema --format toml'\n")
        config_dict = asdict(config)
        for section, values in config_dict.items():
            console.print(f"[{section}]")
            for key, value in values.items():
                if isinstance(value, str):
                    console.print(f'{key} = "{value}"')
                elif isinstance(value, bool):
                    console.print(f"{key} = {str(value).lower()}")
                else:
                    console.print(f"{key} = {value}")
            console.print()

    else:  # table format (default)
        table = Table(title="Beagle Configuration Schema", show_lines=True)
        table.add_column("Section", style="bold cyan")
        table.add_column("Field", style="white")
        table.add_column("Type", style="dim")
        table.add_column("Default", style="green")
        table.add_column("Constraints", style="yellow")

        for section_name, section_info in schema.items():
            cfg_section = getattr(config, section_name, None)
            if cfg_section is None:
                continue
            constraints = section_info.get("constraints", {})
            for idx, f in enumerate(fields(cfg_section)):  # type: ignore[arg-type]
                constraint = constraints.get(f.name, "")
                type_str = f.type.__name__ if hasattr(f.type, "__name__") else str(f.type)
                default_val = getattr(cfg_section, f.name, None)
                table.add_row(
                    section_name if idx == 0 else "",
                    f.name,
                    type_str,
                    str(default_val),
                    constraint,
                )

        console.print(table)


@config_app.command("init")
def config_init(
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing config"),
) -> None:
    """Initialize default configuration file."""
    from ...config.config import get_config_path, save_default_config

    path = get_config_path()
    if path.exists() and not force:
        console.print(f"[yellow]Config already exists at {path}[/yellow]")
        console.print("Use --force to overwrite")
        return

    save_default_config()
    console.print(f"[green]Created config at {path}[/green]")
