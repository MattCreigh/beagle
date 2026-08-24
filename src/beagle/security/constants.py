"""Security constants for Goose Agentic Workflow.

Module-level constants: limits, patterns, whitelists, and compiled regexes.
"""

from __future__ import annotations

import re

# Maximum query length to prevent token bombing
MAX_QUERY_LENGTH = 50000

# Maximum prompt length
MAX_PROMPT_LENGTH = 100000

# Semantic firewall timeout in seconds (configurable for deployment tuning)
SEMANTIC_FIREWALL_TIMEOUT = 15

# Semantic firewall model/provider — runtime defaults; override via env vars.
# FIREWALL_PROVIDER and FIREWALL_MODEL allow operators to swap the firewall
# model without code changes (e.g., air-gapped deployments, cost optimization).
# v1.0.9 (relay Task C): gemma3:27b was retired from the Ollama Cloud
# catalogue and is NOT on [models.allowed]; the cheap firewall model is now
# gemma4:31b (the allowlisted successor). A startup fail-early check in
# security/firewall.py validates this default against the allowlist.
DEFAULT_FIREWALL_PROVIDER = "ollama_cloud"
DEFAULT_FIREWALL_MODEL = "gemma4:31b"

# ── Injection patterns ─────────────────────────────────────────────────────────

# Patterns that indicate potential prompt injection
# Security-sensitive patterns kept in code, not configurable.
INJECTION_PATTERNS = [
    # Prompt override attempts
    r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?)",
    r"disregard\s+(all\s+)?(previous|prior|above)",
    r"forget\s+(everything|all)",
    r"override\s+(all\s+)?(previous|prior|system)\s+(instructions?|prompts?)",
    r"new\s+instructions?\s*:",
    r"you\s+are\s+now\s+a\s+different",
    r"pretend\s+you\s+are",
    r"act\s+as\s+if\s+you\s+are",
    # System prompt injection markers
    r"<system>",
    r"</system>",
    r"\[SYSTEM\]",
    r"\[INST\]",
    r"<<SYS>>",
    # Command substitution injection (backtick with $( inside)
    r"`[^`]*\$\(`",
    # Chained shell commands — require at least two metacharacters in sequence
    # to avoid false positives on URLs (&), JSON ($), and documentation
    r";\s*(rm|wget|curl|chmod|chown|dd|mkfs|shutdown|reboot)\b",
    r"\|\s*(bash|sh|zsh|python|perl|ruby|nc|ncat)\b",
]

# ── Secret patterns ────────────────────────────────────────────────────────────

# Patterns that match secrets to scrub
SECRET_PATTERNS = [
    # API keys and tokens (two patterns: space-separated and key=value)
    r"(?i)(api[_-]?key|apikey)\s*[=:]\s*['\"]?[a-zA-Z0-9_\-]{20,}['\"]?",
    r"(?i)(bearer|token)\s+[a-zA-Z0-9_\-\.]{20,}",
    # bearer/token followed by space or equals (e.g. "Bearer eyJ...", "token=eyJ...")
    # SECURITY: token=<value> with short values (6+ chars) also needs redaction
    r"(?i)(bearer|token)\s*[=:]\s*['\"]?[a-zA-Z0-9_\-\.]{6,}['\"]?",
    # auth/authorization (20+ char threshold for long bearer tokens)
    r"(?i)(auth|authorization)\s*[=:]\s*['\"]?[a-zA-Z0-9_\-\.]{20,}['\"]?",
    # Passwords and secrets (6+ char threshold: common passwords like "hunter2" must be caught)
    r"(?i)(password|passwd|pwd)\s*[=:]\s*['\"]?[^\s'\"]{6,256}['\"]?",
    r"(?i)(secret|credential)\s*[=:]\s*['\"]?[^\s'\"]{6,256}['\"]?",
    # AWS credentials (allow /+ in secret values)
    r"(?i)aws[_-]?(access[_-]?key[_-]?id|secret[_-]?access[_-]?key)\s*[=:]\s*['\"]?[A-Z0-9/+=]{20,}['\"]?",
    r"AKIA[0-9A-Z]{16}",
    # Private keys
    r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----",
    r"-----BEGIN\s+OPENSSH\s+PRIVATE\s+KEY-----",
    # Database URLs with passwords
    r"(?i)(postgres|mysql|mongodb|redis)://[^:]+:[^@]+@",
    # Generic key=value patterns for sensitive keys
    r"(?i)(jwt[_-]?secret|session[_-]?secret|encryption[_-]?key)\s*[=:]\s*['\"]?[^\s'\"]{16,}['\"]?",
    # GitHub tokens
    r"gh[pousr]_[A-Za-z0-9_]{36,}",
    # Age encryption keys
    r"AGE-SECRET-KEY-[A-Z0-9]{59}",
    # SOPS age key file paths with content
    r"(?i)age[_-]?key[_-]?file\s*[=:]\s*[^\s]+",
    # HashiCorp Vault tokens (s.xxxx or hvs.xxxx format)
    r"(?:^|\s|['\"])((?:hvs|s)\.[A-Za-z0-9]{20,})(?:\s|['\"]|$)",
    # Standalone JWT tokens (base64-encoded JSON header)
    r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}",
]

