"""Jinja2 template preprocessing for TOML config files (plan v2).

Renders template variables in consumer files (``agents.toml``, workflow YAML)
**before** ``tomllib.load()`` parses them. Per plan v2:

- The SSOT for model/provider/preset config is ``providers.toml`` +
  ``presets.toml``, loaded through :mod:`.registry` — rendering context comes
  from the registry, **not** from re-reading ``config.toml`` [model_presets]
  (B4: no bootstrap cycle; config.toml itself is Jinja-free).
- The context exposes **scalar leaves only** (B1): ``{{ preset.<role> }}``
  renders the bare model string, and ``{{ preset.<role>.provider }}`` /
  ``.model`` / ``.temperature`` give the upgraded leaf access. No whole-object
  render, no ``repr`` leakage (N1).
- ``env_flags.<bool>`` carries **booleans only** — secret values are never in
  the render context (B6). There is no ``env.<VAR>`` passthrough.
- The ``|toml`` filter emits type-correct TOML literals (B2): an unquoted
  ``temperature = {{ preset.default.temperature | toml }}`` stays a float, not
  the string ``"0.4"``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

from . import registry as _registry

logger = logging.getLogger("Beagle.config.toml_template")

try:
    from jinja2 import Environment, StrictUndefined, select_autoescape
except ImportError:  # pragma: no cover — jinja2 is in requirements.lock
    Environment = None  # type: ignore[assignment,misc]
    StrictUndefined = None  # type: ignore[assignment,misc]
    select_autoescape = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# |toml filter (B2) — emit a TOML-typed literal so unquoted ints/floats/bools
# and simple lists stay typed after tomllib parses them.
# ---------------------------------------------------------------------------


def _toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _toml_filter(value: Any) -> str:
    """Render *value* as a valid TOML inline literal (typed, not a string)."""
    if value is None:
        # TOML has no null; raise rather than silently emit a wrong type.
        raise ValueError("|toml filter cannot render None (TOML has no null)")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_filter(v) for v in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f"{_toml_string(str(k))} = {_toml_filter(v)}" for k, v in value.items())
        return "{ " + inner + " }"
    raise TypeError(f"|toml filter cannot render type {type(value).__name__}")


def _build_environment() -> Any:
    """Build the Jinja2 Environment used for TOML template rendering.

    WP-4 B9: autoescape is enabled for .toml/.tpl files. The render context
    is scalar leaves from the registry (never user HTML), so escaping is a
    defense-in-depth guard against SSTI via a malicious template.
    """
    if Environment is None:
        raise RuntimeError(
            "jinja2 is required for TOML template rendering but is not installed. "
            "Install with: pip install jinja2"
        )
    env = Environment(
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        comment_start_string="##",  # Avoid TOML # comment conflicts
        comment_end_string="##",
        autoescape=select_autoescape(["toml", "tpl"]),
    )
    env.filters["toml"] = _toml_filter
    return env


def _load_template_context(config_path: Path | None = None) -> dict[str, Any]:
    """Build the Jinja render context from the registry (plan v2).

    ``config_path`` is retained for signature back-compat; the registry resolves
    provider/preset/bundle files itself via ``_config_path``. Registered caches
    make repeated calls cheap.

    Returns the registry's scalar-leaf context (preset/provider/fallback/bundle
    plus boolean-only ``env_flags``).
    """
    del config_path  # unused — registry owns file resolution
    return _registry.build_template_context()


def render_toml_template(
    toml_path: Path,
    config_path: Path | None = None,
) -> str:
    """Render Jinja2 templates in a TOML file, returning the processed text.

    Args:
        toml_path: Path to the TOML file with Jinja templates.
        config_path: Ignored (kept for API back-compat); the registry resolves
            all required files.

    Returns:
        The rendered TOML text (ready for tomllib.loads()).

    """
    if Environment is None:
        raise RuntimeError(
            "jinja2 is required for TOML template rendering but is not installed. "
            "Install with: pip install jinja2"
        )

    context = _load_template_context(config_path)

    raw = toml_path.read_text(encoding="utf-8")

    env = _build_environment()

    template = env.from_string(raw)
    rendered = cast(str, template.render(**context))

    logger.debug(
        "Rendered TOML template %s -> %d chars (from %d chars)",
        toml_path,
        len(rendered),
        len(raw),
    )
    return rendered


def load_toml_with_templates(
    toml_path: Path,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Load a TOML file with Jinja2 template preprocessing.

    Renders Jinja templates in the file, then parses the result with tomllib.
    Drop-in replacement for ``tomllib.load()`` when the TOML contains ``{{ }}``.

    Args:
        toml_path: Path to the TOML file with Jinja templates.
        config_path: Ignored (kept for API back-compat).

    Returns:
        Parsed TOML dict.

    """
    try:
        import tomllib
    except ImportError:  # Python < 3.11
        import tomli as tomllib  # type: ignore[no-redef]

    rendered = render_toml_template(toml_path, config_path)
    return tomllib.loads(rendered)


__all__ = [
    "load_toml_with_templates",
    "render_toml_template",
]
