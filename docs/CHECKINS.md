# Check-ins: guided question flows from a TOML file

A **check-in** asks a few questions, one at a time, on a schedule. It keeps
each day's answers on the host and can:

- bring one answer back the next morning;
- ask the next time whether that answer got done;
- send weekly / monthly / yearly reports written by the local model.

The night ritual (`docs/NIGHT_RITUAL.md`) and the work log are both
check-ins. **Everything that differs between them lives in a TOML file; the
engine (`app/openclaw_runtime/checkins.py`) is the same code.** A new flow,
such as weekly goals, reading notes or a mood log, is a new file, not new
code.

## Turning one on

1. Copy a preset from `app/openclaw_runtime/checkin_presets/`
   (`night.toml`, `worklog.toml`) into the bot's check-in folder,
   `profiles/<bot>/workspace/checkins/`. Inside the container that is
   `OPENCLAW_CHECKIN_DIR`, default `/workspace/checkins`.
2. Set the owner's Telegram chat ID in the bot's `.env` under the variable
   the file names in `owner_env` (default `OPENCLAW_CHECKIN_OWNER`). Chat
   IDs never go in the TOML file, so check-in files can be shared.
3. Restart the bot (`bin/openclawctl --profile <bot> restart`). The log
   shows `[<id>] scheduler started ...`, and `/<command>` appears in the
   command menu and `/help`.

A file that doesn't parse is logged (`[checkin] skipped: ...`) and ignored.
One typo never stops the bot. A check-in whose owner variable is unset is
skipped too.

The night ritual is also built in: `OPENCLAW_NIGHT_RITUAL_ENABLED=true`
runs the `night.toml` preset with the `OPENCLAW_NIGHT_RITUAL_*` days and
times, without any file in the check-in folder.

## What every check-in gets

- **The conversation**:
  - one question at a time, answered by text or voice (Whisper);
  - a long spoken answer is tidied in the speaker's language, and the transcript is kept;
  - commands are never taken as answers.
- **Schedule**:
  - starts on the configured days and time;
  - reminders while it's still open (the last one is a "last call");
  - at the deadline, an unfinished day is saved as **partial** (some answers) or **missed**.
- **Follow-up** (optional): before the first question, a ✅ Done / ❌ Not yet
  button card about an earlier answer, e.g. "did you start with the first
  thing you planned?".
- **Morning card** (optional): that answer, the next morning.
- **Closing card**: the day's answers, an optional line from the model, and
  an optional **Add to tomorrow's schedule** button. The button saves one
  answer as a `/mem` item due tomorrow, tagged with the check-in's id.
- **Reports** (optional):
  - weekly, monthly, yearly;
  - counts, streak and comparison with the previous period, computed in code;
  - sections written by the model;
  - a Markdown copy on disk.
- **Commands**:
  - `/<command>`: last 7 days plus the latest entry;
  - `/<command> start`: start now, or pick up where it stopped;
  - `/<command> 7d` or `/<command> YYYY-MM-DD`: past entries;
  - `/<command> week|month|year`: a report now;
  - `/<command> move <from> <to>`: re-date an entry.
- **Restarts**: an open check-in resumes at the same question.

Answers are stored as one JSON file per day under
`/workspace/.openclaw/checkins/<id>/<owner>/` (`OPENCLAW_CHECKIN_DATA_DIR`).
That is outside the inbox, so they are never indexed, never searched by
`/rag`, and never part of the knowledge digest.

The model calls whose language and shape the code decides run without the
bot's persona prompt. That covers the closing line, report sections and
tidied voice answers. Otherwise a persona such as "always reply in
Traditional Chinese" can win over the check-in's "English only", as ERNIE
does on the O6.

## The file

Cards follow the bots' style: Chinese titles, English content. Answers are
shown as written.

