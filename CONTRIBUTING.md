# Contributing to Beagle — Goose Agentic Workflow

Thanks for your interest in contributing to **Beagle** (Goose Agentic Workflow).
This document explains the development workflow, coding standards, and review
process. It is a living document — when in doubt, follow the patterns already
present in the codebase and ask in a PR.

---

## Quick links

- **Issue tracker:** GitHub Issues
- **Architecture overview:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- **Doctrine (rules the codebase enforces on itself):** [`docs/DOCTRINE.md`](docs/DOCTRINE.md)
- **Security model:** [`docs/SECURITY.md`](docs/SECURITY.md)
- **API reference:** [`docs/API.md`](docs/API.md)
- **CLI reference:** [`docs/CLI.md`](docs/CLI.md)
- **Changelog:** [`CHANGELOG.md`](CHANGELOG.md)

---

## Ground rules

1. **Read the doctrine first.** Beagle's [`docs/DOCTRINE.md`](docs/DOCTRINE.md)
   is a set of mechanical rules the codebase enforces on itself (no bare
   `except Exception:`, no `print()` in library code, no hardcoded version
   strings, etc.). Tests in `tests/test_doctrine_*.py` will fail your PR if
   you violate them.
2. **Small, focused PRs.** One concern per PR. If your change touches >500
   lines, split it.
3. **Conventional Commits.** Use `feat:`, `fix:`, `docs:`, `refactor:`,
   `test:`, `chore:`, `security:`, `perf:`. The CHANGELOG is generated from
   these.
4. **Tests required for new code.** New public functions or behaviors need
   at least one test. Security-critical code (under `beagle/security/`)
   needs property-based tests.
5. **No silent failures.** All `except` blocks must either re-raise, log
   explicitly, or have a `# broad catch intentional` justification comment
   *plus* a debug-level log.

---

## Development setup

```bash
# Clone & install
git clone https://github.com/<org>/beagle
cd beagle
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Install pre-commit hooks
pre-commit install
pre-commit install --hook-type push

# Verify the install
pytest tests/test_doctrine_*.py -v
```

Hardware acceleration is **off by default** (see `constraints/cpu-only.txt`).
For GPU dev, comment out that constraint in your local venv.

---

## Coding standards

### Style

- **Formatter:** `ruff format` (Black-compatible, 100-col)
- **Linter:** `ruff check` (E, F, W, I, B, UP, SIM, RUF, N rules)
- **Type checker:** `mypy --strict` (run in CI)
- **Docstrings:** Google style on all public functions and classes
- **Logging:** `logger = logging.getLogger("Beagle.<module>")` — never
  `print()` in library code; only `scripts/` and `benchmarks/` may `print()`.

### Versioning

The single source of truth for the package version is
`beagle/__version__`. Do **not** hardcode version strings
elsewhere. The pre-commit hook
`no-hardcoded-version-string` will reject any file containing
`# v1`, `# v2`, etc. (Note: existing audit-trail comments like
`# v13.21.3: was...` are legitimate history and are exempted; the hook
scans for *new* occurrences only.)

### Magic numbers & timeouts

All thresholds, timeouts, and constants live in
`beagle/constants.py`. Import from there — never
inline a number that has policy significance (compaction ratios, retry
counts, hard caps, timeouts, port numbers, etc.).

### Security

See [`docs/SECURITY.md`](docs/SECURITY.md). Key rules:

- All path operations must use `Path.relative_to()` for containment
  checks (never `str.startswith`).
- All MCP tool schemas must include `"additionalProperties": false`.
- All user input must be validated at the boundary; never trust
  sub-process output verbatim.
- The `google-re2` secret scrubber is **required** at runtime; the
  system must fail closed if it's missing.

---

## Test-driven workflow

1. **Write a failing test first** (TDD). Use the test files under
   `tests/test_<module>.py` corresponding to your change.
2. **Implement the change** to make the test pass.
3. **Run the full suite**: `pytest tests/ -v --tb=short`
4. **Run doctrine tests explicitly**: `pytest tests/test_doctrine_*.py -v`
5. **Run lint + type-check**: `ruff check . && mypy beagle/`

Property-based tests (using `hypothesis`) are required for:

- Path containment (security)
- AST validation
- Deserialization
- Firewall rules
- Schema hardener

Fuzz tests live in `tests/fuzz/` and run on a slow schedule in CI.

---

## Pull request process

1. **Branch from `main`**: `git checkout -b feat/short-description`
2. **Commit with conventional messages**:
   `feat(rag): add per-tenant rate limiting`
3. **Push & open a PR** against `main`.
4. **CI must be green**: lint, type-check, tests, security scan, SBOM.
5. **CODEOWNERS** auto-assigns reviewers. Security-sensitive files
   (`beagle/security/**`) require sign-off from `@beagle-security-team`.
6. **Squash-merge** with a conventional-commit message.

---

## Adding a new workflow

1. Create `beagle/workflows/<name>.yaml`.
2. Validate against `beagle/core/workflow_schema.py`.
3. Register in `beagle/core/workflow_loader.py`.
4. Add integration tests under `tests/test_workflow_<name>.py`.
5. Document in `docs/CLI.md` (the `# beagle run` section).

---

## Adding a new MCP tool

1. Define the tool in the appropriate server module
   (`infrastructure/mcp_*.py`).
2. Add a JSON schema for the parameters. **Must** include
   `"additionalProperties": false`.
3. The central `mcp_schema_hardener.py` will reject any schema missing
   this — verify your schema survives a unit test in
   `tests/test_mcp_schema_hardener.py`.
4. Add a rate-limit decorator (the default is conservative; loosen only
   with justification).
5. Add a tool description and an example call in `docs/API.md`.

---

## Release process

1. Bump `__version__` in `beagle/__init__.py`.
2. Update `CHANGELOG.md` (auto-generated from conventional commits by
   `scripts/release_notes.py`).
3. Tag: `git tag -a v<version> -m "Release v<version>"`.
4. Push the tag: `git push origin v<version>`.
5. CI publishes to PyPI and builds SBOMs.

---

## Code of conduct

Be kind. Assume good faith. Disagree on substance, not on people. The
maintainers reserve the right to close unproductive threads.

---

## License

Beagle is proprietary software. All rights reserved. By contributing, you
agree that your contributions become the property of the copyright holder and
are licensed under the same proprietary terms as the rest of the project. No
usage, copying, modification, or distribution is permitted without a separate
commercial license. See [LICENSE](LICENSE) for the full terms.
