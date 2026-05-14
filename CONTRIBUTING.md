# Contributing to havn

Thanks for your interest in contributing to havn! This document covers everything you need to get started.

## Development Setup

```bash
git clone https://github.com/chraltro/havn.git
cd havn
pip install -e ".[dev]"
cd frontend && npm install && npm run build && cd ..
```

Verify everything works:

```bash
pytest tests/
```

## Project Structure

```
src/havn/           Python package (CLI + engine + server)
frontend/         React + Vite web UI
tests/            pytest test suite
```

See [CLAUDE.md](CLAUDE.md) for a detailed architecture reference.

## Making Changes

### Backend (Python)

1. Source is in `src/havn/`
2. CLI commands live in `src/havn/cli/` (a package, split into modules)
3. Engine logic is in `src/havn/engine/` -- `engine/transform/` is the core SQL DAG engine
4. API endpoints are in `src/havn/server/routes/` (21 modules) wired up in `server/app.py`
5. Run `pytest tests/` after changes

### Frontend (React)

1. Source is in `frontend/src/`
2. React 19 + Vite, no TypeScript
3. Dev server: `cd frontend && npm run dev` (port 5173, proxies to 3000)
4. Build: `cd frontend && npm run build`

## Testing

```bash
pytest tests/              # all tests
pytest tests/ -x           # stop on first failure
pytest tests/ -v           # verbose output
pytest tests/test_api.py   # specific file
```

Tests use temporary in-memory DuckDB databases. No external services needed.

## Code Style

- Python 3.10+, type hints throughout
- `from __future__ import annotations` in all modules
- Imports: stdlib, then third-party, then local
- Rich library for terminal output
- Lazy imports in CLI commands (faster startup)

## SQL Style

SQL files are linted with SQLFluff (DuckDB dialect):
- Keywords: UPPER (`SELECT`, `FROM`, `WHERE`)
- Identifiers: lower (`customer_id`, `order_count`)

```bash
havn lint          # check
havn lint --fix    # auto-fix
```

## Pull Requests

1. Fork the repo and create a branch from `main`
2. Make your changes
3. Add or update tests as needed
4. Run `pytest tests/` and make sure everything passes
5. Open a PR with a clear description of what changed and why

Keep PRs focused — one feature or fix per PR.

## Reporting Issues

Open an issue on GitHub with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Python version and OS

## Adding a New CLI Command

1. Add `@app.command()` function in the appropriate module under `src/havn/cli/`
2. Import engine modules lazily (inside the function body) to keep `havn --help` fast
3. Use `_resolve_project()` for project dir resolution
4. Add a corresponding API endpoint in the matching `src/havn/server/routes/` module if needed
5. Add tests

## Adding a New Connector

1. Create a new file in `src/havn/connectors/`
2. Define a connector class following the existing pattern
3. Register it in `src/havn/connectors/__init__.py`
4. Add tests

## License

havn is licensed under the [Business Source License 1.1](LICENSE), which auto-converts to Apache 2.0 four years after each release.

By contributing, you agree that your contributions will be licensed under the same terms. In addition, we are in the process of rolling out a Contributor License Agreement (CLA): once it is live, contributors will be asked to sign the CLA so that contributed code can be included in both the BSL core and any future commercial distributions (e.g., `havn cloud`). Until the CLA is set up, opening a pull request is taken as agreement that your contribution may be used under these terms.
