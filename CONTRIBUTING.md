# Contributing

Thanks for contributing to `oy-cli`.

## Development Setup

`mise` manages local tooling and `uv` handles Python environments and packaging.

```bash
mise install
uv sync
```

## Common Commands

```bash
mise run fmt
mise run lint
mise run check
uv run oy --help
mise run build
```

## Project Notes

- PyPI package: `oy-cli`
- installed command: `oy`
- intended end-user install path: `uv tool install oy-cli`
- current design goal: keep the implementation small and easy to audit
- prefer simple, direct changes over abstraction-heavy rewrites

## Release Hygiene

- keep `README.md` user-focused
- keep contributor workflow here in `CONTRIBUTING.md`
- make sure `mise run fmt`, `mise run lint`, `mise run check`, and `mise run build` pass before shipping
