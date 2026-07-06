# Runtime Profiles

OpenClaw can run multiple logical profiles from the same repository checkout.
This is useful when a private personal assistant and a public demo assistant
must not share runtime data.

The current profile support focuses on data isolation. It does not yet support
running two profiles at the same time on one host, because service names and
host ports are still shared.

## What A Profile Separates

Each profile should have its own:

- `.env`
- Telegram bot token
- Telegram allowed chat IDs
- cron chat IDs
- Gateway token
- workspace
- Gateway state
- Qdrant collection names
- task history
- cron job state

The source code remains shared.

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

The first profile implementation is intended for switching between personal and
demo profiles. Running both at the same time still requires a future lifecycle
and port-isolation layer:

- remove or parameterize `container_name`
- parameterize host ports
- add `openclawctl --profile`
- add safe demo reset/seed commands
