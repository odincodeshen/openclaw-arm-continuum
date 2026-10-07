# bin/verify: self-verification without personal data

```bash
bin/verify                # standard: unit tests, platform check, then every scenario
bin/verify quick          # unit tests and ci_validate only (no services needed)
bin/verify platform       # what this machine's services can do, against its profile
bin/verify scenarios      # scenarios only
bin/verify gold           # answer quality on the fixed gold set, against this machine's baseline
bin/verify full           # standard, then gold (the weekly run)
bin/verify gold --accept  # take this run as the new baseline, after an intended change
bin/verify coverage       # bot commands and check-in templates no scenario uses
bin/verify standard --remote o6:openclaw-verify   # copy the files to another machine and run it there
bin/verify scenarios --only checkin_skip_fillin rag_keywords_chinese
bin/verify platform --platform orion-o6   # use a profile instead of detecting one
```

It runs on any machine with Docker and a running bot: a GB10, an Orion O6,
or anything else. Each run:

- **Code:** copies the files git tracks, with uncommitted changes, into
  throwaway containers built from `verify/Dockerfile` (built once per
  machine). `profiles/`, `.env` files and `.cache/` are gitignored, so they
  never reach a sandbox.
- **Unit tests:** run there, with Telegram unreachable.
- **Scenarios:** each scenario in `verify/scenarios/*.yaml` gets its own
  sandbox:
  - the real gateway code, driven by a fake Telegram;
  - this machine's model, embeddings, Qdrant, Whisper and TTS;
  - a tmpfs workspace;
  - Qdrant collections named `verify_<time>_<id>_`, deleted at the end. Ones
    left by a crash are deleted after an hour, and the nightly backup skips
    them.
- **Services:** found by copying a fixed allowlist of service settings
  (addresses, context sizes, `/rag` settings) and the Docker network from a
  running `openclaw-telegram-*` container. Set `VERIFY_REFERENCE=<container>`
  in `.verify.local` (gitignored) to choose one. Chat IDs, tokens, prompts
  and data are never copied.
- **Reports:** go to `.cache/verify/`. The exit code is 0 when everything
  passed, or was skipped because a service isn't available on this machine.

The sandbox only knows a made-up chat ID. A real-looking bot token stops
the run, and `api.telegram.org` resolves to `127.0.0.1`.

## Platform check and profiles

`bin/verify platform` (`verify/platform_check.py`) checks this machine's
services, whatever the hardware.

**Pass or fail.** These checks must pass:

- the model answers;
- its context window (llama.cpp `/props` or vLLM `/v1/models`) is at least
  the configured one;
- no reasoning leaks into answers;
- the bots' JSON schemas come back complete;
- a long prompt is answered from its middle;
- the vision model reads the fixture receipt word for word in English,
  Traditional and Simplified Chinese;
- embeddings, Qdrant, Whisper and TTS work where present.

**Measured.** Speeds are compared with a profile in `verify/platforms/`, and
a slow number is a warning, not a failure:

- generation and prompt-reading speed;
- the long prompt's time;
- image-text time;
- embedding time.

Each timed prompt starts with a random line, so a prompt cache can't make
reading look faster than it is.

The profile is the highest-`priority` one whose `[match]` fits the host:

- `board` and `gpu` are substrings of the board name (device tree or DMI)
  and the `nvidia-smi` GPU name; `gpu = "none"` means no GPU;
- `arch` must equal the CPU architecture exactly.

`gb10` and `orion-o6` are calibrated on real machines. `nvidia-gpu` and
`cpu-only` are loose fallbacks. Set `VERIFY_PLATFORM=<name>` in
`.verify.local`, or pass `--platform`, to choose one.

To add a machine, copy a profile, set its `[match]` and
`run.long_prompt_tokens`, and leave the thresholds at 0 (no limit). Run
`bin/verify platform`, then set the limits from what it measured, with
about 30% room.

```toml
name = "my-box"
priority = 30
[match]
board = "My Board"          # or gpu = "RTX 4090", arch = "x86_64"
[run]
long_prompt_tokens = 6000
[thresholds]
generation_tokens_per_s_min = 11
prompt_tokens_per_s_min = 30
long_prompt_seconds_max = 400
vision_seconds_max = 120
embedding_ms_max = 1000
```

## The gold set: answer quality against a baseline

`bin/verify gold` (`verify/gold_check.py`) measures quality on a fixed,
made-up set in `verify/gold/`.

**Inputs:**

- five documents in English, Traditional and Simplified Chinese, among them
  a manual and a log full of near-identical sections. They are filed into
  the knowledge base and three categories, as a bot would file them.
- 48 questions in `gold.yaml`:
  - 16 asked in the document's language;
  - 22 across languages (Chinese about English and the reverse), 12 of them
    also paraphrased, the hardest kind;
  - 10 paraphrased in the same language.
- 7 images, each with its exact text: clean, rotated, blurred,
  low-contrast, small print, a Traditional Chinese notice and a Simplified
  Chinese table. `make_images.py` regenerates them on a host with CJK fonts.

**Measures:** all scored by code, no model judging another.

