# oy-cli

Tiny local coding CLI for simple shell interactive code editing and auditing.

`oy` is intentionally small:

- one-shot Typer CLI, not a full TUI
- decent progress output, not advanced orchestration
- one main code file as the implementation target
- intentionally small and auditable

The point is to keep the tool surface boring, explicit, and easy to audit (most existing tools and agents are large/complex or locked to specific providers).

## Design Notes

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

## Quick Start using AWS Bedrock (automatic)

If you have AWS credentials configured (via `AWS_PROFILE`, environment variables, or `~/.aws/credentials`), Bedrock is automatically configured:

```bash
oy models
oy model moonshotai.kimi-k2.5
oy "inspect this repository and suggest the smallest safe fix"
```

To manually export Bedrock tokens (for use in other tools or scripts):

```bash
eval "$(oy bedrock-token)"
```

`oy models` uses the OpenAI SDK with `client.models.list()`. `oy model <id>` saves your default model.

With OpenAI-compatible manual creds:

```bash
export OPENAI_BASE_URL=https://your-endpoint.example/v1
export OPENAI_API_KEY=...
oy "summarize this project and list the next changes"
```

With OpenAI's default API endpoint:

```bash
export OPENAI_API_KEY=...
oy "summarize this project and list the next changes"
```

## Runtime Behavior

- simple CLI flow, no REPL and no TUI
- simple commands: `oy "..."`, `oy bedrock-token`, `oy models`, `oy model <id>`
- model chooses from a small set of local tools
- file operations are scoped to the working directory
- required tools are checked up front
- indeterminate progress is shown while waiting on API calls
- default model/tool budgets are intentionally high for longer build-style runs

## Model Selection

```bash
oy model
oy models
oy model moonshotai.kimi-k2.5
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
