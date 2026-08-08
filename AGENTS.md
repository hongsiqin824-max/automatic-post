# Repository Guidelines

## Project Structure & Module Organization

The Flask application lives in `app/`. Persistence is split between `app/db.py` (schema and connections) and `app/repository.py` (queries and workflow updates). External clients and processing logic belong in `app/services/`. Server-rendered pages are in `templates/`, browser assets in `static/`, and tests in `tests/`. Runtime SQLite files stay under `instance/` and must not be committed.

## Build, Test, and Development Commands

Run commands from the repository root, whose path contains a space:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py web   # local UI on port 8890
.venv/bin/python run.py once  # one ingestion and quality pass
.venv/bin/pytest -q           # complete test suite
```

Copy `.env.example` to `.env` for local credentials. The app must still start when optional integrations are unconfigured.

## Coding Style & Naming Conventions

Use four-space Python indentation, type hints for public functions, `snake_case` for Python and JSON fields, and uppercase workflow constants such as `NEEDS_REVIEW`. Keep routes thin; put business decisions in services and all SQL in the repository layer. JavaScript uses two-space indentation and `data-*` hooks rather than inline handlers. Add comments only for non-obvious behavior.

## Testing Guidelines

Use `pytest` and Flask's test client. Name files `test_*.py` and tests `test_<behavior>`. Mock all remote HTTP and LLM calls. Cover idempotent ingestion, state transitions, source-tab constraints, review actions, and failure handling. Never publish real content from tests.

## Commit & Pull Request Guidelines

Use concise imperative commits such as `Add review queue transitions`. Pull requests should explain behavior changes, list verification commands, and include screenshots for UI updates. Call out schema or environment-variable changes explicitly.

## Security & Configuration

Never commit API keys, cookies, `.env`, SQLite databases, or raw responses containing secrets. Bind the unauthenticated v1 server to `127.0.0.1` only. Treat `READY_TO_PUBLISH` as the terminal state until the publisher is deliberately enabled and verified.