```toml
[checkin]
id = "worklog"              # 2-24 lowercase letters/digits/_ ; also the data folder
title = "收工紀錄"           # card title: 【收工紀錄 1/3】
command = "worklog"         # optional, default = id
owner_env = "OPENCLAW_CHECKIN_OWNER"
unit = "day"                # "5 of 6 days", "Streak: 3 days" (the night ritual says "night")
subject = "Today's work log"          # "... is still open" in reminders
history_title = "工作紀錄"             # /worklog card title (default: title + 紀錄)
voice_context = "a work-log question" # what tidied voice answers answer
# timezone = "Europe/London"          # default: the bot's OPENCLAW_CRON_TIMEZONE
# data_dir = "/workspace/.openclaw/worklog"   # default: <OPENCLAW_CHECKIN_DATA_DIR>/<id>

[schedule]
days = ["mon", "tue", "wed", "thu", "fri"]
start = "18:00"
reminders = ["19:00"]       # each after start; the last is the "last call"
deadline = "00:00"          # "00:00" = midnight; or a time later the same day
reminder_title = "收工提醒"
last_call_note = "Replies count until midnight; after that today is saved as it is."

[[question]]                # one block per question, in order
id = "done"
label = "Done today"        # card heading: 【收工紀錄 1/3】· Done today
prompt = "What did you finish or move forward today?"
hint = "One message, a few items · text or voice"   # optional, italic
list = true                 # several items in one answer -> bullets
short = "Done"              # label in /worklog history (default: label)
# card = "..."              # label on the closing card / Markdown (default: label)
# shape = "..."             # what a long spoken answer is tidied into

[follow_up]                 # optional
from = "first"              # a question id
title = "昨天的第一件事"
label = "Yesterday's first thing"
intro = "You planned to start with:"
prompt = "Did you start with it?"
report_label = "First things"   # in reports: "First things: 4 written · 3 done"
short = "First"                 # in history: "First done: ✅"

[morning]                   # optional
from = "first"
time = "07:01"
title = "今天的第一件事"
label = "First thing today"
intro = "You planned yesterday:"
outro = "Start here."

[closing]
title = "今日收工"
schedule_button = "first"   # optional: the answer "Add to tomorrow's schedule" saves
model_line = ""             # optional instruction for one line from the model; empty = none
# fallback = "..."          # the line if the model fails

[reports]                   # optional
prepare = "04:00"           # make reports off-peak (only for periods already over)
heading = "Work log"        # Markdown title
subject = "end-of-workday notes"      # what the model is told it's reading
# closing = "one short, warm sentence for the end of the {period}."
week  = { day = "fri", time = "19:30", period = "week_to_date", title = "工作週報" }
month = { time = "07:30", title = "工作月報" }
# year  = { time = "07:30", title = "..." }

[[reports.section]]         # one block per model-written section
name = "Done this week"
from = ["done"]             # question ids it reads
items = true                # a bullet list (else one sentence)
ask = "the 3-5 most significant things finished or moved forward, each a short line."
```

Report periods:

| `period` | Covers | Made off-peak? |
| --- | --- | --- |
| `last_7_days` (week default) | the 7 days up to yesterday | yes |
| `week_to_date` | Monday to the report day, including today's check-in | no -- made at send time |
| `previous_month` (month) | the calendar month before | yes |
| `previous_year` (year) | the calendar year before | yes |

A report's window is from its time to 4 hours later (or midnight for times
from 20:00), so a restart never sends a stale one.

## Pairing with `/cron`

A check-in covers the questions and their own reports. Other parts of a
routine are `/cron` jobs using existing commands. The work log on the O6,
for example:

```
/cron add daily 07:00 工作｜今天與未來3天 :: /mem upcoming
/cron add weekly fri 19:35 本週完成 :: /mem list done since:7d
/cron add weekly fri 19:40 本週文件 :: /rag digest week
```

`/mem list done since:7d` lists items marked done in the last 7 days
(`since:YYYY-MM-DD` also works; for active items it's when they were added).
