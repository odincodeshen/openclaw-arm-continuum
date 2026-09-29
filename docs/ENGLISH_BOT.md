# English-learning coach bot (optional, per-bot)

A bot can run a daily English coach: one listening or speaking task every
morning, feedback on each voice or text reply, a word lookup, and
spaced-repetition review of looked-up words. It's off by default and meant
for a dedicated bot (see `docs/PROFILES.md` "Run Several Bots At Once").

## The week

Every week starts on Monday with a new BBC *Desert Island Discs* episode;
the rest of the week builds on it.

| Day | Task | Reply | Feedback |
|---|---|---|---|
| Mon | Listen to a guest-led stretch of the episode (sent as a Telegram voice message -- an Opus clip that plays inline -- with a link to the full episode); learn 3 chunks (phrases) picked from it, each with a Chinese meaning and an English example that uses it | One message, by number: 1. the gist, 2. a 3-gap dictation from the clip, 3. one chunk in your own sentence | Gist check, dictation word by word, chunks used, then the clip's transcript to check what you heard |
| Tue | Shadow a short audio clip of the guest, with stress and pauses marked | Voice | Word-level accuracy (missed/extra words), pace, a short analysis in Traditional Chinese, the reference text |
| Wed | IELTS Speaking Part 2 cue card; from week 18, Part 2 plus a related Part 3 question | Voice, 1.5–2 min | STAR check (Situation/Task/Action/Result), vocabulary upgrades, a band-8 model answer; Part 3 adds a claim/concession/conclusion check |
| Thu | British workplace small talk: a colleague's opener | Voice | Anchor & Bounce check, a more natural phrasing, a model reply, the colleague's reply |
| Fri | Use this week's chunks (listed with their English examples) in a 1-minute ramble on anything | Voice | Which chunks were used, a model ramble using all of them |
| Sat | Cloze quiz on this week's chunks, from your own sentences when there are any | Text or voice, all answers in one message | Right/wrong per blank (position matters), an explanation and example per chunk, the answer key |
| Sun | A Guardian lifestyle column to read for the gist, then a weekly recap | None | — |

The Monday transcript comes from the local Whisper service; the chunk
choice, annotations and every evaluation come from the local model. Tuesday's
stress marks are a best guess from the text, not a measurement of the audio.

Monday's **listening check** makes it intensive listening: the dictation is one
sentence from the clip with three content words blanked (never words from the
week's chunks, which are on the same card), graded word by word with one typo
allowed on longer words since the answer comes from a Whisper transcript. The
gist and the chunk sentence are judged in the same single model call, and the
feedback then shows the transcript of the clip.

After the reading card, Sunday sends each learner a **weekly recap**: which of
Monday–Saturday they answered, their Monday listening result, which of the week's chunks they've learned,
and -- with the word list enabled -- how many words they looked up. A chunk
counts as learned when its latest Monday, Friday and Saturday attempts were
all right; one that's still flagged, or was never practised, moves into that
learner's `/vocab review` queue (due Monday, then on the usual
spaced-repetition schedule), quizzed from their own sentence when they made
one.

Sunday picks a recent column not sent before: from Tim Dowling's Guardian
feed first, then Grace Dent's, then the Guardian lifestyle feed. It sends the
link with a short English summary and its Traditional Chinese translation.

## Messages

Every task and every piece of feedback uses the same card layout, rendered
with Telegram HTML:

- task card: short Chinese title and week, then **Goal**, **Time**,
  **Today** (the only part that differs by day) and **How to**;
- feedback card: **Result**, **Tips**, **Example** and **Your answer** (both
  collapsed until tapped), then how to continue.

When the word list is enabled and saved words are due, the task card ends
with a **Word review** block. See `docs/DICTIONARY.md`.

## Replying

- A day's task stays open until you send `/Done`, so you can try as many
  times as you like; each attempt is evaluated, and the last one before
  `/Done` is what counts. A chunk is flagged for review while the latest
  Monday, Friday or Saturday attempt at it is wrong -- redoing Friday can
  clear Friday's miss, but a wrong Saturday answer still flags it.
- Voice replies shorter than 10 seconds are not counted (an accidental tap
  doesn't use up the task).
- Commands (anything starting with `/`, except `/Done`) are never treated as
  an answer, so `/w` works in the middle of a task.
- Open tasks are held in the Telegram process's memory: restarting that
  container drops an unanswered task.

At 21:00 every task still not answered that week is marked `skipped`.

## Settings

```text
OPENCLAW_ENGLISH_BOT_ENABLED=true
OPENCLAW_ENGLISH_BOT_OWNERS=<chat_id>[,<chat_id>...]
OPENCLAW_ENGLISH_BOT_PUSH_TIME=07:15
OPENCLAW_ENGLISH_BOT_SWEEP_TIME=21:00
OPENCLAW_ENGLISH_BOT_TIMEZONE=Europe/London
OPENCLAW_ENGLISH_BOT_STATE_PATH=/workspace/.openclaw/english_bot_state.json
OPENCLAW_ENGLISH_BOT_PREPARE_TIME=04:00      # optional, see below
```

The scheduler runs inside the Telegram gateway process. It pushes once a day
after `PUSH_TIME` and sweeps once after `SWEEP_TIME`, both in `TIMEZONE`, and
remembers what it already did in `STATE_PATH`.

**Off-peak preparation.** With `PREPARE_TIME` set, everything the day's push
needs the model, Whisper or audio clipping for is generated then (any time in
the 90 minutes after it), and the finished cards, clips and task are held in
the state file until `PUSH_TIME`, when they are sent and the task opens. The
learner sees no difference. Preparation is tried once a day; if it fails, or
the bot was down, the push is generated at `PUSH_TIME` as usual.

Open tasks (and open `/vocab review`s) are also saved to
`OPENCLAW_PENDING_STATE_PATH` (default `/workspace/.openclaw/pending_answers.json`),
so restarting the container doesn't drop them.

For testing, `OPENCLAW_ENGLISH_BOT_FORCE_DAY_CODE=mon`…`sun` makes every day
behave as that weekday. To re-run a push on the same day, also clear
`last_push_date` from the state file and set `PUSH_TIME` a couple of minutes
ahead. Remove the override afterwards.

## Data

Everything lives in the bot's tracker collection
(`OPENCLAW_TRACKER_COLLECTION`):

- **Shared weekly content** (episode, transcript, chunks, which IELTS
  questions and Sunday articles were already used) has no owner: everyone on
  `OPENCLAW_ENGLISH_BOT_OWNERS` gets the same week.
- **Per-person records** (daily completion, chunk progress, your saved
  sentences, word list) carry an `owner` field, and every read of them must
  filter by it -- the helpers in `owned_records.py` refuse to run without one.

The week number is the highest week stored so far; Monday's push creates the
next one from the newest full-length episode not used yet -- when the show is
on a break (only short daily clips), that's an older episode from the feed.
Monday only skips the push when every full-length episode has been used.

## Requirements

- The Whisper service (`openclaw-whisper`) for Monday's transcript and voice
  replies; it also cuts the audio clips (PyAV).
- Outbound HTTPS to the BBC podcast feed and the Guardian.
- The IELTS Part 2 bank has 112 cue cards (28 each: events, people, places,
  objects) -- over two years of Wednesdays without a repeat; once all have
  been asked it starts again from the full bank.
