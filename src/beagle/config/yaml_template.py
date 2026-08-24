"""Jinja2 template preprocessing for YAML workflow files.

Renders ``{{ preset.xxx }}`` template variables in YAML workflow files
before ``yaml.safe_load()`` parses them. This lets develop.yaml and
metaprompts/*.yaml reference config.toml [model_presets] by name
instead of hardcoding model strings that drift on refresh.

Usage in workflow YAML::

    phases:
      - name: plan
        model: "{{ preset.orchestration }}"

Which gets rendered to::

    phases:
      - name: plan
        model: "glm-5.2:cloud"

before yaml.safe_load() parses it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger("Beagle.config.yaml_template")

try:
    from jinja2 import Environment, StrictUndefined, select_autoescape
except ImportError:  # pragma: no cover
    Environment = None  # type: ignore[assignment,misc]
    StrictUndefined = None  # type: ignore[assignment,misc]
    select_autoescape = None  # type: ignore[assignment,misc]


def _load_template_context(config_path: Path | None = None) -> dict[str, Any]:
    """Load preset values from the registry for Jinja rendering (plan v2).

    Reuses the same registry-backed context as the TOML template renderer —
    there is a single SSOT (providers.toml + presets.toml) and a single cache
    owner (registry.py, 7.3).
    """
    del config_path  # unused — registry owns file resolution
    from . import registry as _registry

    return _registry.build_template_context()


def _build_environment() -> Any:
    """Build the Jinja2 Environment used for YAML template rendering.

    WP-4 B9: autoescape is enabled for .yaml/.yml/.tpl files. The render
    context is scalar leaves from the registry (never user HTML), so escaping
    is a defense-in-depth guard against SSTI via a malicious template.
    """
    if Environment is None:
        raise RuntimeError(
            "jinja2 is required for YAML template rendering but is not installed. "
            "Install with: pip install jinja2"
        )
    from .toml_template import _toml_filter

    env = Environment(
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=select_autoescape(["yaml", "yml", "tpl"]),
    )
    env.filters["toml"] = _toml_filter
    return env


def _render(raw: str, context: dict[str, Any]) -> str:
    env = _build_environment()
    template = env.from_string(raw)
    return cast(str, template.render(**context))


def render_yaml_template(
    yaml_path: Path,
    config_path: Path | None = None,
) -> str:
    """Render Jinja2 templates in a YAML file, returning the processed text.

    Args:
        yaml_path: Path to the YAML file with Jinja templates.
        config_path: Ignored (kept for API back-compat).

    Returns:
        The rendered YAML text (ready for yaml.safe_load()).

    """
    if Environment is None:
        raise RuntimeError(
            "jinja2 is required for YAML template rendering but is not installed. "
            "Install with: pip install jinja2"
        )

    context = _load_template_context(config_path)
    raw = yaml_path.read_text(encoding="utf-8")
    rendered = _render(raw, context)

    logger.debug(
        "Rendered YAML template %s -> %d chars (from %d chars)",
        yaml_path,
        len(rendered),
        len(raw),
    )
    return rendered


def load_yaml_with_templates(
    yaml_path: Path,
    config_path: Path | None = None,
) -> Any:
    """Load a YAML file with Jinja2 template preprocessing.

    Renders Jinja templates in the file, then parses the result with
    yaml.safe_load(). This is the drop-in replacement for
    ``yaml.safe_load(open(path))`` when the YAML file contains
    ``{{ }}`` template variables.

    Args:
        yaml_path: Path to the YAML file with Jinja templates.
        config_path: Path to config.toml (auto-discovered if None).

    Returns:
        Parsed YAML dict.

    """
    import yaml

    rendered = render_yaml_template(yaml_path, config_path)
    return yaml.safe_load(rendered)


__all__ = [
    "load_yaml_with_templates",
    "render_yaml_template",
]
