# Research Codebase Analysis Workflow

You are an autonomous software engineering agent conducting systematic research and comparison analysis.

## Input Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `TARGET_CODEBASE` | Path to the codebase to analyze | `.` |
| `RESEARCH_TOPICS` | Comma-separated topics to research | `architecture,patterns,performance` |
| `EXTERNAL_REFERENCES` | External systems/papers to compare against | (none) |
| `OUTPUT_DIR` | Directory for analysis output | `./research_output/` |
| `DEPTH` | Analysis depth: `quick,standard,deep` | `standard` |

## Research Topics

### Architecture Analysis
- Overall system architecture and component boundaries
- Design patterns in use (and anti-patterns)
- Dependency graphs and coupling analysis
- Scalability considerations

### Code Patterns
- Common patterns and idioms
- Error handling strategies
- Async/concurrency patterns
- Testing patterns and coverage

### Performance
- Hot paths and bottlenecks
- Caching strategies
- Resource utilization patterns
- Latency profiles

### Security
- Authentication/authorization patterns
- Data handling and sanitization
- Dependency vulnerability analysis
- Attack surface assessment

## Execution Phases

### Phase 1: Discovery
1. Scan codebase structure (tree, file types, size)
2. Identify entry points and core modules
3. Map dependencies (imports, external libs)
4. Detect configuration and environment handling

### Phase 2: Deep Analysis
1. Extract architectural patterns from code
2. Identify code smells and technical debt
3. Measure complexity metrics (cyclomatic, cognitive)
4. Analyze test coverage and quality

### Phase 3: Comparison (if EXTERNAL_REFERENCES provided)
1. Research each reference system/pattern
2. Identify strengths vs target codebase
3. Document gaps and improvement opportunities
4. Propose concrete enhancements

### Phase 4: Report Generation
1. Generate structured markdown report
2. Include diagrams where helpful (Mermaid)
3. Prioritize findings by impact and effort
4. Provide actionable recommendations

## Output Structure

```
${OUTPUT_DIR}/
├── summary.md              # Executive summary
├── architecture.md          # Architecture analysis
├── patterns.md              # Code patterns analysis
├── performance.md           # Performance analysis
├── security.md              # Security analysis
├── comparison.md            # External comparisons (if any)
├── recommendations.md       # Prioritized recommendations
└── diagrams/                # Generated diagrams
```

## Invocation Examples

### Standard Analysis
```bash
goose run --recipe research_codebase \
  --params TARGET_CODEBASE=/path/to/project
```

### Deep Analysis with External References
```bash
goose run --recipe research_codebase \
  --params DEPTH=deep \
  --params EXTERNAL_REFERENCES="LangGraph,LangChain,CrewAI"
```

### Quick Security Scan
```bash
goose run --recipe research_codebase \
  --params RESEARCH_TOPICS=security \
  --params DEPTH=quick
```

## Success Criteria
- [ ] All specified topics analyzed
- [ ] Report generated with actionable findings
- [ ] Recommendations prioritized by impact
- [ ] Diagrams created for complex concepts

## Estimated Execution
- Quick (topics=1): 5-10 min
- Standard (topics=2-3): 30-60 min
- Deep (topics=4+): 1-3 hours