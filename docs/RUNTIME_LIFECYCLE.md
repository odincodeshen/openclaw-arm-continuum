# Runtime lifecycle: core vs. model engine

```text
OpenClaw core  !=  model engine
```

The Telegram gateway, dashboard, cron worker, memory watcher, browser scraper
and Whisper ("**core**") do not need the GPU. The vLLM server ("**model**")
does. On a shared GB10 / DGX workstation you usually want core always-on and
the model engine started only when you need chat or RAG summarization, so the
GPU is easy to reclaim for other projects.

## `bin/openclawctl`

A thin wrapper over `docker compose` that acts on the two groups separately.

```bash
bin/openclawctl status                 # core + model container status
bin/openclawctl start   core           # gateway, cron, memory, scraper, whisper
bin/openclawctl start   model          # just vLLM
bin/openclawctl stop    model          # free the GPU, leave Telegram up
bin/openclawctl restart core
bin/openclawctl start   full           # core + model
bin/openclawctl boot                   # act on OPENCLAW_BOOT_MODE
```

Overrides (env):

| Variable | Default |
| --- | --- |
| `OPENCLAWCTL_CORE_SERVICES` | `openclaw-gateway openclaw-telegram openclaw-cron openclaw-memory-watcher openclaw-browser-scraper openclaw-whisper` |
| `OPENCLAWCTL_MODEL_SERVICES` | `openclaw-vllm` |
| `OPENCLAWCTL_COMPOSE` | `docker compose` |
| `OPENCLAWCTL_DRY_RUN` | unset (`1` prints the compose commands instead of running them) |

It does not restructure `compose.yaml` -- it just targets service names, so
`docker compose` and `openclawctl` stay interchangeable.

## Boot modes

`bin/openclawctl boot` reads `OPENCLAW_BOOT_MODE` (from the process env; wire it
through your `.env` / systemd unit):

| Mode | `boot` starts |
| --- | --- |
| `full` (default) | core + model |
| `core` | core only |
| `manual` | nothing |

Recommended defaults:

- **GB10 / DGX**: `core` -- keep Telegram, memory, cron and doc intake online;
  start the model engine on demand.
- **Arm CPU-only always-on box**: `full`, or `core` when CPU/RAM is shared.

Example systemd unit (host):

```ini
[Unit]
Description=OpenClaw boot
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=/home/you/openclaw-arm-continuum
EnvironmentFile=/home/you/openclaw-arm-continuum/.env
ExecStart=/home/you/openclaw-arm-continuum/bin/openclawctl boot
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

## What Telegram does when the model engine is down

Plain chat and voice replies detect an unreachable / still-loading model and
answer with a clear message instead of a raw stack trace:

> The local model engine is not responding. It may be paused to free the GPU
> (OPENCLAW_BOOT_MODE=core) or still loading a model.
>
> Still works without it: /mem writes, /cat, /doc imports, /cron, /tasks,
> /agents, /help.
> On the host, start it with:  bin/openclawctl start model

Model-independent flows keep working while the engine is stopped: `/mem`
writes, `/cat`, document and Google Doc intake, `/cron` schedule management,
`/tasks`, `/agents`, `/help`, `/new`. Uploaded photos and voice are still saved
and transcribed; only the model-backed analysis/answer is deferred.

## Validation

- `tests/test_openclawctl.py` -- group targeting, boot modes, overrides, usage
  errors (all via `OPENCLAWCTL_DRY_RUN`).
- `tests/test_telegram_gateway.py::ModelPausedMessageTest` -- the paused-engine
  reply path.
- `scripts/ci_validate.py` runs `sh -n bin/*`.
