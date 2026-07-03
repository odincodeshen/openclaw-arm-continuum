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

Send a message to your bot, then inspect updates through Telegram's API or your preferred bot tooling. Put your chat id into:

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
