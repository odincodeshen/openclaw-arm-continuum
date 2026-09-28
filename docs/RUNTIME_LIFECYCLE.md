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

### One bot at a time (`--profile`)

When several bots run side by side (`docs/PROFILES.md` "Run Several Bots At
Once"), each bot is one `compose.persona.<bot>.yaml` + `profiles/<bot>/.env`.
`--profile` acts on just that bot's four containers (Telegram, memory
watcher, cron, Gateway), always with `--no-deps`, so the shared vLLM /
Whisper / scraper services are never touched:

```bash
bin/openclawctl profiles                        # every bot and its containers
bin/openclawctl --profile bot_a status
bin/openclawctl --profile bot_a start           # also applies compose/.env changes
bin/openclawctl --profile bot_a restart         # reload the code
bin/openclawctl --profile bot_a stop
bin/openclawctl --profile bot_a logs cron       # telegram|cron|watcher|gateway, or all four
bin/openclawctl --profile all restart           # every bot that is running now
```

`start` first creates the bot's Gateway `state` directory as you, so Docker
doesn't create it as root (which silently stops the Gateway's
`admin-http-rpc` plugin from loading). `--profile all` supports
`status|stop|restart` and skips bots that are stopped; start a bot by name.
`OPENCLAWCTL_ROOT` overrides the repo root and `OPENCLAWCTL_LOG_LINES` the
number of log lines (default 50).

## Alerts

Problems that would otherwise only reach a log are sent to
`OPENCLAW_ALERT_CHAT_IDS` on Telegram, by the bot that hit them and labelled
with its `OPENCLAW_RUNTIME_LABEL`:

| Alert | Raised by | Recovers when |
| --- | --- | --- |
| A `/cron` job failed | cron worker | the job next succeeds |
| The Gateway unreachable for `OPENCLAW_ALERT_GATEWAY_OUTAGE_MINUTES` (default 10) | cron worker | it answers again |
| An off-peak preparation failed (cron job or English bot) | cron worker / Telegram gateway | the next preparation succeeds |
| The English bot's scheduler errored | Telegram gateway | the next push goes out |
| The memory watcher can't scan, or can't index files | memory watcher | a clean scan |
| The cron worker's loop errored | cron worker | -- |

The same problem alerts at most once per `OPENCLAW_ALERT_COOLDOWN_MINUTES`
(default 360); when it clears, one "recovered" message follows. Restarts on
their own never alert. With no chat IDs set, alerts are off.

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
