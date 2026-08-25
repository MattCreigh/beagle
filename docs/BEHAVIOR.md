# Beagle Agent Behavior Configuration

## Auto-Creation of Missing Files

### Problem

The Beagle delegate agent previously checked for the existence of required files (test files,
configuration stubs, output files) but did **not** create them when missing. This caused
workflow failures when expected files were absent — the agent would abort rather than
create the necessary files.

### Solution

As of Beagle v13.6.0, the `SafeFileWriter` and `safe_file_ops` module automatically creates
missing files with appropriate default content. This behavior is **enabled by default** and
can be controlled via configuration.

### Configuration

#### config.toml

```toml
[behavior]
auto_create_missing_files = true    # Auto-create missing files during workflow execution
safe_file_operations = true         # Use SafeFileWriter for all agent file operations
```

#### Environment Variables

| Variable | Values | Default | Description |
|---|---|---|---|
| `BEAGLE_AUTO_CREATE_MISSING_FILES` | `true`, `1`, `yes` / `false`, `0`, `no` | `true` | Enable/disable auto-creation |
| `BEAGLE_SAFE_FILE_OPS` | `true`, `1`, `yes` / `false`, `0`, `no` | `true` | Enable/disable safe file operations |

#### Runtime API

```python
from beagle.utils.safe_file_ops import configure_auto_create, is_auto_create_enabled

# Disable at runtime
configure_auto_create(False)

# Check current state
if is_auto_create_enabled():
    print("Auto-creation is enabled")
```

### File Templates

When a file is auto-created, default content is inferred from the file type:

| Suffix | Template | Content |
|---|---|---|
| `test_*.py` | PYTEST | Pytest class with placeholder tests |
| `*.py` | PYTHON | Minimal Python module with `main()` |
| `*.yaml` / `*.yml` | YAML | YAML comment header |
| `*.toml` | TOML | TOML comment header |
| `*.json` | JSON | Empty JSON object |
| `*.md` | MARKDOWN | Markdown heading with placeholder |
| Other | TEXT | Generic placeholder text |

### API Reference

#### Core Functions

- **`ensure_file_exists(path, default_content=None,
  template=None)`** — Create missing file with default content.
  Returns `Path`.
- **`ensure_test_file_exists(path, class_name=None,
  module_path=None)`** — Create missing pytest file with proper
  boilerplate.
- **`safe_read(path, default_content=None, template=None)`** —
  Read file, creating it if missing. Returns content string.
- **`safe_write(path, content, create_if_missing=True)`** — Write
  file, optionally creating it. Returns `Path`.
- **`ensure_recipe_exists(recipe_path)`** — Create missing agent
  recipe stub.

#### Context Manager

```python
with SafeFileWriter("/path/to/output.py", default_content="# Auto-generated\n") as f:
    f.write("print('hello')")
```

### System Directive

The agent system directive (`SYSTEM_DIRECTIVE_TEMPLATE`) has been updated to include:

> CRITICAL 0A: If a required file does not exist, CREATE IT with appropriate content.
> Do NOT skip or delegate file creation. If you need to write to a file that doesn't
> exist, create the parent directories and write the file.

### Disabling Auto-Creation

When auto-creation is disabled (`auto_create_missing_files = false`), all safe file
operations raise `FileNotFoundError` instead of creating files. This maintains the
original behavior for environments where file creation should be explicit.

### Success Criteria

- [x] Workflows no longer fail due to missing expected files; files are created on-demand
- [x] The delegate agent correctly writes missing test files, configuration stubs, and output files
- [x] The behavior is controlled by a configuration flag
- [x] Documented in docs/BEHAVIOR.md
