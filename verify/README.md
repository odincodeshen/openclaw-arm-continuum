# bin/verify: self-verification without personal data

```bash
bin/verify                # standard: unit tests, then every scenario
bin/verify quick          # unit tests and ci_validate only (no services needed)
bin/verify scenarios      # scenarios only
bin/verify scenarios --only checkin_skip_fillin rag_keywords_chinese
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

## Writing a scenario

One file per feature. Every new feature gets one.

```yaml
name: work log -- skip, then fill in an earlier day
requires: [model, embeddings, qdrant]   # skipped where one is missing (also: vision, whisper, tts)
retries: 1                              # rerun once from scratch, for answers a CPU model may vary on
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
