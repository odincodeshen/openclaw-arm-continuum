# Runtime Profiles

OpenClaw can run multiple logical profiles from the same repository checkout.
This is useful when a private personal assistant and a public demo assistant
must not share runtime data.

A profile is a data-isolation unit. One profile can run on its own (see
"Start One Profile"), or several can run at the same time as separate
Telegram bots sharing one model engine (see "Run Several Bots At Once").

## What A Profile Separates

Each profile should have its own:

- `.env`
- Telegram bot token
- Telegram allowed chat IDs
- cron chat IDs
- Gateway token
- workspace
- Gateway state
- `OPENCLAW_TRACKER_COLLECTION` / `OPENCLAW_KNOWLEDGE_COLLECTION` (plain env
  vars -- just set them)
- `OPENCLAW_CATEGORY_COLLECTION_PREFIX` (**do not skip this one** -- see
  below)
- task history
- cron job state

The source code remains shared.

### Category RAG collections need their own prefix -- this one is easy to miss

Unlike the tracker/knowledge collections above, a Category RAG collection's
name is derived from the category's *display name* (`category_slug()`),
not from anything profile-specific. If two profiles both leave
`OPENCLAW_CATEGORY_COLLECTION_PREFIX` at its default (`oc_cat_`) and each
independently creates a category with the same name (e.g. both use
`trip`), they silently resolve to the **same Qdrant collection** and their
content gets mixed together -- no error, no warning, just diluted/wrong
`/rag #<category>` answers once enough unrelated content piles up.

Give every profile its own `OPENCLAW_CATEGORY_COLLECTION_PREFIX` (e.g.
`bot-a_oc_cat_`) for real isolation. This was found via real multi-bot
usage, not caught in review -- see `docs/CATEGORY_RAG.md`.

## Naming and privacy

This repository is public. A profile directory name, and any `.env.example`
or compose file you add for it, ends up tracked in git if you commit it --
unlike `.env` itself, a filename is not secret-scanned or gitignored by
default. Keep profile names generic (`personal`, `demo`, `bot-a`) rather than
a real person's name, a household relationship, or a private topic. Real,
identifying profile configuration (the `.env` itself, and any per-profile
compose file -- see `compose.persona.*.yaml` in `.gitignore`) stays local.
See `docs/PUBLISH_CHECKLIST.md`.

## Who Uses A Bot, And Who Gets What

Data is separated per bot, never per Telegram account. Everyone on a bot's
allowlist reads and writes the same memory, knowledge, categories and
`/cron` jobs. Whether people share data or keep it apart is therefore a
choice of how many bots to run and whom each one allows. It is set in each
bot's `.env`, with no code change.

Each role has its own setting:

