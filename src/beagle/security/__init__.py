"""Security module — backward-compatible re-exports.

SP-12: the seven ``from .x import *`` statements are gone. A star import hides
which names this shim actually promises — ruff cannot tell whether a name is
re-exported deliberately or leaked by accident, which is why every line here
used to carry a suppression comment. The names are now listed explicitly and
repeated in ``__all__``, so the re-export contract is the code rather than a
comment, and adding a name to a submodule no longer silently widens this
module's public surface.

The old star imports also leaked stdlib names (``os``, ``re``, ``logging``,
``threading``, ``functools``, ``html``, ``signal``, ``subprocess``,
``tempfile``, ``contextlib``) and two names pulled in from the config shim
(``get_config`` and the goose-binary resolver). Nothing imports those from
here, so they are deliberately no longer re-exported.
"""

from __future__ import annotations

from .ast_validator import validate_python_code_ast
from .constants import (
    DANGEROUS_ATTRIBUTES,
    DANGEROUS_CALLS,
    DANGEROUS_MODULE_CALLS,
    DANGEROUS_MODULES,
    DEFAULT_FIREWALL_MODEL,
    DEFAULT_FIREWALL_PROVIDER,
    INJECTION_PATTERNS,
    MAX_PROMPT_LENGTH,
    MAX_QUERY_LENGTH,
    SECRET_PATTERNS,
    SEMANTIC_FIREWALL_TIMEOUT,
    get_hard_char_cap,
)
from .deserialization_guard import (
    safe_load_prompt,
    safe_loads,
)
from .firewall import (
    semantic_firewall,
    validate_firewall_model,
)
from .sanitization import (
    RegexTimeoutError,
    regex_search_safe,
    scrub_output,
    scrub_secrets,
)
from .validation import (
    SecurityContext,
    get_agent_whitelist,
    get_security_context,
    reset_security_context,
    sanitize_container_name,
    validate_agent_type,
    validate_file_path,
    validate_http_url,
    validate_prompt,
    validate_query,
    validate_query_async,
    validate_regex_pattern,
)
from .vigil import (
    validate_tool_output,
)

__all__ = [
    "DANGEROUS_ATTRIBUTES",
    "DANGEROUS_CALLS",
    "DANGEROUS_MODULES",
    "DANGEROUS_MODULE_CALLS",
    "DEFAULT_FIREWALL_MODEL",
    "DEFAULT_FIREWALL_PROVIDER",
    "INJECTION_PATTERNS",
    "MAX_PROMPT_LENGTH",
    "MAX_QUERY_LENGTH",
    "SECRET_PATTERNS",
    "SEMANTIC_FIREWALL_TIMEOUT",
    "RegexTimeoutError",
    "SecurityContext",
    "get_agent_whitelist",
    "get_hard_char_cap",
    "get_security_context",
    "regex_search_safe",
    "reset_security_context",
    "safe_load_prompt",
    "safe_loads",
    "sanitize_container_name",
    "scrub_output",
    "scrub_secrets",
    "semantic_firewall",
    "validate_agent_type",
    "validate_file_path",
    "validate_firewall_model",
    "validate_http_url",
    "validate_prompt",
    "validate_python_code_ast",
    "validate_query",
    "validate_query_async",
    "validate_regex_pattern",
    "validate_tool_output",
]
