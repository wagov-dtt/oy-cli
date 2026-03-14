# oy-cli

**Tiny AI coding assistant for your shell.** Reads files, runs commands, makes edits - nothing fancy.

```bash
uv tool install oy-cli
oy "add docstrings to public functions"
```

## Examples

```bash
# Basic usage
oy "read the main module and suggest improvements"

# Work in a specific directory
OY_ROOT=./my-project oy "fix the failing tests"

# Non-interactive mode (CI/pipelines)
echo "update the changelog" | OY_NON_INTERACTIVE=1 oy

# Security audit
oy audit
oy audit "focus on authentication"
```

## Commands

```bash
oy "prompt"              # Run with a prompt (default)
oy audit                  # Security audit against OWASP ASVS/MSVS
oy models                 # Interactive model picker
oy model                  # Show current default model
oy --help                 # Show all commands
```

## Why This Exists

Most AI coding tools are large, complex, or lock you into specific providers. `oy` is ~2000 lines of straightforward Python with a minimal tool surface: easy to audit, understand, and run safely.

**Design goals:** small auditable codebase, minimal tool surface (8 tools), OpenAI-compatible via completions API (including AWS Bedrock Mantle), fresh session each run, interactive checkpoints when needed.

## Tools

| Tool | Purpose | When to Use |
|------|---------|-------------|
| `read` | Read files/directories | Always read before editing |
| `apply` | Modify files | All file edits (replace/write/move/delete) |
| `list` | List directory contents | Exploring structure |
| `glob` | Find files by pattern | Know the pattern, not the path |
| `grep` | Search file contents | Find code by text/regex |
| `bash` | Run shell commands | Builds, tests, git, package managers |
| `httpx` | HTTP requests | Fetch docs, standards, API data |
| `ask` | Ask user questions | Interactive checkpoints |

**Key details:**
- `read`: Primary inspection tool with line numbers. Use `offset`/`limit` for large files.
- `apply`: Exact string replacement (read first!), write new files, move/delete.
- `grep`: Ripgrep preferred, returns `file:line` format, use `file_glob` to filter.
- `httpx`: Smart defaults - `preset="page"` for HTML->markdown, `json_path` for nested data.
- `bash`: Not for reading files. Output clips at ~16k, preserving head and tail.

## Agent Behavior

**Core workflow:** Inspect before changing, use the narrowest tool (grep -> read -> apply), batch operations, fresh session each run.

**Interactive mode:** Checkpoints for plans and risky changes, asks to commit after changes.

**Non-interactive mode:** No checkpoints, auto-recovers from failures, clear status when blocked.

**Output truncation:** Tools clip at ~16k chars (httpx: ~20k), bash preserves head and tail. Agent narrows queries when clipped. 

## Audit Command

Fetches current OWASP ASVS/MSVS standards, explores the repository, identifies security issues and complexity problems, writes findings to `ISSUES.md`.

```bash
oy audit                    # Full audit
oy audit "focus on auth"    # With focus area
OY_ROOT=./src oy audit      # Audit specific directory
```

## Configuration

**Environment variables:**

| Variable | Purpose |
|----------|---------|
| `OY_MODEL` | Override model for this session |
| `OY_NON_INTERACTIVE` | Set to `1` to disable checkpoints |
| `OY_ROOT` | Run against different workspace |
| `OY_SYSTEM_FILE` | Append extra system instructions |
| `OY_CONFIG` | Override config path (default: `~/.config/oy/config.json`) |

**Config file** (`~/.config/oy/config.json`):
```json
{"model": "moonshotai.kimi-k2.5"}
```

Default model: Kimi (moonshotai.kimi-k2.5). Use `oy models` to choose a different model.

## Requirements

- Python 3.14+
- `bash`
- (Optional) `rg` (ripgrep) for faster search
- OpenAI API key **OR** AWS CLI configured for Bedrock

## Installation

```bash
uv tool install oy-cli  # Preferred
pip install oy-cli       # Alternative
```

## Authentication

**OpenAI:**
```bash
export OPENAI_API_KEY=sk-...
```

For OpenAI-compatible endpoints:
```bash
export OPENAI_BASE_URL=https://your-endpoint.example/v1
export OPENAI_API_KEY=...
```

**AWS Bedrock (automatic):** Uses your default AWS profile/region. Supports auto-refresh of stale SSO sessions.
```bash
export AWS_PROFILE=my-profile
export AWS_REGION=us-west-2
```

## Troubleshooting

**"Missing API credentials"** -> Set `OPENAI_API_KEY` or configure AWS CLI (`aws configure`). For Bedrock: ensure your profile has `bedrock:InvokeModel` permission.

**"stdin is not a TTY"** -> Piping input disables `ask`. Set `OY_NON_INTERACTIVE=1` to make explicit.

**"AWS SSO session is stale"** -> Run `aws sso login --use-device-code --no-browser` or run `oy` in a TTY for auto-prompt.

**"command timed out"** -> `bash` default timeout is 120s. Agent can increase `timeout_seconds` parameter.

**"replace target not found"** -> `apply` requires exact string match. Read file first, check whitespace.

**Output truncated** -> Tools clip at ~16k chars. Agent auto-narrows queries, or guide explicitly: "read lines 100-200".

## Security

`oy` can run shell commands and modify files with your permissions. Recommended: run in containers, mount only needed directories, avoid exposing secrets.

**Automatic protections:** Redacts sensitive headers in `httpx` output, respects workspace boundaries, refreshes Bedrock tokens securely.

## Links

- [Issues](ISSUES.md) - Known issues and audit findings
- [Contributing](CONTRIBUTING.md) - Development and release notes

## License

Apache License 2.0
