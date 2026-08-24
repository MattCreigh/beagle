# Codebase Analysis & Remediation Workflow

You are an autonomous software engineering agent. Your task is to analyze codebase reports and execute remediation actions systematically.

## Input Parameters

The following parameters should be provided when invoking this workflow:

| Parameter | Description | Default |
|-----------|-------------|---------|
| `REPORTS_DIR` | Directory containing analysis reports | `./analysis_reports/` |
| `PROJECT_ROOT` | Root directory of the project | `.` (current directory) |
| `PRIORITIES` | Comma-separated: `critical,high,medium,low` | `critical,high` |
| `PHASES` | Which phases to run: `triage,security,architecture,docs,all` | `all` |
| `DRY_RUN` | If true, only plan without making changes | `false` |

## Reports Structure

The workflow expects reports in subdirectories:

```
${REPORTS_DIR}/
├── README.md              # Index of all reports
├── architecture/          # C4 models, dependency graphs, technical debt
├── security/              # OWASP findings, CVSS scores, vulnerabilities
├── performance/           # Latency profiles, optimization recommendations
├── protocol/              # Protocol analysis, correctness proofs
├── documentation/          # Doc gaps, README audit
└── business/              # Patent inventory, valuation metrics
```

## Execution Phases

### Phase 1: Triage (Read-Only)
1. Read `${REPORTS_DIR}/README.md` to understand report structure
2. Extract findings matching specified priorities from each report
3. Create prioritized remediation queue ordered by:
   - Severity (CRITICAL > HIGH > MEDIUM > LOW)
   - Impact (security > performance > maintainability)
   - Effort (low effort fixes first within same priority)

### Phase 2: Security Fixes
For each security finding matching priorities:
1. Analyze the vulnerability and root cause
2. Implement fix following secure coding practices
3. Write or update tests to prevent regression
4. Document the fix in a CHANGELOG or remediation log

### Phase 3: Architecture Improvements
For each architecture finding:
1. Assess impact of change on existing code
2. Refactor incrementally (small commits)
3. Run test suite after each change
4. Update architecture documentation

### Phase 4: Documentation
- Update stale README files with current information
- Create architecture diagrams (Mermaid, PlantUML)
- Ensure inline docstrings match implementation
- Sync any paired documentation files

## Constraints
- All code changes must pass existing tests
- Use appropriate agent/delegate for complex changes
- Commit atomically (one fix per commit)
- Document all changes in remediation log
- On failure: log, revert, create TODO, continue

## Output

Writes execution log to `${REPORTS_DIR}/remediation_log.md`:

```markdown
# Remediation Log

## [TIMESTAMP] Finding: [FINDING_ID]
- **Severity**: CRITICAL|HIGH|MEDIUM|LOW
- **File**: [AFFECTED_FILE]
- **Action**: [WHAT_WAS_DONE]
- **Result**: SUCCESS|ROLLBACK|SKIPPED
- **Commit**: [HASH] (if applicable)
- **Notes**: [ANY_NOTES]
```

## Invocation Examples

### Full Workflow (All Phases)
```bash
goose run --recipe analyze_and_remediate \
  --params REPORTS_DIR=/path/to/analysis_reports \
  --params PROJECT_ROOT=/path/to/project
```

### Security-Only (Fast)
```bash
goose run --recipe analyze_and_remediate \
  --params PHASES=triage,security \
  --params PRIORITIES=critical,high
```

### Dry Run (Plan Only)
```bash
goose run --recipe analyze_and_remediate \
  --params DRY_RUN=true
```

### With Custom Report Location
```bash
goose run --recipe analyze_and_remediate \
  --params REPORTS_DIR=./reports \
  --params PROJECT_ROOT=~/projects/myapp
```

## Success Criteria
- [ ] All findings at specified priorities addressed
- [ ] All tests pass after changes
- [ ] Documentation updated to reflect changes
- [ ] Remediation log complete and accurate

## Rollback Plan
If any fix causes test failures:
1. `git revert HEAD` to undo last commit
2. Log the failure with details
3. Create TODO for manual review
4. Continue with next finding

## Estimated Execution
- Phase 1 (Triage): 5-15 min, read-only
- Phase 2 (Security): 30-60 min per CRITICAL/HIGH finding
- Phase 3 (Architecture): 1-2 hours per major refactor
- Phase 4 (Documentation): 30-60 min total