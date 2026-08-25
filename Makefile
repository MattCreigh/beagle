.PHONY: help lint test clean build install dev-deps check vulture typecheck banned qa

# The project venv, not the system interpreter. `python3` here resolved to
# /usr/bin/python3, which carries none of the dev tooling, so `make vulture`
# and `make check` exited 1 on "No module named vulture" instead of gating.
# Override for a different env:  make check PY=/path/to/python
VENV ?= /opt/beagle/beagle_venv
PY   ?= $(VENV)/bin/python

# v1.3.1: keep __pycache__ OUT of the source tree. Every python process
# started by make inherits this, so bytecode lands in ~/.cache/beagle/pycache
# instead of next to the .py files (a checkout stays clean of ephemeral junk).
export PYTHONPYCACHEPREFIX ?= $(HOME)/.cache/beagle/pycache

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

lint: ## Run ruff linter
	$(PY) -m ruff check src/beagle/ tests/
	$(PY) scripts/check_hook_health.py
	$(PY) scripts/check_plan_commands.py
	$(PY) scripts/check_quality_ratchet.py

lint-fix: ## Run ruff linter with auto-fix
	$(PY) -m ruff check --fix src/beagle/ tests/

vulture: ## Find dead code
	$(PY) -m vulture src/beagle/ tests/ .vulture_whitelist.py --min-confidence 90

typecheck: ## Run mypy on the source tree (SP-3: zero-error gate, audit E1)
	$(PY) -m mypy src

test: ## Run test suite (full failure set, no -x)
	# Honour pyproject.toml testpaths
	$(PY) -m pytest --timeout=120 -v --tb=short

test-fast: ## Run test suite, stop at the first failure
	# Honour pyproject.toml testpaths
	$(PY) -m pytest --timeout=120 -x -v --tb=short

test-cov: ## Run tests with coverage
	$(PY) -m pytest --timeout=30 -v --cov=beagle --cov-report=html --cov-report=term-missing

check: lint vulture typecheck ## Run all checks
	@echo "All checks passed"

clean: ## Remove build artifacts
	rm -rf build/ dist/ *.egg-info .pytest_cache .ruff_cache htmlcov .mypy_cache
	rm -rf src/beagle/__pycache__
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
	find . -type f -name '*.pyc' -delete 2>/dev/null

build: clean ## Build wheel and sdist
	uv build

install: build ## Build and install into current venv (CPU-only; --no-deps avoids GPU torch)
	uv pip install --reinstall --no-deps dist/beagle-*.whl
	uv pip install -r requirements.lock --no-deps

# H4 (MS-6): the deployed venv is the BLUE side of a red/blue split. It is
# NON-EDITABLE by design — the frozen wheel in site-packages is what every
# plan's /opt/...python3 verification actually executes. This target
# installs the [dev] extra WITHOUT -e, so a source edit cannot silently
# replace the wheel with a live view of src/.
dev-deps: ## Install the [dev] extra, non-editable (CPU-only index; never the GPU torch stack)
	uv pip install ".[dev]" --extra-index-url https://download.pytorch.org/whl/cpu --no-deps

locked-install: ## Install from locked requirements (reproducible)
	uv pip install -r requirements.lock --no-deps

freeze-requirements: ## Generate locked requirements file from the clean venv via uv export
	$(PY) -m uv export --no-dev --no-emit-project > requirements.lock

pip-audit: ## Audit transitive dependencies for known vulnerabilities
	# v1.0.4 (audit R9): pip-audit is now a declared dev dependency, so this
	# gate fails closed instead of silently echoing "not installed". Tracked
	# CVEs (pip-audit --strict, 2026-08-07): langgraph-checkpoint 4.0.2,
	# langchain 1.2.15, langchain-anthropic 1.4.1, click 8.3.2, mcp 1.27.0 —
	# see the dependency-bump follow-up queue before release.
	$(PY) -m pip_audit -r requirements.txt --strict

banned:  ## Check for banned coding patterns
	@echo "Checking for banned patterns..."
	@! grep -rn "datetime\.utcnow()" src/beagle/ --include="*.py" || (echo "FAIL: Use datetime.now(timezone.utc) instead of datetime.utcnow()" && exit 1)
	@! grep -rn "NamedTemporaryFile(delete=False)" src/beagle/ --include="*.py" || (echo "FAIL: Use delete=True + explicit cleanup instead of delete=False" && exit 1)
	@! grep -rn "uuid4()\[" src/beagle/ --include="*.py" || (echo "FAIL: Use full uuid4(), never truncated" && exit 1)
	@# SP-13: an in-memory elapsed-duration holder must use time.monotonic(), not
	@# time.time(). time.time() is a wall clock that can jump backwards under an
	@# NTP / DST step, producing a negative duration. The full holder matrix is
	@# guarded by tests/test_monotonic_clocks.py; this is a lightweight write-side
	@# guard for the two names that denote an in-memory elapsed holder.
	@! grep -rnE "(_last_fire_at|warm_worker.*created_at|cached_at)\s*=\s*time\.time\(\)" src/beagle/ --include="*.py" || (echo "FAIL: Use time.monotonic() for an in-memory elapsed-duration timestamp" && exit 1)
	@! grep -rn "yaml\.load(" src/beagle/ --include="*.py" | grep -v "yaml\.safe_load\|yaml\.FullLoader\|yaml\.SafeLoader" || (echo "FAIL: Use yaml.safe_load() instead of yaml.load()" && exit 1)
	@! grep -rn "pickle\.loads\|pickle\.load(" src/beagle/ --include="*.py" | grep -v "# pickle-ok" || (echo "FAIL: Avoid pickle.loads on untrusted data" && exit 1)
	@echo "All banned pattern checks passed"

qa: lint banned test  ## Run all quality checks (lint + banned + test)

render-prompts: ## Render all Beagle prompt-substrate files (XML/YAML only — no MD)
	$(PY) -m beagle.cli.cli render-prompts

render-hints: ## Render only the Top-of-Mind artefact (fast)
	$(PY) -m beagle.cli.cli render-hints