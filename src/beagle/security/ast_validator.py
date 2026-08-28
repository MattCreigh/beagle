"""AST-based code validation for Goose Agentic Workflow.

Provides validate_python_code_ast(), _SecurityASTVisitor,
_ASTValidationStop, and _validate_import_node().
"""

from __future__ import annotations

import ast
import contextlib

from .constants import (
    _SENSITIVE_PATH_PREFIXES,
    DANGEROUS_ATTRIBUTES,
    DANGEROUS_CALLS,
    DANGEROUS_MODULE_CALLS,
    DANGEROUS_MODULES,
)

# ── AST validation ──────────────────────────────────────────────────────────────


def _validate_import_node(
    node: ast.AST,
    node_type: str,
    strict: bool,
    allowed_imports: set[str] | None,
) -> tuple[bool, str] | None:
    """Validate an Import or ImportFrom AST node against module policies.

    In strict mode: block dangerous modules, allow safe ones.
    In non-strict mode: block dangerous modules, allow if in allowed_imports
    or allow all if no allowed_imports filter is provided.

    Returns None if the import is acceptable, or (False, reason) tuple if blocked.
    """
    # Determine the module names being imported
    modules: list[str] = []
    if node_type == "Import":
        modules = [alias.name for alias in node.names]  # type: ignore[attr-defined]
    elif node_type == "ImportFrom" and node.module:  # type: ignore[attr-defined]
        modules = [node.module]  # type: ignore[attr-defined]

    for mod in modules:
        # Check top-level module and any parent packages
        mod_root = mod.split(".")[0]
        is_dangerous = mod_root in DANGEROUS_MODULES or mod in DANGEROUS_MODULES

        if strict:
            # Strict mode: block dangerous modules outright
            if is_dangerous:
                return False, f"Dangerous import blocked: {mod}"

            # Only allow explicitly allowed or known-safe modules
            _SAFE_STRICT_MODULES = frozenset(
                {
                    "json",
                    "math",
                    "datetime",
                    "collections",
                    "itertools",
                    "functools",
                    "operator",
                    "re",
                    "string",
                    "textwrap",
                    "typing",
                    "dataclasses",
                    "enum",
                    "abc",
                    "pathlib",
                    "decimal",
                    "fractions",
                    "statistics",
                    "random",
                    "copy",
                    "pprint",
                    "hashlib",
                    "base64",
                    "uuid",
                    "logging",
                    "traceback",
                    "inspect",
                    "dis",
                    "csv",
                    "io",
                    "struct",
                    "zlib",
                }
            )
            if allowed_imports is not None:
                # User-provided allowlist takes priority
                if mod_root not in allowed_imports and mod not in allowed_imports:
                    return False, f"Import not in allowed list: {mod}"
            else:
                # No explicit allowlist — use known-safe set
                if mod_root not in _SAFE_STRICT_MODULES and mod not in _SAFE_STRICT_MODULES:
                    return False, f"Import not allowed in strict mode: {mod}"
        else:
            # Non-strict mode: allow dangerous module imports (dangerous CALLS
            # like os.system() are still caught by the function-call check above).
            # Only block if an explicit allowed_imports filter was provided and
            # the module isn't in it.
            not_in_allowed = (
                allowed_imports is not None
                and mod_root not in allowed_imports
                and mod not in allowed_imports
            )
            if not_in_allowed:
                return False, f"Import not in allowed list: {mod}"

    return None  # Import is acceptable


class _ASTValidationStop(Exception):
    """Sentinel exception to short-circuit AST walk once a violation is found."""

    pass


