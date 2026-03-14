# oy-cli

**Tiny AI coding assistant for your shell.** Reads files, runs commands, makes edits - nothing fancy.

```bash
uv tool install oy-cli
oy "add type hints to the main function"
```

## Why This Exists

Most AI coding tools are large, complex, or lock you into specific providers. `oy` is ~1000 lines of straightforward Python with a minimal tool surface. Easy to audit, easy to understand, easy to run safely.

## Tools

File operations: `read` `apply` `list` `glob` `grep`  
Shell: `bash` (for builds, tests, git)  
Network: `httpx` (for web pages and APIs; supports smart defaults, JSON bodies, filtering, and truncation)  
Collaboration: `ask` (interactive runs only, for approvals, checkpoints, and feedback)

## Agent Notes

- The built-in system prompt tells the agent to inspect before editing and prefer the narrowest tool that fits.
- Tool output is clipped to keep long tasks inside model context. Most tool results are capped at about 16k chars, `bash` keeps both the start and end when clipped, and `httpx` defaults to about 20k chars after response formatting.
- Each `oy` run is a fresh session. It does not inject workspace history into later runs, so the agent should rely on the current prompt plus current tool results.
- When output is clipped, the intended recovery path is to narrow the query: use `read` with offsets, `grep`, `glob`, `list`, or another focused `httpx` call instead of guessing.
- By default, interactive runs keep `ask` enabled and the system prompt encourages using it for plans, reviews, ambiguous product decisions, and meaningful checkpoints.
- `OY_NON_INTERACTIVE=1` removes the `ask` tool from the run and swaps in a prompt that tells the agent to keep going without interruptions and recover from faults when it can.
- For broad or risky changes, the prompt nudges the agent to ask whether it should summarise and commit the current state first so undo is easy.
- Before ending with a normal completion summary after making changes, the prompt nudges the agent to ask whether it should summarise and commit the completed work.
- `apply` batches structured file operations for exact replacements, writes, moves, and deletes.
- `httpx` can make authenticated HTTP requests, use `preset=json` or `preset=post_json` for common API calls, extract a `json_path`, and limit output with `max_chars`.

## Configuration

`oy` keeps the run command short and expects most per-run configuration to come from env vars:

```bash
export OY_MODEL=zai.glm-5
export OY_NON_INTERACTIVE=1
export OY_SYSTEM_FILE=./ops/system.txt
export OY_ROOT=/path/to/workspace
oy "fix the failing test"
```

- `OY_MODEL`: override the configured default model for this shell/session
- `OY_NON_INTERACTIVE=1`: disable `ask` and run straight through without checkpoints
- `OY_SYSTEM_FILE`: append extra system instructions from a file
- `OY_ROOT`: run against a workspace without changing shell directories
- `OY_CONFIG`: override the config file path used for persisted settings like the default model

## Requirements

- Python 3.14+
- `bash`
- (Optional) `rg` (ripgrep) or `grep` for search support
- OpenAI API key OR AWS CLI auth (for Bedrock)

## Installation

Preferred:

```bash
uv tool install oy-cli
```

Alternative:

```bash
pip install oy-cli
```

This installs the `oy` command.

## AWS Bedrock (automatic)

If your AWS CLI can auth, Bedrock is auto-configured. `oy` uses the same
profile/session the `aws` command would use, and can auto-run
`aws sso login --use-device-code --no-browser` when an SSO session is stale.

```bash
oy models
oy models moonshot
oy model
```

## OpenAI API

```bash
export OPENAI_API_KEY=...
oy "summarize this project"
```

For OpenAI-compatible endpoints:

```bash
export OPENAI_BASE_URL=https://your-endpoint.example/v1
export OPENAI_API_KEY=...
```

## Commands

```bash
oy "prompt"              # Run with a prompt
oy models                # Interactive model picker
oy models <query>        # Start picker with a filter
oy model                 # Show current model
```

## Security

`oy` can run shell commands and modify files. Treat it like an automation tool with real permissions.

Recommended posture:

- run it in a container or similarly constrained environment
- mount only the project/dirs you want it to touch
- avoid broad host access
- do not expose secrets you do not want shell commands to inherit

## Contributing

Development and release notes live in `CONTRIBUTING.md`.

## License

Licensed under the Apache License 2.0. See `LICENSE`.
