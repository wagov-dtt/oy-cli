# oy-cli

Tiny local coding CLI for simple shell interactive code editing and auditing.

`oy` is intentionally small:

- one-shot Typer CLI, not a full TUI
- decent progress output, not advanced orchestration
- one main code file as the implementation target
- overall code size target under 500 lines

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
- `bash`: run shell commands with `subprocess` via `bash -lc`
- `read`: read files with `pathlib`
- `grep`: search text with `subprocess` + `ripgrep`
- `glob`: find files and directories with `pathlib.glob`
- `webfetch`: fetch web content with `httpx`

That is enough for useful local coding work and security audits.

## Requirements

- Python 3.14+
- `bash`
- `patch`
- `OPENAI_API_KEY` and `OPENAI_BASE_URL` in the environment

`ripgrep` is included as a PyPI dependency. `oy bedrock-token` can also use the AWS CLI as a fallback to recover authentication if token generation fails. If `bash`, `rg`, or `patch` are missing, `oy` exits with install guidance instead of silently falling back.

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

## Quick Start using AWS Bedrock

```bash
eval "$(oy bedrock-token)"
oy select-model
oy "inspect this repository and suggest the smallest safe fix"
```

`oy list-models` and `oy select-model` use the OpenAI SDK with `client.models.list()` and can save the chosen model in local settings.

With manual creds:

```bash
export OPENAI_BASE_URL=https://your-endpoint.example/v1
export OPENAI_API_KEY=...
oy "summarize this project and list the next changes"
```

## Runtime Behavior

- simple CLI flow, no REPL and no TUI
- Typer-based CLI with a small `settings` surface
- model chooses from a small set of local tools
- file operations are scoped to the working directory
- required tools are checked up front
- indeterminate progress is shown while waiting on API calls
- default model/tool budgets are intentionally high for longer build-style runs

## Settings

```bash
oy settings show
oy settings set model moonshotai.kimi-k2.5
oy settings get model
```

## Security

`oy` can run shell commands and modify files. Treat it like an automation tool with real permissions.

Recommended posture:

- run it in a container or similarly constrained environment
- mount only the project/dirs you want it to touch
- avoid broad host access
- do not expose secrets you do not want shell commands to inherit

## Development

`mise` manages local tooling and `uv` handles Python environments and packaging.

```bash
mise install
uv sync
mise run fmt
mise run lint
mise run check
uv run oy --help
mise run build
```

## Packaging

- PyPI package: `oy-cli`
- installed command: `oy`
- intended end-user install path: `uv tool install oy-cli`

## License

Licensed under the Apache License 2.0. See `LICENSE`.
