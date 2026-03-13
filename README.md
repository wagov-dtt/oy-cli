# oy-cli

**Tiny AI coding assistant for your shell.** Reads files, runs commands, makes edits - nothing fancy.

```bash
uv tool install oy-cli
oy "add type hints to the main function"
```

## Why This Exists

Most AI coding tools are large, complex, or lock you into specific providers. `oy` is ~1000 lines of straightforward Python with a minimal tool surface. Easy to audit, easy to understand, easy to run safely.

## Tools

File operations: `read` `write` `edit` `patch` `list` `glob` `grep`  
Shell: `bash` (for builds, tests, git)  
Network: `webfetch` (for docs, API lookups)

## Requirements

- Python 3.14+
- `bash`, `patch`
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
oy model moonshotai.kimi-k2.5
```

To export Bedrock tokens for other tools:

```bash
eval "$(oy bedrock-token)"
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

`oy` tries the Responses API first and falls back to Chat Completions when the
provider rejects it before any tool runs. Override with
`OY_RESPONSES=auto|always|never`.

## Commands

```bash
oy "prompt"              # Run with a prompt
oy models                # List available models
oy model <id>            # Set default model
oy model                 # Show current model
oy bedrock-token         # Export Bedrock credentials
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