class _SecurityASTVisitor(ast.NodeVisitor):
    """Single-pass AST visitor that checks all security rules in one walk.

    Replaces the previous O(n²) pattern of walking the AST 4 separate times
    (main checks, string concatenation, base64, f-strings).
    Cyclomatic complexity: ~3 per method (was CC≈43 for the monolithic function).
    """

    def __init__(self, strict: bool, allowed_imports: set[str] | None):
        self.strict = strict
        self.allowed_imports = allowed_imports
        self.violation: str = ""

    def _fail(self, message: str) -> None:
        """Record a violation and stop further visiting."""
        self.violation = message
        raise _ASTValidationStop()

    def visit_Import(self, node: ast.Import) -> None:
        """Check import statements via the shared import validator."""
        result = _validate_import_node(node, "Import", self.strict, self.allowed_imports)
        if result is not None:
            self._fail(result[1])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Check from-import statements via the shared import validator."""
        result = _validate_import_node(node, "ImportFrom", self.strict, self.allowed_imports)
        if result is not None:
            self._fail(result[1])
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Check for dangerous attribute access."""
        if node.attr in DANGEROUS_ATTRIBUTES:
            self._fail(f"Dangerous attribute access: {node.attr}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        """Check for dangerous bare name references."""
        if node.id in DANGEROUS_ATTRIBUTES or node.id in DANGEROUS_CALLS:
            self._fail(f"Dangerous name reference: {node.id}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Check for dangerous function calls."""
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in DANGEROUS_CALLS:
                self._fail(f"Dangerous function call: {func.id}")
            if func.id in DANGEROUS_MODULE_CALLS:
                self._fail(f"Dangerous module call: {func.id}")
            if func.id == "open":
                self._check_open_call(node)
            if func.id == "b64decode":
                self._fail("Base64 decode detected — potential payload obfuscation")
        elif isinstance(func, ast.Attribute):
            if func.attr in DANGEROUS_CALLS:
                self._fail(f"Dangerous function call: {func.attr}")
            if func.attr in DANGEROUS_MODULE_CALLS:
                self._fail(f"Dangerous module call: {func.attr}")
            if (
                isinstance(func.value, ast.Name)
                and func.value.id in DANGEROUS_MODULES
                and func.attr
                not in {
                    "path",
                    "environ",
                    "getcwd",
                    "listdir",
                    "stat",
                    "isdir",
                    "isfile",
                }
            ):
                self._fail(f"Dangerous module method: {func.value.id}.{func.attr}")
            if (
                func.attr == "b64decode"
                and isinstance(func.value, ast.Name)
                and func.value.id == "base64"
            ):
                self._fail("Base64 decode detected — potential payload obfuscation")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        """Check for dangerous subscript access (e.g., __class__.__bases__,
        __builtins__['eval'] — both Attribute and Name subscript targets)."""
        if isinstance(node.value, ast.Attribute) and node.value.attr in DANGEROUS_ATTRIBUTES:
            self._fail(f"Dangerous subscript on attribute: {node.value.attr}")
        # v13.17.1: Also catch Name-based subscript evasion:
        #   __builtins__['ev' + 'al'] → payload execution
        if isinstance(node.value, ast.Name) and node.value.id in DANGEROUS_ATTRIBUTES:
            self._fail(f"Dangerous subscript on builtin: {node.value.id}")
        self.generic_visit(node)

    @staticmethod
    def _fold_string_concat(node: ast.expr) -> str | None:
        """Recursively fold left-deep string concatenation trees.

        e.g., BinOp(BinOp(BinOp('e','v'),'a'),'l') → 'eval'

        Returns the folded string, or None if the tree contains non-string
        constants (indicating dynamic values that can't be statically folded).
        """
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = _SecurityASTVisitor._fold_string_concat(node.left)
            right = _SecurityASTVisitor._fold_string_concat(node.right)
            if left is not None and right is not None:
                return left + right
        return None

    def visit_BinOp(self, node: ast.BinOp) -> None:
        """Check for string concatenation obfuscating dangerous patterns.

        v13.17.1: Recursively folds left-deep trees (e.g., 'e'+'v'+'a'+'l'→'eval')
        to catch multi-fragment obfuscation, not just simple pairs.
        """
        if isinstance(node.op, ast.Add):
            folded = self._fold_string_concat(node)
            if folded is not None:
                for dangerous in DANGEROUS_CALLS:
                    if dangerous in folded:
                        self._fail(f"Obfuscated dangerous pattern detected: {dangerous}")
        self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        """Check for f-string injection patterns."""
        for value in node.values:
            if (
                isinstance(value, ast.FormattedValue)
                and isinstance(value.value, ast.Attribute)
                and value.value.attr in DANGEROUS_ATTRIBUTES
            ):
                self._fail(f"F-string injection detected: {{{{{value.value.attr}}}}}")
        self.generic_visit(node)

    def _check_open_call(self, node: ast.Call) -> None:
        """v13.5.2: Check open() comprehensively — mode + Path sensitivity."""
        is_write = False
        mode_str = None
        for arg in node.args[1:]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                mode_str = arg.value
                if any(m in arg.value for m in ("w", "a", "x")):
                    is_write = True
        for kw in node.keywords:
            if (
                kw.arg == "mode"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
            ):
                mode_str = kw.value.value
                if any(m in kw.value.value for m in ("w", "a", "x")):
                    is_write = True
        if is_write and self.strict:
            self._fail(f"open() write mode not allowed in strict mode: {mode_str!r}")
            return
        if node.args and isinstance(node.args[0], ast.Constant):
            path = str(node.args[0].value)
            if any(path.startswith(p) or p.strip("/\\") in path for p in _SENSITIVE_PATH_PREFIXES):
                self._fail(f"open() on sensitive path: {path!r}")


def validate_python_code_ast(
    code: object,
    strict: bool = True,
    allowed_imports: set[str] | None = None,
) -> tuple[bool, str]:
    """Validate Python code using AST parsing to detect dangerous constructs.

    Uses Python's ast module to parse code and detect potentially malicious
    constructs that regex-based validation would miss. This is more robust
    against obfuscation attempts.

    Args:
        code: Python code string to validate
        strict: If True, block all dynamic code execution. If False, allow
                safe imports but still block eval/exec/etc.
        allowed_imports: Set of module names allowed for import (strict mode only)

    Returns:
        Tuple of (is_valid, error_message)
        - (True, "") if code is safe
        - (False, "reason") if dangerous constructs detected

    Examples:
        >>> validate_python_code_ast("print('hello')")
        (True, "")

        >>> validate_python_code_ast("eval('1+1')")
        (False, "Dangerous function call: eval")

        >>> validate_python_code_ast("import os")
        (False, "Dangerous import blocked: os")

    """
    if not isinstance(code, str):
        return False, "Input must be a string"
    if not code or not code.strip():
        return False, "Empty code string"

    # Cap code length to prevent resource exhaustion
    MAX_CODE_LENGTH = 100_000  # 100KB
    if len(code) > MAX_CODE_LENGTH:
        return False, f"Code too long: {len(code)} chars (max {MAX_CODE_LENGTH})"

    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        return False, f"Syntax error: {e}"
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        return False, f"Failed to parse code: {e}"

    # Single-pass AST walk using NodeVisitor (replaces 4 separate ast.walk() calls)
    visitor = _SecurityASTVisitor(strict=strict, allowed_imports=allowed_imports)
    with contextlib.suppress(_ASTValidationStop):
        visitor.visit(tree)
    if visitor.violation:
        return False, visitor.violation

    return True, ""
