"""Sandboxed Jinja2 environment factory for block rendering."""

from __future__ import annotations

import logging

logger = logging.getLogger("Beagle.blocks.jinja")

_jinja_env = None


def get_jinja_env():
    """Get the singleton sandboxed Jinja environment."""
    global _jinja_env
    if _jinja_env is None:
        try:
            from jinja2.sandbox import SandboxedEnvironment

            _jinja_env = SandboxedEnvironment(
                autoescape=False,
                trim_blocks=True,
                lstrip_blocks=True,
            )
            import json as _json

            import yaml as _yaml

            _jinja_env.filters.update(
                {
                    "to_yaml": lambda v: _yaml.safe_dump(v, default_flow_style=False),
                    "to_json": lambda v: _json.dumps(v),
                }
            )
        except ImportError:
            logger.warning("jinja2 not installed — block rendering unavailable")
            _jinja_env = None
    return _jinja_env


def render_template(template_text: str, context: dict | None = None) -> str:
    """Render a Jinja2 template string with sandboxing."""
    env = get_jinja_env()
    if env is None:
        raise RuntimeError("jinja2 is required for block rendering")
    template = env.from_string(template_text)
    rendered = template.render(context or {})
    return str(rendered)