- **retrieval:** did `/rag`, with the bot's real settings (relevance margin,
  keyword search, budget), send the right document to the model?
- **answers:** does the `/rag` answer contain one of the expected strings?
- **ocr:** the image text's character accuracy.

**When a run fails:**

- below the `minimums` in `gold.yaml` (the same on every platform);
- or more than the `tolerances` below this machine's baseline,
  `.cache/verify/baseline-<platform>.json`. The first run that passes is
  saved as the baseline; `--accept` replaces it after an intended change,
  such as a new model.

**Warnings only:**

- questions that were right in the baseline and are wrong now;
- answers or images more than 1.5x slower;
- a different model.

With the 48 questions (October 2026; the baselines are this set):

| | Retrieval | Answers | Cross-language answers |
| --- | --- | --- | --- |
| GB10, nomic-embed-text | 73% | 73% | 41% |
| GB10, qwen3-embedding:0.6b | 100% | 100% | 100% |
| O6, nomic-embed-text | 71% | 67% | 32% |
| O6, qwen3-embedding:0.6b | 100% | 90% | 82% |

Same-language questions are answered either way. Cross-language ones need
a multilingual embedding model: `nomic-embed-text` links Chinese and
English poorly, and keyword search can't help when no words are shared.

## Trying a setting before switching to it

`--env OPENCLAW_KEY=VALUE` changes a setting in the sandboxes only, never
in the bots. A run with it is compared with the baseline but never saved
as one. This is how the embedding models were compared:

```bash
bin/verify gold --env OPENCLAW_EMBEDDING_MODEL=qwen3-embedding:0.6b --env OPENCLAW_EMBEDDING_VECTOR_SIZE=1024
bin/verify standard --env OPENCLAW_EMBEDDING_MODEL=qwen3-embedding:0.6b --env OPENCLAW_EMBEDDING_VECTOR_SIZE=1024
```

## Where it runs

- **Releases:** `bin/release` runs `bin/verify standard` here, where it must
  pass, and on the machines in `RELEASE_VERIFY_REMOTE` through `--remote`,
  where a failure only warns.
- **Every week:** `scripts/openclaw_maintenance.py verify` runs
  `bin/verify full` on Monday at 01:00, a quiet hour for the model. The
  watchdog's weekly summary reports it, and a failure alerts on Telegram
  (`docs/RUNTIME_LIFECYCLE.md`).
- **Coverage:** `standard` and `full` end with the commands and templates
  no scenario uses. Today none are left; `/doc`, `/search`, `/review`,
  `/w`, `/vocab` and `/say` are exempt with a stated reason in
  `verify/host.py`.
- **Outside a git checkout:**
  - A `--remote` target receives the sender's exact git file list
    (`.verify-files`) and uses it.
  - A deployed copy without that list falls back to the code, tests,
    scripts, docs and examples.
  - `profiles/` is never included.

## Writing a scenario

One file per feature. Every new feature gets one.

```yaml
name: work log -- skip, then fill in an earlier day
requires: [model, embeddings, qdrant]   # skipped where one is missing (also: vision, whisper, tts)
retries: 1                              # rerun once in a fresh sandbox (earlier failures are still reported)
timeout: 900                            # seconds (default 900)
clock: "2026-10-01T17:55"               # the scenario clock (Europe/London unless timezone: is set)
checkins: [worklog]                     # templates copied into the check-in folder
env:                                    # OPENCLAW_* settings for this scenario (no tokens, chat IDs or owners)
  OPENCLAW_RAG_KEYWORD_SEARCH: "true"
files:                                  # extra files in the workspace
  checkins/holidays.txt: "2026-10-06\n"
steps:
  - clock: "2026-10-01T18:00"           # move the clock
    tick: true                          # one pass of every check-in's schedule
    expect: {contains: ["【收工紀錄 1/3】"], buttons: ["Skip today"]}
  - tap: "Skip today"                   # press a button (by its text or callback data)
  - say: "/mem Renew it due:{today+1}"  # a message; {today}, {today+N}: real dates
  - upload: {file: battery_cells.md, caption: "#batteries"}   # a document from verify/fixtures/
  - photo: receipt_three_scripts.png
  - inbox: {file: claw_debugger_manual.md, to: knowledge}     # a file the memory watcher would find
  - ingest: true                        # index the inbox now
```

Each `expect` checks the replies sent during that step:

| Key | Passes when |
| --- | --- |
| `contains: [..]` | every string appears (case-insensitive) |
| `contains_any: [..]` | at least one appears |
| `not_contains: [..]` | none appears |
| `matches: <regex>` | the regex matches |
| `buttons: [..]` | each is a button's text or callback data |
| `none: true` / `count: N` | no reply / exactly N |
| `language: en` or `zh` | the replies are mostly in that language |
| `status: {checkin, date, status, answered}` | the check-in entry has that status (and number of answers) |

A scenario also fails when the gateway logs a traceback or a loop error.

Fixtures (`verify/fixtures/`) are made up: a long manual with many
near-identical sections, a Traditional Chinese note, a short battery note
and a receipt in English, Traditional and Simplified Chinese. Add your own
there, never real documents.