| Setting | Decides | Empty means |
|---|---|---|
| `OPENCLAW_TELEGRAM_ALLOWED_CHAT_IDS` | who can use the bot (and so shares its data) | anyone who finds the bot |
| `OPENCLAW_CRON_CHAT_IDS` | who receives scheduled results | everyone on the allowlist |
| `OPENCLAW_ALERT_CHAT_IDS` | who receives operator alerts | no alerts |
| `OPENCLAW_CHECKIN_OWNER` (or the variable a check-in's `owner_env` names) | who a check-in asks | that check-in is off |
| `OPENCLAW_NIGHT_RITUAL_OWNER`, `OPENCLAW_ENGLISH_BOT_OWNERS` | who the night ritual / English coach is for | off |

All values are comma-separated Telegram chat IDs, except the single-owner
ones. Chat IDs live only in `.env`, never in check-in files or compose
files.

Everyone on the allowlist has full use of the bot. There is no read-only
role. Removing someone from the allowlist doesn't delete `/cron` jobs that
deliver to their chat; remove those with `/cron delete`.

### Example layouts

Placeholders: `<A>`, `<B>`, `<C>` are three people's chat IDs.

**One shared bot plus one private bot per person.** Everyone uses `bot-home`.
`bot-a` and `bot-b` each belong to one person.

```dotenv
# profiles/bot-home/.env -- shared data
OPENCLAW_TELEGRAM_ALLOWED_CHAT_IDS=<A>,<B>,<C>
OPENCLAW_CRON_CHAT_IDS=<A>,<B>,<C>
OPENCLAW_ALERT_CHAT_IDS=<A>          # whoever runs the box

# profiles/bot-a/.env -- A's own data
OPENCLAW_TELEGRAM_ALLOWED_CHAT_IDS=<A>
OPENCLAW_CRON_CHAT_IDS=<A>
OPENCLAW_ALERT_CHAT_IDS=<A>

# profiles/bot-b/.env -- B's own data
OPENCLAW_TELEGRAM_ALLOWED_CHAT_IDS=<B>
OPENCLAW_CRON_CHAT_IDS=<B>
OPENCLAW_ALERT_CHAT_IDS=<A>          # alerts still go to the operator
```

**One main user plus a supervisor.** Both can use the bot and see the same
data. Pushes, the daily check-in and alerts go only to the main user.

```dotenv
# profiles/bot-work/.env
OPENCLAW_TELEGRAM_ALLOWED_CHAT_IDS=<A>,<B>   # A = main user, B = supervisor
OPENCLAW_CRON_CHAT_IDS=<A>
OPENCLAW_ALERT_CHAT_IDS=<A>
OPENCLAW_CHECKIN_OWNER=<A>
```

To make the supervisor receive the weekly reports too, add `<B>` to
`OPENCLAW_CRON_CHAT_IDS`. To make them an equal second user, add them
everywhere except the single-owner settings.

## Directory Layout

```text
openclaw-arm-continuum/
  profiles/
    personal/
      .env
      workspace/
      gateway-data/
    demo/
      .env
      workspace/
      gateway-data/
```

Only `.env.example` files should be committed. Real `.env`, workspace, and
Gateway state are ignored by git.

## Arm CPU-Only Example

Create a personal profile:

```bash
cp profiles/personal/.env.example profiles/personal/.env
```

Create a demo profile:

```bash
cp profiles/demo/.env.example profiles/demo/.env
```

Edit each file and fill in profile-specific private values:

```env
OPENCLAW_TELEGRAM_BOT_TOKEN=
OPENCLAW_TELEGRAM_ALLOWED_CHAT_IDS=
OPENCLAW_CRON_CHAT_IDS=
OPENCLAW_GATEWAY_TOKEN=
```

Use a different Telegram bot for the demo profile whenever possible.

## Start One Profile

Personal:

```bash
docker compose \
  --env-file profiles/personal/.env \
  -f compose.arm-cpu-only.yaml \
  --profile web \
  --profile gateway \
  --profile voice \
  up -d --build
```

Demo:

```bash
docker compose \
  --env-file profiles/demo/.env \
  -f compose.arm-cpu-only.yaml \
  --profile web \
  --profile gateway \
  --profile voice \
  up -d --build
```

The selected profile `.env` must set:

```env
OPENCLAW_ENV_FILE=profiles/demo/.env
OPENCLAW_HOST_WORKSPACE=./profiles/demo/workspace
OPENCLAW_HOST_GATEWAY_DATA=./profiles/demo/gateway-data
```

For the personal profile, use `profiles/personal/...` paths instead.

## Run Several Bots At Once

Each extra bot is a profile plus its own compose file,
`compose.persona.<name>.yaml`, copied from the tracked
`compose.persona.example.yaml`. That file gives the bot's four
per-bot services (Telegram, memory watcher, cron, Gateway) their own
container names and the Gateway its own host port, and `include`s
`compose.yaml` for the services every bot shares: `openclaw-vllm`,
`openclaw-whisper`, `openclaw-browser-scraper`, and the host's Qdrant.

1. Create `profiles/<name>/.env` with the bot's own token, chat IDs, Gateway
   token, `OPENCLAW_HOST_WORKSPACE` / `OPENCLAW_HOST_GATEWAY_DATA`, and its own
   `OPENCLAW_TRACKER_COLLECTION`, `OPENCLAW_KNOWLEDGE_COLLECTION` and
   `OPENCLAW_CATEGORY_COLLECTION_PREFIX`.
2. Copy `compose.persona.example.yaml` to `compose.persona.<name>.yaml`,
   replace `bot-a` / `bot_a` with the name, and pick an unused Gateway host
   port.
3. Create the Gateway data directory as your own user **before** the first
   start: `mkdir -p profiles/<name>/gateway-data/state`. If Docker creates it
   instead, it's owned by root, the Gateway can't write its state, and its
   `admin-http-rpc` plugin silently fails to load -- the bot's cron then
   can't read jobs set on the Gateway dashboard (`cron.list failed: HTTP
   404`). Fixing the ownership and restarting the Gateway recovers it.
4. Start it with `bin/openclawctl --profile <name> start`. That starts only
   the bot's own services, always with `--no-deps` -- without it, Compose
   recreates the shared services with this bot's settings and briefly
   interrupts every other bot -- and creates the Gateway `state` directory
   first (step 3). `status`, `restart`, `stop` and `logs` work the same way;
   see `docs/RUNTIME_LIFECYCLE.md`.

Real `compose.persona.*.yaml` copies are gitignored -- keep them local.

## Stop The Current Profile

```bash
docker compose \
  --env-file profiles/demo/.env \
  -f compose.arm-cpu-only.yaml \
  --profile web \
  --profile gateway \
  --profile voice \
  down
```

Use the matching profile `.env` for the profile that is currently running.

## Validation

### 1. Workspace Isolation

Send a memory command to the demo bot:

```text
/mem demo profile isolation smoke test
```

Then confirm files and state are under:

```text
profiles/demo/workspace/
```

The personal profile workspace should not change.

### 2. Qdrant Collection Isolation

Demo profile:

```env
OPENCLAW_TRACKER_COLLECTION=demo_tracker_memory
OPENCLAW_KNOWLEDGE_COLLECTION=demo_knowledge_base
```

Personal profile:

```env
OPENCLAW_TRACKER_COLLECTION=personal_tracker_memory
OPENCLAW_KNOWLEDGE_COLLECTION=personal_knowledge_base
```

Expected behavior:

- The demo bot can retrieve demo memories.
- The personal bot cannot retrieve demo-only memories.

### 3. Telegram Isolation

Expected behavior:

- Demo bot messages only produce demo bot responses.
- Personal bot messages only produce personal bot responses.
- Cron pushes only go to the profile's configured chat IDs.

### 4. Gateway Isolation

Profile Gateway state should live under:

```text
profiles/<profile>/gateway-data/
```

Cron state should live under:

```text
profiles/<profile>/workspace/.openclaw/
```

The demo dashboard should not list personal cron jobs.

## Current Limitation

Adding a bot is still a compose-authoring step (copy the example file, see
above); `bin/openclawctl --profile` then runs it. Not done yet:

- safe demo reset/seed commands
