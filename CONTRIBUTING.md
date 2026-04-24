# Contributing to cog-sandbox-mcp

Thanks for your interest. This is the CogOS filesystem-coding-agent sandbox exposed via the Model Context Protocol. It bridges MCP clients (LM Studio, Claude Code, etc.) to the CogOS kernel.

## Development setup

```sh
git clone https://github.com/cogos-dev/cog-sandbox-mcp.git
cd cog-sandbox-mcp
uv sync --extra dev --extra evals
```

Requirements: Python 3.10+, `uv` for dependency management.

## Running tests

```sh
uv run pytest tests/ -v
```

The test suite covers:

- Base sandbox tools (file read/write, grep, tree)
- The `cogos_*` bridge tools (status, emit, events_read, resolve, session, handoff)
- Registration visibility (bridge tools appear only when `COG_OS_BASE_URL` is set)
- Threaded mock HTTP kernels for round-trip integration

## Smoke-testing against a real kernel

If you have a CogOS kernel running locally:

```sh
COG_OS_BASE_URL=http://localhost:5100 uv run python scripts/smoke_bridge.py
```

This spawns the MCP subprocess the way LM Studio does, issues `tools/list` + a handful of bridge calls, and verifies the bridge channel end-to-end.

## Project layout

- `src/cog_sandbox_mcp/` — main package
  - `tools/` — tool implementations (base + `cogos_bridge.py`)
  - `__main__.py` — MCP server entry point
- `tests/` — pytest suite
- `scripts/` — `smoke_bridge.py` and evaluation harnesses
- `evals/` — behavioral evaluation fixtures
- `docs/` — protocol docs (HANDOFF_PROTOCOL.md, etc.)

## Submitting changes

1. Fork the repo and create a branch from `main`
2. Make your changes
3. Run the test suite; add tests for new tools
4. For new `cogos_*` bridge tools: match the existing contract — never raise into the agent's loop, always return `{"success": bool, ...}`
5. Update `CHANGELOG.md` under the Unreleased section
6. Bump version in `pyproject.toml` if the change is user-visible
7. Open a pull request using the org PR template

## Bridge-tool design

New `cogos_*` tools should:

- Register conditionally on `COG_OS_BASE_URL` being set (use the pattern from `cogos_status`)
- Never raise unhandled exceptions — always return a structured `{"success": False, "error": ...}` on failure
- Mirror the kernel's response shape where possible (pass through verbatim)
- Have a dedicated test for registration visibility (on + off)
- Have a dedicated test for unreachable-host behavior (structured error, not raise)

## Reporting issues

Use the org-level [Bug Report](https://github.com/cogos-dev/cog-sandbox-mcp/issues/new?template=bug.yml) or [Feature Request](https://github.com/cogos-dev/cog-sandbox-mcp/issues/new?template=feature.yml) forms.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
