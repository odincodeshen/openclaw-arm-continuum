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

## Integration layer (`integration.yml`)

`.github/workflows/integration.yml` runs `tests/test_scenarios_integration.py`
against a real Qdrant service container, with an in-process fake model /
embedding server (`tests/fake_inference.py`) so there is still no GPU. It
covers the ingest -> Qdrant -> retrieve -> format pipeline: category
isolation, `Sources:` output, knowledge-doc indexing, `/mem` round trip, and
conversational memory. See `docs/TESTING.md`.

The unit `test` job above runs the same file but its Qdrant-backed cases skip
(no service), so a plain `pytest` stays green.

## Not covered in hosted CI (needs GPU + real models)

Model-quality behaviour — intent-router accuracy, the `/review` workflow, a
real VLM reading an image — is the L3 layer: same scenarios, real vLLM /
Ollama on the GB10 self-hosted runner. Tracked in `docs/TESTING.md`; manual
procedures in `docs/CATEGORY_RAG_MANUAL_TEST.md` and
`docs/DGX_V13_VALIDATION.md`.
