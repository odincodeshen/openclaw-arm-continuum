# Video summary relay (optional, per-bot)

Lets a bot accept a bare YouTube link as input, hand it off to an external
Gemini-based summarizer (a Google Apps Script Web App), and have the
resulting summary land back in the same chat as a normal message -- which
then flows straight into [Category RAG](CATEGORY_RAG.md)'s existing
reply-to-message ingest.

This feature is entirely optional and off by default; it only makes sense
for a bot whose owner has their own Apps Script deployment doing the actual
Gemini call. OpenClaw itself has no video/transcript understanding -- it
only detects the link and relays it.

## Why this needs a relay instead of a webhook

A Telegram bot token can only use **one** update-delivery mechanism at a
time: webhook or `getUpdates` polling, never both. OpenClaw's gateway uses
polling (needed for the reply-based Category RAG ingest and every other
command). So an external service cannot also register a webhook on the same
bot to watch for incoming links -- Telegram would silently stop delivering
updates to OpenClaw's polling the moment a webhook is registered (confirmed
in production: `getUpdates` returns `409 Conflict` until the webhook is
deleted).

The relay design keeps OpenClaw as the sole receiver: OpenClaw sees the
link first (via polling), and pushes it *out* to the external service over
plain HTTP -- no webhook involved on either side.

## Flow

```
user -> bot: https://youtu.be/xxxx  (nothing else in the message)
bot -> user: "Got it -- sending this to Gemini for a summary..."
bot -> Apps Script (HTTP POST): {"video_url": "...", "secret": "..."}
Apps Script -> Gemini API: generate summary
Apps Script -> bot (sendMessage): pushes the summary into the same chat
user -> bot: reply to that summary with #<category>
bot -> RAG: files it (existing reply-ingest flow, unchanged)
```

Only a message whose entire trimmed text is a single `youtube.com` or
`youtu.be` link triggers the relay. A link mixed with other text (e.g.
"is this worth watching? https://youtu.be/xxxx") is left alone and falls
through to normal chat / `/search` handling instead.

## Settings

| Env var | Default | Meaning |
|---|---|---|
| `OPENCLAW_VIDEO_SUMMARY_RELAY_URL` | *(empty, disabled)* | The Apps Script Web App `/exec` URL to POST the link to |
| `OPENCLAW_VIDEO_SUMMARY_RELAY_SECRET` | *(empty)* | Shared secret sent in the POST body's `secret` field; the Apps Script side must check it matches, since the deployed Web App is reachable by anyone who has the URL |
| `OPENCLAW_VIDEO_SUMMARY_RELAY_TIMEOUT_SECONDS` | `360` | How long the background thread waits for the Apps Script response. Runs off the polling loop, so a long wait does not block other chat traffic. Matches Apps Script's own ~6-minute execution ceiling. |

Only set these for the bot profile that actually owns the Apps Script
integration (e.g. `profiles/lc9_dgx2_apa/.env`). Every other bot leaves
`OPENCLAW_VIDEO_SUMMARY_RELAY_URL` unset, which keeps the feature off and
bare links fall through to normal `/search` handling.

## The Apps Script side (not part of this repo)

The Apps Script Web App is owned and deployed by whoever runs that bot's
integration -- it is not code in this repository. Its `doPost(e)` must
accept a plain JSON body `{"video_url": "...", "secret": "..."}` (not a
Telegram webhook update -- this endpoint is called directly by OpenClaw's
gateway, not by Telegram), reject the request if the secret does not match,
run the existing Gemini summary function, and push the result back with
`sendMessage`. Responds `{"ok": true}` on success or `{"ok": false, "error":
"..."}` on failure so the relay call can report a failure back to the user
instead of leaving them waiting silently.

Apps Script Web Apps serve whichever version was last **deployed**, not
whatever is currently saved in the editor -- a code change needs a new
deployment (Deploy -> Manage deployments -> edit -> new version) to take
effect.
