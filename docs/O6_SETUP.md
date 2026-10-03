# Running the full OpenClaw on a Radxa Orion O6

The goal is the same bots and features as the GB10, on an Arm CPU-only board:

- several Telegram bots;
- memory and category RAG, including image text and scanned PDFs;
- voice;
- check-ins such as the work log or the night ritual;
- the English coach and pronunciation.

The O6 (CIX P1: 8 Cortex-A720 + 4 Cortex-A520, 30 GiB usable RAM) has no
GPU. The model runs as **llama.cpp on the host**, and everything is slower.
Off-peak preparation (04:00) keeps scheduled messages on time.

This is how the O6 used for validation is set up (October 2026): two bots,
a work log and a personal-growth coach. `compose.arm-cpu-only.yaml` (one
bot, host networking) still works for a quick demo.

## What runs where

| Where | What |
|---|---|
| Host, systemd **user** services | `openclaw-llama-main` (text, :8080), `openclaw-llama-vision` (images, :8081) |
| Host, system services | Ollama (`nomic-embed-text`), Qdrant |
| `compose.o6.yaml` (shared) | `openclaw-whisper`, `openclaw-browser-scraper`, `openclaw-tts` |
| `compose.persona.<bot>.yaml` (per bot) | Telegram gateway, cron, memory watcher, OpenClaw Gateway |
| Host, systemd user timers | watchdog, nightly backup, weekly e2e |

Bots reach the host through `host.docker.internal`, exactly as on the GB10.
Each bot's Gateway keeps its own published port, so bots never clash.

## 1. The model (validated)

Two candidates were run through `scripts/o6_validate.py` and a 2.8k-token
prompt benchmark:

| | **B: ERNIE 4.5 21B-A3B-PT (Q4_0) + Qwen2-VL-7B (Q4_K_M)** -- in use | A: Qwen3-VL-30B-A3B-Instruct (Q4_0), one model |
|---|---|---|
| `o6_validate.py` | **7/7** | 6/7 -- the ~6k-token prompt timed out after 15 min |
| Generation | 16.5-18 tokens/s | 15.7-17.4 tokens/s |
| Prompt reading | **40-47 tokens/s** | 28-30 tokens/s |
| Image text (EN / 繁 / 简) | exact, 29 s | exact, 43 s |
| RAM left | ~13 GiB | ~8 GiB |

Both are mixture-of-experts with ~3B active parameters, so generation is
fast. On the GB10, Qwen3.6-27B (dense) generates at ~8 tokens/s. **Reading
the prompt is the O6's bottleneck**: about 40 tokens/s against the GB10's
1,500+. Without a limit, a `/rag` question over 8 passages reads ~3,800
tokens and takes ~100 s. `.env.o6.example` therefore caps the retrieved text
(`OPENCLAW_RAG_CONTEXT_TOKENS=1500`, `OPENCLAW_RAG_PASSAGE_TOKENS=400`), which
brings it to 20-30 s. `scripts/rag_budget_eval.py` checked that budget on real
documents: it kept as many answers as no limit.

Use the **non-Thinking** ERNIE (`-PT`). The Thinking variant writes its
reasoning first.

llama.cpp needs a build recent enough for `--mmproj` and
`response_format: json_schema` (the O6 runs a November 2025 build). The two
servers are systemd user services from `deploy/o6/`:

```bash
cp deploy/o6/openclaw-llama-*.service ~/.config/systemd/user/
cp deploy/o6/o6-models.env.example ~/.config/openclaw-o6-models.env   # model paths, threads
systemctl --user daemon-reload
systemctl --user enable --now openclaw-llama-main openclaw-llama-vision
loginctl enable-linger $USER    # start at boot without a login
```

The services:

- bind to the Docker bridge (`172.17.0.1`), so only containers on this host can reach them;
- use `-c 16384` for text and `-c 8192` for vision;
- use `-t 8` threads;
- cap llama.cpp's prompt cache at 2 GiB (text) and 512 MiB (vision), not
  its 8 GiB default (`MAIN_CACHE_RAM`, `VISION_CACHE_RAM`);
- set the slot similarity to 0.01 (`SLOT_SIMILARITY`). At the default 0.10
  a long `/rag` prompt, which shares only the ~150-token persona with
  earlier requests, went to an empty slot and reused nothing.

`scripts/perf_probe.py` measures what this buys. Run it in a bot container;
it reports prompt size, tokens reused from the cache, and the time spent
reading the prompt and writing the answer. The persona is short, so caching
saves 1-4 s per request. The long `/rag` prompts are what take the time.

Set `OPENCLAW_MODEL_CONTEXT_TOKENS` / `OPENCLAW_VLM_CONTEXT_TOKENS` to the
same values; prompts are trimmed to fit.

To try candidate A, set `MAIN_MODEL` / `MAIN_MMPROJ` in the env file. Stop
`openclaw-llama-vision`, and point the bots' `OPENCLAW_VLM_BASE_URL` at
`:8080`.

Threads: 8 versus 6 was measured over a minute of load. 6 threads read
prompts 16% slower and ran only 1-4 °C cooler, so keep 8.

## 2. Embeddings and Qdrant

