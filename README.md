# oy-cli

**Tiny AI coding assistant for your shell.** Reads files, runs commands, makes edits—nothing fancy.

```bash
uv tool install oy-cli
oy "add type hints to the main function"
```

## Quick Examples

```bash
# Refactor code
oy "convert this callback to async/await"

# Work with a specific directory
OY_ROOT=./my-project oy "fix the failing tests"

# Use a specific model
OY_MODEL=gpt-4o oy "explain the auth flow"

# Non-interactive (pipelines, CI)
echo "update the changelog" | OY_NON_INTERACTIVE=1 oy

# Fetch and analyze web content
oy "summarize this API doc"  # agent uses httpx tool
```

## Why This Exists

Most AI coding tools are large, complex, or lock you into specific providers. `oy` is ~1000 lines of straightforward Python with a minimal tool surface. Easy to audit, easy to understand, easy to run safely.

**Design goals:**
- Small, auditable codebase
- Minimal tool surface (7 tools)
- Works with OpenAI or AWS Bedrock
- Fresh session each run (no hidden state)
- Interactive checkpoints when you need them

## Tool Reference

| Tool | Purpose | When to Use |
|------|---------|-------------|
| `read` | Read files or directories | Always read before editing |
| `apply` | Modify files (replace/write/move/delete) | All file modifications |
| `list` | List directory contents | Exploring structure |
| `glob` | Find files by pattern | Know the pattern, not the path |
| `grep` | Search file contents | Find code by text/regex |
| `bash` | Run shell commands | Builds, tests, git, package managers |
| `httpx` | HTTP requests | Fetching pages, calling APIs |
| `ask` | Ask user questions | Interactive checkpoints (TTY only) |

### Tool Details

**read**: Primary inspection tool. Includes line numbers. Use `offset` and `limit` for large files. For directories, behaves like `list`.

**apply**: All file modifications go through this tool. Operations:
- `replace`: Exact string match replacement (read the file first!)
- `write`: Create new files (`overwrite=true` to modify existing)
- `move`: Rename files
- `delete`: Remove files

**grep**: Searches with ripgrep (preferred) or standard grep. Returns matching lines with file paths and line numbers. Use `file_glob` to filter by extension.

**httpx**: HTTP client with smart defaults:
- `preset="page"`: Fetch HTML, auto-convert to markdown
- `preset="json"`: API expecting JSON response
- `preset="post_json"`: POST with JSON body
- `json_path`: Extract nested fields (e.g., `data.items.0.id`)
- Sensitive headers (Authorization, etc.) are redacted in output

**bash**: For shell commands only. Not for reading files (use `read`) or searching (use `grep`). Output clips at ~16k chars, preserving both head and tail.

**ask**: Interactive checkpoint tool. Use for plan approvals, ambiguous decisions, and commit offers. Only available in interactive mode (stdin is a TTY and `OY_NON_INTERACTIVE` is not set).

## Agent Behavior

The system prompt guides the agent with specific behaviors:

**Core workflow:**
1. Inspect before changing (read files first)
2. Use the narrowest tool that fits (grep → read → apply)
3. Batch related operations (single `apply` for related edits)
4. Fresh session each run (no hidden state or memory)

**Tool output truncation:**
- Most tools: ~16k chars max
- `bash`: preserves both head AND tail when clipped
- `httpx`: ~20k chars
- When clipped: agent narrows queries (read with offsets, specific grep) instead of guessing

**Interactive mode (`ask` enabled):**
- Offers checkpoints for plans, risky changes, and multi-batch work
- Asks to commit after making changes
- Never asks after trivial steps—batches work meaningfully

**Non-interactive mode (`OY_NON_INTERACTIVE=1`):**
- No checkpoints; runs to completion or blocking error
- Recovers from failures automatically (retries, alternate tools, simpler approaches)
- Provides clear status when blocked (what was tried, what remains, next step)

**Safety:**
- Asks before deleting files (interactive mode)
- Offers to commit state before risky changes
- Prefers safe, boring solutions over clever ones

