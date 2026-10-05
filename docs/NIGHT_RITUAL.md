# Night ritual (optional, per-bot)

A short wind-down journal: every ritual night the bot asks four questions,
one at a time, then closes the day. The point is not long writing but a
clear line between work and rest -- about three to five minutes.

It's off by default and runs for exactly one person
(`OPENCLAW_NIGHT_RITUAL_OWNER`, a Telegram chat ID).

The night ritual is a **check-in** (`docs/CHECKINS.md`): its questions,
cards and report sections are the preset
`app/openclaw_runtime/checkin_presets/night.toml`, run by the same engine as
any other check-in. `OPENCLAW_NIGHT_RITUAL_ENABLED=true` uses that built-in
preset with the settings below. To change its questions or wording, copy
the file into the bot's check-in folder, edit it, and turn the built-in one
off.

## A night

At `OPENCLAW_NIGHT_RITUAL_TIME` (default 22:30) on
`OPENCLAW_NIGHT_RITUAL_DAYS` (default Sunday-Friday):

0. **Yesterday's first thing** -- if the previous night named a first thing
   for today, a card shows it with **✅ Done** / **❌ Not yet** buttons.
   One tap, then the questions start.
1. 【晚安儀式 1/4】 **Wins & Gratitude** -- one message, 1-3 things.
2. 【晚安儀式 2/4】 **1% Better** -- where today was a little better than yesterday.
3. 【晚安儀式 3/4】 **One Adjustment** -- one thing to smooth out, no judgement.
4. 【晚安儀式 4/4】 **Mental Shutdown** -- the first thing for tomorrow.

Each answer is text or voice (voice is transcribed by the local Whisper
service and the recording deleted; only the text is kept). A long spoken
answer is tidied by the local model into the few words the card needs, in
the language you spoke; the full transcript is kept alongside it. There is no
skip. After the fourth answer a 【今日結案】 card lists the night's answers
and ends with one or two warm sentences from the local model -- no advice,
no analysis.

Cards follow the bots' format: a Chinese title, English content; your own
answers are shown as you wrote them.

The closing card has an **Add to tomorrow's schedule** button: it saves the
first thing as a `/mem` item due the next day, tagged `night`, in
`OPENCLAW_NIGHT_RITUAL_SCHEDULE_COLLECTION` -- another bot's tracker memory
on the same Qdrant, so it shows in that bot's morning schedule report
(`/mem upcoming`) -- or this bot's own when that's empty. One tap only.

- **Reminders** at `OPENCLAW_NIGHT_RITUAL_REMINDERS` (default 23:00 and
  23:30, the last one a "last call"), only while the night is still open.
  They say where to pick up (e.g. 2/4).
- **Midnight** closes the night as it is: **partial** if some questions
  were answered (the answers are kept), **missed** if none.
- `/night start` starts early or picks up an open night; if tonight is
  already done, the 22:30 push is skipped.
- Commands still work mid-ritual; they are never taken as an answer.

## The next morning

At `OPENCLAW_NIGHT_RITUAL_MORNING_TIME` (default 07:05) a 【今天的第一件事】
card repeats last night's first thing. No ritual last night, no card.

## /night

```
/night             the last 7 days (✅ done · ◐ partial · ❌ missed) and the latest entry
/night 7d          the last 7 nights in full (up to 31d)
/night 2026-09-28  one night
/night start       start or pick up tonight
/night move 2026-09-29 2026-09-28   re-date a night (the target must be empty)
/night skip [2026-10-10]   skip tonight (or a coming night): no questions, not counted as missed
/night unskip [date]       undo a skip
/night start 2026-10-02    fill in a night from the last 7 days
/night edit 2026-10-02 2 <answer>   change one answer
/night week        last week's report now · /night month  last month's · /night year
```

## Reports

- **Weekly** (【晚安週報】), Monday at `OPENCLAW_NIGHT_RITUAL_REPORT_TIME`
  (default 07:30): the seven days up to Sunday.
- **Monthly** (【晚安月報】), on the 1st: the previous month.
- **Yearly** (【晚安年報】), on 1 January: the previous year, with each
  month's share of nights done.

Each shows nights done / partly done, a **Trend** block (the current streak
of done nights -- a skipped Saturday doesn't break it -- and the share of
nights done and of first things done, against the week or month before,
with ↑ / ↓ / →), up to three highlights, the
improvement that shows up most, the adjustment that keeps coming back,
how many "first things" were written and done, and a closing line. The
counts are computed in code; the wording comes from the local model. They
are generated off-peak at `OPENCLAW_NIGHT_RITUAL_PREPARE_TIME` (default
04:00) and sent at report time (generated live if that failed). A Markdown
copy with every night in full is saved under
`<dir>/<owner>/reports/week-YYYY-MM-DD.md` / `month-YYYY-MM.md`.

## Where it's kept

One JSON file per night, `<OPENCLAW_NIGHT_RITUAL_DIR>/<owner>/YYYY-MM-DD.json`
(default dir `/workspace/.openclaw/night`), plus the schedule's own
`state.json`. It is outside the inbox, so it is never indexed into memory,
never searched by `/rag`, and never part of the knowledge digest. Only the
local model and local Whisper see it. An open night is saved with the other
open tasks (`OPENCLAW_PENDING_STATE_PATH`), so a restart resumes at the same
question.

## Settings

```text
OPENCLAW_NIGHT_RITUAL_ENABLED=true
OPENCLAW_NIGHT_RITUAL_OWNER=<your Telegram chat ID>
OPENCLAW_NIGHT_RITUAL_TIMEZONE=Europe/London     # default: OPENCLAW_CRON_TIMEZONE
OPENCLAW_NIGHT_RITUAL_DAYS=sun,mon,tue,wed,thu,fri
OPENCLAW_NIGHT_RITUAL_TIME=22:30
OPENCLAW_NIGHT_RITUAL_REMINDERS=23:00,23:30
OPENCLAW_NIGHT_RITUAL_MORNING_TIME=07:05
OPENCLAW_NIGHT_RITUAL_REPORT_TIME=07:30
OPENCLAW_NIGHT_RITUAL_PREPARE_TIME=04:00
OPENCLAW_NIGHT_RITUAL_DIR=/workspace/.openclaw/night
OPENCLAW_NIGHT_RITUAL_SCHEDULE_COLLECTION=      # e.g. bot1's OPENCLAW_TRACKER_COLLECTION
```

Recreate the bot's Telegram container after changing them
(`bin/openclawctl --profile <bot> start`). `/night` appears in the command
menu and `/help` only when it's enabled.
