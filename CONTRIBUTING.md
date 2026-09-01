# Contributing

AutomatiQ is in early alpha. Things are rough and changing fast — contributions, bug reports, and ideas are all welcome.

## Setup

```bash
git clone https://github.com/StoneSteel27/AutomatiQ.git
cd AutomatiQ
uv sync
pre-commit install
```

See the [README](README.md) for full install and configuration details.

## Repo layout

- `src/automatiq/mcp/` - The MCP server: FastMCP stdio server, runtime, annotation, logging setup, vision, and status log (each module under 700 lines).
- `src/automatiq/core/` - Recorder engine: config, events, browser and binary managers, telemetry, and the `recorder/` subpackage (`cdp/`, `compile/`, `extension/`).
- `tests/` - Pytest suite (16 modules, 101 tests).
- The legacy CLI, LLM investigator agent, IPython sandbox, and telemetry server backend live on the `legacy/v0.3.x` branch, not in `main`.

## Code style

We use [Ruff](https://docs.astral.sh/ruff/) for linting and formatting. The rules are defined in `pyproject.toml`.

If `pre-commit` passes, you're good. To run manually:

```bash
uv run ruff check src tests
uv run ruff format src tests
```

- Line length: 121
- Python 3.11+ (CI matrix: 3.11/3.12/3.13)
- Double quotes

## Tests

Run the suite with `uv run pytest -q`. Tests follow no-network conventions: fake CDP events feed real handlers, and anything touching the network, a real browser, or a real LLM is monkeypatched (e.g. `automatiq.mcp.runtime.vision_preflight`).

## Pull requests

1. Fork the repo
2. Create a branch
3. Make your changes
4. Ensure pre-commit passes
5. Open a PR

CI runs the same pre-commit checks on every PR — if it's green locally, it'll be green in CI.

## Issues

Found a bug? Have an idea? Open an issue. No template required — just describe what's wrong or what you'd like to see.