# ── AST validation constants ───────────────────────────────────────────────────

# REMOVED "Attribute" — it blocked all obj.attr access (false positives on
# legitimate code like `response.status`). The DANGEROUS_ATTRIBUTES set below
# and visit_Attribute() already catch malicious lookups (__import__, eval, etc.).
DANGEROUS_AST_NODES: frozenset[str] = frozenset()

# Dangerous attribute names when accessed via Attribute nodes
DANGEROUS_ATTRIBUTES = frozenset(
    {
        "__import__",
        "eval",
        "exec",
        "compile",
        "input",
        "__class__",
        "__bases__",
        "__subclasses__",
        "__globals__",
        "__builtins__",
    }
)

# Sensitive filesystem paths that should never be read in sandboxed code.
# Matches paths that leak system information or allow privilege escalation.
_SENSITIVE_PATH_PREFIXES = (
    "/etc/",
    "/etc\\",
    "/proc/",
    "/proc\\",
    "/sys/",
    "/sys\\",
    "/dev/",
    "/dev\\",
    "/run/udev/",
    "/var/run/",
    "/root/",
    "/shadow",
    "/passwd",
    "/ssh/",
    "/ssl/",
    "/private/",
    "/secret",
)

# Dangerous function calls
# v13.5.2: Added "globals", "locals", "vars" — sandbox escape vectors:
#   globals/locals/vars expose the runtime namespace for privilege escalation.
#   NOTE: "open" is NOT here — it's handled by mode/path checks below, since
#   open('file.txt', 'r') is safe but open('/etc/passwd') and open('f','w') are not.
DANGEROUS_CALLS = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
    }
)

# v13.4: Extended dangerous modules and calls
DANGEROUS_MODULE_CALLS = frozenset(
    {
        "system",  # os.system
        "popen",  # os.popen
        "spawn",  # os.spawn*
        "fork",  # os.fork
        "execve",  # os.execve
        "kill",  # os.kill
        "remove",  # os.remove / shutil.rmtree
        "rmtree",  # shutil.rmtree
        "call",  # subprocess.call
        "run",  # subprocess.run
        "Popen",  # subprocess.Popen
        "check_output",  # subprocess.check_output
        "check_call",  # subprocess.check_call
    }
)

DANGEROUS_MODULES = frozenset(
    {
        "os",
        "subprocess",
        "shutil",
        "ctypes",
        "socket",
        "http",
        "urllib",
        "requests",
        "pickle",
        "shelve",
        "marshal",
        "code",
        "codeop",
        "compileall",
        "importlib",
        "pkgutil",
        "sysconfig",
        # v13.17.1: Added per Gemini security audit — these modules enable
        # payload decoding and codec-based evasion of static string analysis.
        "binascii",
        "codecs",
    }
)

# ── Compiled regex patterns (performance) ──────────────────────────────────────

# Compiled patterns for efficiency
_INJECTION_REGEX = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE)
_SECRET_REGEXES = [re.compile(p) for p in SECRET_PATTERNS]


# v13.19.4: Public getter for the hard character cap on query strings.
# Tests in tests/test_validation_async.py and downstream callers
# import this function rather than the raw constant, so that future
# cap overrides via env var or config.toml can be implemented without
# changing the public surface.
def get_hard_char_cap() -> int:
    """Return the maximum number of characters allowed in a user query.

    The cap exists to prevent token-bombing attacks where a very long
    query consumes unbounded context-window budget. v13.19.4 introduces
    this getter as the public API; the underlying value is the same
    MAX_QUERY_LENGTH constant used elsewhere in the security module.
    """
    return MAX_QUERY_LENGTH
