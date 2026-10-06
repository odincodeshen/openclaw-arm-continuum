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

### Container watchdog

A process can't report its own crash, so `scripts/openclaw_watchdog.py` runs
on the host (standard library only) and compares `docker inspect` of every
`openclaw-*` container with its last run:

| Alert | Recovers when |
| --- | --- |
| A container crashed and Docker restarted it (its `RestartCount` went up), with the exit code or "OOM-killed" | -- |
| A container stuck restarting | it stays up |
| A running container's health check fails | it's healthy again |
| The host rebooted | -- |

A restart or recreate by `openclawctl` / `docker compose` doesn't change
`RestartCount` (a recreated container starts a new baseline), and a stopped
container never alerts, so only unexpected restarts are reported. It sends
through the first `profiles/*/.env` (then `.env`) that sets both
`OPENCLAW_TELEGRAM_BOT_TOKEN` and `OPENCLAW_ALERT_CHAT_IDS`, or the file named
by `OPENCLAW_WATCHDOG_ENV_FILE`; the cooldown is
`OPENCLAW_WATCHDOG_COOLDOWN_MINUTES` (default 360), and its state is kept in
`.cache/openclaw-watchdog.json`. The first run only records a baseline. Run it
from the host's crontab:

```text
*/5 * * * * cd /path/to/openclaw-arm-continuum && /usr/bin/python3 scripts/openclaw_watchdog.py >> .cache/openclaw-watchdog.log 2>&1
```

Once a week -- Monday from 08:00 host time (`OPENCLAW_WATCHDOG_SUMMARY_DAY`,
0 = Monday, and `OPENCLAW_WATCHDOG_SUMMARY_HOUR`) -- the same run also sends
a **weekly health summary**: containers running, any not healthy or failing
their health check, crash restarts and alerts over the week, host reboots,
disk use, and the GPU (from `nvidia-smi`, when there is one) -- plus the
last backup, the last weekly e2e run and how much the daily cleanup freed.

## Backup, cleanup and the weekly e2e run

Everything personal -- memory, knowledge, categories, the English bot's
progress and word lists, the night-ritual journal -- exists only on this
host, so `scripts/openclaw_maintenance.py` (host, standard library only)
keeps copies:

- `backup` (nightly, 03:15): a Qdrant snapshot of every collection (except
  the throwaway `verify_`, `e2e_`, `perf_` and `evalcopy_` ones), and
  `files.tar.gz` with each profile's workspace (minus what can be rebuilt:
  the dictionary, the English bot's weekly audio, voice messages, staging),
  `profiles/*/.env` and the Gateway state, into
  `OPENCLAW_BACKUP_DIR/<YYYY-MM-DD_HHMM>/` (default `~/openclaw-backups`,
  made private -- the `.env` files hold bot tokens). The newest
  `OPENCLAW_BACKUP_KEEP` (default 14) are kept; other folders there are
  never touched. To restore a collection, upload its `.snapshot` through
  Qdrant's snapshot API; unpack the archive over the repo for the files.
- `e2e` (weekly, Monday 03:40): runs `scripts/e2e_run.py` inside a bot's
  Telegram container and keeps the report in `.cache/e2e-latest.md`. It
  uses `--bot` or `OPENCLAW_E2E_BOT`, `--container` or
  `OPENCLAW_E2E_CONTAINER`, or else the first running `openclaw-telegram-*`
  container. It checks the live bot: its token, Gateway and services.
- `verify` (weekly, Monday 01:00): runs `bin/verify full` (`--mode`). That
  is unit tests, the platform check, every feature scenario and the gold
  set, in sandboxes with no personal data (`verify/README.md`).
  - `--dir` / `OPENCLAW_VERIFY_DIR` names the checkout, for a deployed copy
    without tests.
  - The weekly summary shows the result, and a failure alerts.

Both alert on Telegram when they fail. Crontab:

```text
15 3 * * * cd /path/to/repo && /usr/bin/python3 scripts/openclaw_maintenance.py backup >> .cache/openclaw-maintenance.log 2>&1
40 3 * * 1 cd /path/to/repo && /usr/bin/python3 scripts/openclaw_maintenance.py e2e >> .cache/openclaw-maintenance.log 2>&1
0 1 * * 1 cd /path/to/repo && /usr/bin/python3 scripts/openclaw_maintenance.py verify >> .cache/openclaw-maintenance.log 2>&1
```

Where cron isn't available, systemd user timers do the same (no root, with
`loginctl enable-linger`) -- the O6 runs the watchdog, backup, e2e and verify that
way; see `docs/O6_SETUP.md`, "Operations".

Cleanup runs inside each bot's Telegram container (the files belong to
it), daily at `OPENCLAW_HOUSEKEEPING_TIME` (default 03:30; empty = off):
voice messages older than `OPENCLAW_AUDIO_RETENTION_DAYS` (14), sent export
files and never-filed staging uploads older than 7 days, and the English
bot's weekly audio except the newest two weeks. Indexed documents, memory
and the journal are never touched. The pronunciation service drops cached
clips unused for `OPENCLAW_TTS_CACHE_DAYS` (180).

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