## Configuration

`oy` keeps the run command short and expects most configuration from environment variables:

```bash
export OY_MODEL=anthropic.claude-3-5-sonnet-20241022-v2:0
export OY_NON_INTERACTIVE=1
export OY_SYSTEM_FILE=./ops/system.txt
export OY_ROOT=/path/to/workspace
oy "fix the failing test"
```

| Variable | Purpose |
|----------|---------|
| `OY_MODEL` | Override the default model for this session |
| `OY_NON_INTERACTIVE` | Set to `1` to disable `ask` and run without checkpoints |
| `OY_SYSTEM_FILE` | Append extra system instructions from a file |
| `OY_ROOT` | Run against a different workspace directory |
| `OY_CONFIG` | Override config file path (default: `~/.config/oy/config.json`) |

**Config file** (`~/.config/oy/config.json`):
```json
{"model": "moonshotai.kimi-k2.5"}
```

Use `oy models` to interactively select a default model.

## Requirements

- Python 3.14+
- `bash`
- (Optional) `rg` (ripgrep) for faster search, falls back to `grep`
- OpenAI API key **OR** AWS CLI configured for Bedrock

## Installation

```bash
# Preferred
uv tool install oy-cli

# Alternative
pip install oy-cli
```

## Authentication

### OpenAI API

```bash
export OPENAI_API_KEY=sk-...
oy "summarize this project"
```

For OpenAI-compatible endpoints:

```bash
export OPENAI_BASE_URL=https://your-endpoint.example/v1
export OPENAI_API_KEY=...
```

### AWS Bedrock (automatic)

If your AWS CLI is configured, Bedrock auth is automatic. `oy` uses the same profile/session as the `aws` command and can auto-refresh stale SSO sessions:

```bash
# Uses your default AWS profile/region
oy "fix the bug in auth.py"

# Or configure explicitly
export AWS_PROFILE=my-profile
export AWS_REGION=us-west-2
```

## Commands

```bash
oy "prompt"              # Run with a prompt (default subcommand)
oy models                # Interactive model picker
oy models claude         # Filter models by name
oy model                 # Show current default model
oy --help                # Show all commands
oy --version             # Show version
```

## Troubleshooting

**"Missing API credentials"**
- Set `OPENAI_API_KEY` or configure AWS CLI (`aws configure`)
- For Bedrock: ensure your AWS profile has `bedrock:InvokeModel` permission

**"stdin is not a TTY"**
- You're piping input, so `ask` is disabled
- Set `OY_NON_INTERACTIVE=1` to make this explicit

**"AWS SSO session is stale"**
- Run `aws sso login --use-device-code --no-browser` manually
- Or run `oy` in a TTY and it will prompt for SSO refresh

**"command timed out"**
- `bash` has a `timeout_seconds` parameter (default: 120)
- For long-running commands, the agent can increase this

**"replace target not found"**
- The `apply` replace operation requires exact string match
- Read the file first to get the exact text
- Check for whitespace differences (indentation, line endings)

**Output truncated unexpectedly**
- Tool output is clipped at ~16k chars to preserve context
- The agent should automatically narrow queries when this happens
- Guide it explicitly if needed: "read lines 100-200 of that file"

## Security Considerations

`oy` can run shell commands and modify files with your permissions.

**Recommended security posture:**

- Run in a container or sandboxed environment
- Mount only the directories you want modified
- Avoid broad host access
- Don't expose secrets you don't want shell commands to access
- Review changes before committing (agent offers checkpoints in interactive mode)

**What `oy` does automatically:**

- Redacts sensitive headers (Authorization, Cookie, etc.) in `httpx` output
- Respects workspace boundaries (can't escape `OY_ROOT`)
- Refreshes Bedrock tokens securely via AWS CLI

## Contributing

Development and release notes live in `CONTRIBUTING.md`.

## License

Licensed under the Apache License 2.0. See `LICENSE`.
