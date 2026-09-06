# CI

`.github/workflows/ci.yml` runs on every push to `main` / `dev` and every PR
targeting them. Three jobs, all on GitHub-hosted runners (no GPU, no network
services):

| Job | What it does |
|---|---|
| `lint` | `ruff check` (config in `pyproject.toml`) |
| `test` | `pytest` — the full unit suite (~210 tests, pure stdlib + `pypdf`) |
| `validate` | `scripts/ci_validate.py` + `docker compose config` on every `compose*.yaml` |

`scripts/ci_validate.py` does the static checks a unit test can't:

- import-smoke `openclaw_telegram_gateway`, `openclaw_memory_watcher`,
  `openclaw_cron_worker`, and every `openclaw_runtime.*` module (catches
  merge breakage and circular imports)
- parse `app/skills.json`
- validate every `app/models*.json` against `ModelSpec` / `ModelRegistry`
  (bad role, missing `base_url`, dangling `fallback`, duplicate id)

The Whisper and browser-scraper services need heavy optional deps
(`faster-whisper`, `playwright`) and are covered by their own Docker image
builds and mocked unit tests, so CI does not import them directly.

## Running the same checks locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install ruff pytest pypdf

ruff check .
pytest
python scripts/ci_validate.py
for f in compose*.yaml; do OPENCLAW_GATEWAY_TOKEN=ci docker compose -f "$f" config -q; done
```

`pyproject.toml` sets `pythonpath = ["app", "."]` for pytest, so
`from tests.support import ...` and `import openclaw_runtime` both work with
no manual `PYTHONPATH`.

## Lint scope

`ruff` is intentionally conservative for now: real-bug rules only
(`E4`/`E7`/`E9`/`F`/`W6` — undefined names, unused imports, bare `except`,
syntax). Import sorting (`I`) and line length (`E501`) are left for a
separate formatting pass so this stays reviewable; tighten
`[tool.ruff.lint] select` in `pyproject.toml` when ready.

## Not covered here (needs GPU + model endpoints)

End-to-end behaviour that depends on vLLM / Qdrant / Ollama — category
isolation against a real index, `Sources:` output, intent-router
classification, the `/review` workflow — is not run in hosted CI. That is
the second CI layer (a self-hosted runner on the GB10 box, or a
`workflow_dispatch` job) and is tracked separately. The manual procedure
lives in `docs/CATEGORY_RAG_MANUAL_TEST.md` and
`docs/DGX_V13_VALIDATION.md`.
