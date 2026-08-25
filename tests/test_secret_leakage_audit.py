"""Section 9.2: Audit all log statements for secret leakage.

Validates that no logger statement in the Beagle codebase
logs actual secret values, tokens, or API key contents.
Only key *names* (identifiers) are acceptable in logs.
"""

from __future__ import annotations

import re
from pathlib import Path

# Directories to audit
_AUDIT_DIRS = [
    "src",
    "beagle/bridges",
    "beagle/core",
    "beagle/infrastructure",
    "beagle/lifecycle",
]

# Patterns that indicate a secret VALUE is being logged (not just the key name)
# These match f-string or %-format patterns where a variable holding a secret
# value is interpolated into a log message.
_SECRET_VALUE_PATTERNS = [
    # Direct variable interpolation of known secret-holding variables
    re.compile(r"logger\.\w+\(.*\{value\}.*\)", re.IGNORECASE),
    re.compile(r"logger\.\w+\(.*\{secret\}.*\)", re.IGNORECASE),
    re.compile(r"logger\.\w+\(.*\{api_key\}.*\)", re.IGNORECASE),
    re.compile(r"logger\.\w+\(.*\{token_value\}.*\)", re.IGNORECASE),
    re.compile(r"logger\.\w+\(.*\{password\}.*\)", re.IGNORECASE),
    re.compile(r"logger\.\w+\(.*\{credential\}.*\)", re.IGNORECASE),
    re.compile(r"logger\.\w+\(.*%s.*secret", re.IGNORECASE),
]

# Allowed patterns — key names in quotes are OK (not the value)
_ALLOWED_PATTERNS = [
    r"auth_key",  # key *name* like "OLLAMA_CLOUD_API_KEY"
    r"key_name",  # key *name* identifier
    r"_A2A_SECRET_FILE",  # file path, not value
    r"key_file",  # file path, not value
    r"secrets_file",  # file path reference
    r"secret.*path",  # path to secret file
    r"secret.*not found",  # just saying it wasn't found
]


def _collect_python_files() -> list[Path]:
    """Collect all Python files in audit directories."""
    root = Path(".")
    files = []
    for audit_dir in _AUDIT_DIRS:
        audit_path = root / audit_dir
        if audit_path.exists():
            files.extend(audit_path.rglob("*.py"))
    return sorted(files)


def _is_allowed_log_line(line: str) -> bool:
    """Check if a log line is an allowed pattern (key names, paths)."""
    return any(re.search(pattern, line, re.IGNORECASE) for pattern in _ALLOWED_PATTERNS)


class TestNoSecretLeakage:
    """No logger statement leaks actual secret values."""

    def test_no_secret_values_in_logs(self):
        """Search all logger statements for secret value interpolation."""
        violations = []
        for py_file in _collect_python_files():
            content = py_file.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                # Skip comments
                if stripped.startswith("#"):
                    continue
                # Only check lines with logger calls
                if "logger." not in stripped:
                    continue
                # Check against forbidden patterns
                for pattern in _SECRET_VALUE_PATTERNS:
                    if pattern.search(stripped) and not _is_allowed_log_line(stripped):
                        violations.append(f"{py_file}:{i}: {stripped[:100]}")
        assert len(violations) == 0, (
            f"Found {len(violations)} log statements that may leak secrets:\n"
            + "\n".join(violations)
        )

    def test_secrets_loader_never_logs_values(self):
        """secrets_loader.py specifically never logs secret values."""
        repo_root = Path(__file__).resolve().parent.parent
        sl = repo_root / "src" / "beagle" / "secrets_loader.py"
        content = sl.read_text(encoding="utf-8")
        # The module should never log `value` or the loaded secret directly
        for i, line in enumerate(content.splitlines(), 1):
            if "logger." in line and "value" in line:
                # Only allow logging about absence, file path, or type
                assert not re.search(r"logger\.\w+\(.*\{value\}.*\)", line), (
                    f"secrets_loader.py:{i} logs secret value"
                )

    def test_no_bare_api_key_in_logs(self):
        """No logger statement contains a string literal that looks like an API key."""
        violations = []
        # Match string literals that look like API keys (40+ chars of base64/hex)
        key_literal = re.compile(r'logger\.\w+\(.*["\'][A-Za-z0-9+/=]{40,}["\']')
        for py_file in _collect_python_files():
            content = py_file.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(content.splitlines(), 1):
                if key_literal.search(line):
                    violations.append(f"{py_file}:{i}: {line.strip()[:100]}")
        assert len(violations) == 0, (
            f"Found {len(violations)} log statements with API-key-like literals:\n"
            + "\n".join(violations)
        )
