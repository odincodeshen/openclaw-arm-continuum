# Use Cases

This page gives practical English-language scenarios for trying
`openclaw-arm-continuum` after the runtime is installed.

The examples assume Telegram is configured and the runtime is connected to a
local or trusted private-network OpenAI-compatible model endpoint.

## 1. Private Personal Memory

Use OpenClaw as a local memory notebook for preferences, reminders, and small
facts you want your assistant to remember.

Why it matters:

- Memory stays in your local Qdrant collections.
- Telegram becomes the capture UI.
- Later questions can retrieve the saved context with `/rag`.

Try it in Telegram:

```text
/mem #preference I prefer short technical answers with concrete commands.
/rag memory: How do I prefer technical answers?
```

Expected result:

OpenClaw should retrieve the saved memory and answer using the preference you
stored.

## 2. Local Document RAG

Use OpenClaw to ingest manuals, notes, PDFs, logs, or public Google Docs into a
local knowledge base.

Why it matters:

- Documents stay on the local host.
- Long-term knowledge can be stored separately from dynamic personal memory.
- You can ask follow-up questions without uploading files to a cloud chatbot.

Try it in Telegram:

```text
/doc url https://docs.google.com/document/d/<public-doc-id>/edit
/rag knowledge: Summarize the imported document in five bullets.
```

For Telegram file uploads, attach a supported file and use a caption shortcut:

```text
/knowledge
```

or:

```text
/mem
```

Expected result:

OpenClaw saves the file or document into the local workspace, indexes it, and
lets `/rag` retrieve relevant context.

## 3. Local Web Research

Use OpenClaw to search the web through the local Playwright scraper worker and
summarize the result with your local model.

Why it matters:

- The browser worker runs locally.
- Search results can be written into the local inbox for later indexing.
- The local model summarizes the result instead of sending your conversation to
  a public LLM API.

Try it in Telegram:

```text
/search latest Arm server CPU news
```

Expected result:

OpenClaw should browse, extract useful page content, and return a concise
summary with source context when available.

Note:

Weather questions are routed as plain language, not through `/search`:

```text
Berlin weather tomorrow
```

## 4. Proactive Cron Briefings

Use OpenClaw as a proactive assistant that sends scheduled Telegram updates.

Why it matters:

- Cron tasks are configured dynamically from Telegram.
- Jobs can run daily, weekly, or monthly.
- Results are pushed to your configured chat ID.

Try it in Telegram:

```text
/cron add daily 07:30 Morning weather :: Berlin weather today
/cron add weekly mon 08:00 AI hardware roundup :: /search latest AI hardware news
/cron list
```

Expected result:

OpenClaw lists the scheduled jobs and sends results at the configured time
window.

You can also open the Gateway dashboard through a local or SSH-tunneled browser
session to inspect cron state.

## 5. Voice Notes Into Local Reasoning

Use Telegram voice messages as hands-free input.

Why it matters:

- The local Whisper service transcribes voice messages.
- The transcribed text is routed through the same skill and agent pipeline as a
  normal Telegram message.
- This is useful for quick notes, reminders, and mobile capture.

Try it in Telegram:

1. Send a short voice message to the bot.
2. Say something like:

```text
Remember that the demo profile should use a separate Telegram bot.
```

Expected result:

OpenClaw should transcribe the voice note and process it as text. If the message
is phrased as a memory request, save it with `/mem` in a follow-up command or
send the text form directly:

```text
/mem Demo profile should use a separate Telegram bot.
```

## 6. Personal vs Demo Runtime Profiles

Use separate profiles when you want to demo OpenClaw without exposing your real
personal memory, files, Telegram bot, or cron jobs.

Why it matters:

- `personal` and `demo` can use separate `.env` files.
- Each profile can use separate workspace and Gateway state directories.
- Each profile can use separate Qdrant collection names.

Create profile env files:

```bash
cp profiles/personal/.env.example profiles/personal/.env
cp profiles/demo/.env.example profiles/demo/.env
```

Start the demo profile:

```bash
docker compose \
  --env-file profiles/demo/.env \
  -f compose.arm-cpu-only.yaml \
  --profile web \
  --profile gateway \
  --profile voice \
  up -d
```

Expected result:

Messages sent to the demo bot use demo collections, demo workspace, and demo
Gateway state instead of your personal runtime data.

## 7. Arm Continuum Deployment Demos

Use the same project to demonstrate different local-first deployment shapes.

Why it matters:

- Edge and server deployments can share the same Arm architecture family.
- CPU-only hosts can run small local models for private assistance.
- Larger DGX / GB10-class workstations can run high-throughput vLLM locally.
- Small Arm gateway hosts can route inference to a trusted private LAN model
  server in the future.

Suggested demo story:

```text
1. Start with Telegram as the single user interface.
2. Save a memory with /mem.
3. Retrieve it with /rag memory:.
4. Run a local /search query.
5. Show a scheduled /cron job.
6. Explain how the same runtime can move between Arm CPU-only, DGX / GB10, and
   future Arm gateway + private LAN LLM profiles.
```

Expected result:

The audience should understand OpenClaw as a local-first personal AI runtime,
not a single-board demo or a cloud API wrapper.

## Suggested First Smoke Test

After setup, this is the fastest end-to-end test:

```text
/help
/mem #demo OpenClaw can remember local private context.
/rag memory: What can OpenClaw remember?
/search latest Arm AI software news
/cron add daily 07:30 Local morning check :: Singapore weather today
/cron list
```

If those commands work, the Telegram gateway, memory path, RAG retrieval, web
scraper, cron worker, and local model routing are all exercising useful parts of
the runtime.