Ollama with `nomic-embed-text` and Qdrant on 6333, as in
`docs/ERNIE_LLAMA_CPP.md`. Ollama listens on `127.0.0.1` by default, which
containers can't reach. Make it listen on the Docker bridge with a systemd
drop-in, so the stock unit is unchanged:

```ini
# /etc/systemd/system/ollama.service.d/openclaw.conf
[Unit]
After=docker.service

[Service]
Environment="OLLAMA_HOST=172.17.0.1:11434"
```

Then `sudo systemctl daemon-reload && sudo systemctl restart ollama`. The
`ollama` CLI on the host then needs `OLLAMA_HOST=172.17.0.1:11434`.

## 3. Shared services

```bash
docker compose -f compose.o6.yaml up -d --build
```

- Whisper: `base`, int8, CPU.
- The web scraper.
- Pronunciation: Kokoro on CPU, ~3.5 s for a first clip, then cached.

The first TTS start downloads the voice model.

## 4. Bots

For each bot:

1. `cp compose.persona.o6.example.yaml compose.persona.<bot>.yaml`, replace
   `bot-a` / `bot_a` with the bot's name and give its Gateway its own host
   port (18791, 18792, ...).
2. Create `profiles/<bot>/.env`:
   - the usual settings (persona, chat IDs, collections, `OPENCLAW_CRON_TIMEZONE`);
   - the O6 settings from `.env.o6.example`;
   - a fresh `OPENCLAW_GATEWAY_TOKEN`;
   - the bot token. Edit it in yourself; don't paste it into a chat or a command line.
3. Features per bot, as on the GB10:
   - check-ins: `docs/CHECKINS.md`, e.g. `worklog.toml` into `profiles/<bot>/workspace/checkins/`;
   - `/cron` jobs;
   - the English coach, the dictionary and pronunciation, if wanted.
4. `bin/openclawctl --profile <bot> start`. The first start builds the
   memory-watcher image, which includes pypdfium2 for scanned PDFs.

For the non-bot commands, tell `openclawctl` where the shared services are
and that the model runs outside Docker:

```bash
export OPENCLAWCTL_COMPOSE="docker compose -f compose.o6.yaml"
export OPENCLAWCTL_CORE_SERVICES="openclaw-whisper openclaw-browser-scraper openclaw-tts"
export OPENCLAWCTL_MODEL_SERVICES=""
```

## 5. Validate

```bash
docker exec -i openclaw-telegram-<bot> python3 - < scripts/o6_validate.py
docker exec -i openclaw-telegram-<bot> python3 - < scripts/e2e_run.py
```

The O6 bots pass both: `o6_validate` 7/7, e2e 10/10. Before the bots exist,
`o6_validate.py` can run in a throwaway `python:3.12-slim` container on the
compose network with an env file and `--add-host
host.docker.internal:host-gateway`.

## 6. Operations

The host scripts work unchanged (`docs/RUNTIME_LIFECYCLE.md`). On the O6
they run as **systemd user timers** rather than cron. Its cron spool wasn't
usable, and timers need no root. Each one is a oneshot `.service` running
the script from the repo, plus a `.timer`:

| Timer | `OnCalendar` | Runs |
|---|---|---|
| `openclaw-watchdog` | `*:0/5` | `scripts/openclaw_watchdog.py` |
| `openclaw-backup` | `*-*-* 03:15:00 Europe/London` | `scripts/openclaw_maintenance.py backup` |
| `openclaw-e2e` | `Mon *-*-* 03:40:00 Europe/London` | `scripts/openclaw_maintenance.py e2e --container openclaw-telegram-<bot>` |

Make sure the repo's `.cache/` belongs to your user. The TTS container
creates `.cache/tts` as root, and the watchdog writes its state next to it.

## 7. Heat and the fan

On this board the `pwm-fan` isn't bound to any thermal trip, so it runs flat
out from boot. Under load, the temperature behaves as follows:

| Situation | Temperature | Fan |
|---|---|---|
| A minute of LLM work, controlled fan | 63-66 °C | middle step |
| 15+ minutes of continuous load | up to 82 °C | full speed (passive throttling starts at 85 °C) |

A small temperature-based controller (a root systemd service on the board,
not part of this repo) keeps it quiet, and drops to full speed if it stops.
On this board the fan steps are PWM 0 / 150 / 200 / 255, so even step 1 is
audible.

| Step | On at | Off below |
|---|---|---|
| 1 | 55 °C | 45 °C (back to off) |
| 2 | 62 °C | 59 °C |
| 3 (full) | 72 °C | 69 °C |

With the fan off, the idle board settles around 50 °C. The wide 45-55 °C
band keeps the fan off at idle. With a narrow band (on at 45 °C) the fan
started and stopped every two minutes. A bot reply that crosses 55 °C runs
the fan until the board is back under 45 °C. Long off-peak runs at 04:00 can
spin the fan up for several minutes.

## What to expect

- Replies take longer than on the GB10:
  - short chat is as fast;
  - `/rag` takes 20-30 s with the budget above; other long prompts take 1-2 minutes;
  - English-coach feedback can take minutes.
- Image text reading takes 30-60 s per image, in the background. The
  【圖片文字】 card arrives when it's done.
- Scheduled messages (07:00-07:30 reports, the English task) are prepared at
  04:00 and delivered on time. Week-to-date reports such as the work log's
  are made at send time.
