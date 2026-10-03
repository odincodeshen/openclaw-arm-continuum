# Telegram Setup

## Bot Token

1. Open Telegram.
2. Talk to BotFather.
3. Create a bot.
4. Copy the bot token into `.env`:

```text
OPENCLAW_TELEGRAM_BOT_TOKEN=<your-token>
```

## Chat Id

Start the bot with your account not yet on the allowlist and send it any
message. The bot ignores it, and its log shows the ID:

```bash
docker logs openclaw-telegram-<bot> 2>&1 | grep "rejected chat_id"
```

This is the only log line with a full chat ID. Everywhere else
(`chat_id=`, `owner=`, `owners=`, `recipients=`) only the last three digits
are logged, e.g. `chat_id=…175`. That is enough to tell accounts apart
without putting an account's ID in logs that get copied around.

Put your chat id into:

```text
OPENCLAW_TELEGRAM_ALLOWED_CHAT_IDS=<your-chat-id>
OPENCLAW_CRON_CHAT_IDS=<your-chat-id>
```

Only allowlisted chat ids can use the runtime.

## First Commands

```text
/help
/mem #preference Answer in Traditional Chinese
/rag memory: What language preference did I save?
/search UK weather tomorrow
```
