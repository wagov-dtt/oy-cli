# oy-cli

Tiny local coding CLI for simple shell interactive code editing and auditing.

## Quick Start

```bash
uv tool install oy-cli
oy "summarize this project and suggest next changes"
```

The point is to keep the tool surface boring, explicit, and easy to audit (most existing tools and agents are large/complex or locked to specific providers).

## Design

- follow OWASP-minded secure defaults
- prefer the grugbrain.dev philosophy: simple, direct, low-magic solutions
- run inside a locked-down container with limited filesystem, process, and network permissions if you want to use it safely
- de-scope advanced surfaces like todos, skills, and MCPs (bash is fine, most tools have good CLI interfaces)

## Tool Surface

Initial tools:

- `write`: create new files with `pathlib`
- `edit`: modify existing files with `pathlib` read/write
- `patch`: apply unified diffs with `patch` (via subprocess)
- `list`: list directory contents with `pathlib`
- `read`: read files with `pathlib`
- `grep`: search text with `subprocess` + `ripgrep`
- `glob`: find files and directories with `pathlib.glob`
- `bash`: run shell commands with `subprocess` via `bash -lc` as a last resort
- `webfetch`: fetch web content with `httpx`

That is enough for useful local coding work and security audits.

## Known Issues

Some LLMs occasionally emit duplicated tool call arguments. `oy` includes a workaround that detects and recovers from this by hunting for valid JSON around the midpoint of the malformed response.

## Requirements

- Python 3.14+
- `bash`
- `patch`
- `OPENAI_API_KEY` in the environment, OR AWS credentials for automatic Bedrock setup

`ripgrep` is included as a PyPI dependency. If AWS credentials are available (via `AWS_PROFILE`, `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, or `~/.aws/credentials`), `oy` will automatically generate Bedrock tokens. If `bash`, `rg`, or `patch` are missing, `oy` exits with install guidance instead of silently falling back.

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

If you have AWS credentials configured, Bedrock is auto-configured:

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
