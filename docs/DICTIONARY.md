# Word lookup and word list (optional, per-bot)

An English-learning bot can look words up with `/w` and keep a personal word
list with `/vocab`. It's off by default (`OPENCLAW_DICTIONARY_ENABLED=false`)
and meant for a bot running the daily English coach.

```
/w resilient                           look a word up; it's added to your word list
/w resilient | She's remarkably resilient.
                                       ...and say what it means in that sentence
/vocab                                 your word list, newest first
/vocab rm resilient                    remove a word
/vocab review                          review the saved words due today
/vocab export                          your word list as an Anki import file
```

You don't need `/w`: sending just the word (or `word | sentence`) looks it
up too. A message counts as a lookup when it's 1-4 English words and nothing
else -- letters, apostrophes, hyphens and spaces, no digits or sentence
punctuation -- and isn't an everyday reply like "thanks" or "ok". When
several things are waiting for a reply, a bare word goes to:

1. an open `/vocab review` first (its answers are often single words),
2. then the lookup -- ahead of an open daily English task, whose answers are
   sentences or voice,
3. never while a file is waiting for its category name.

`/w` always works, including while a daily English task is still open:
commands are never treated as the task's answer.

`/vocab export` sends the whole list as a tab-separated `.txt` file ready for
Anki's File > Import: the front is the word and its phonetic, the back the
meaning and the saved sentence (in italics), and each note is tagged
`openclaw_lookup` (a word you looked up) or `openclaw_chunk` (a weekly chunk
moved in on Sunday). The file's header lines set the separator, HTML and the
tag column, so Anki needs no import settings; re-importing updates notes with
the same front instead of duplicating them.

## Where meanings come from

- **Offline dictionary first.** Meanings, phonetics and exam tags come from
  [ECDICT](https://github.com/skywind3000/ECDICT) (MIT licensed), stored as a
  local SQLite file. Looking a word up needs no network and no model.
- **The model only adds context.** One LLM call says which sense the word has
  in the sentence you gave, and writes one example sentence. If that call
  fails, the dictionary meaning is still shown.
- **Words the dictionary doesn't have** (idioms, slang, most multi-word
  phrases) are explained by the model instead, and the card says so:
  *AI explanation — not in the local dictionary*.

## Word list

Every successful lookup is saved to the tracker collection
(`OPENCLAW_TRACKER_COLLECTION`) as an owner-scoped record (`tag: eng_vocab`,
`kind: vocab`), so each person has their own list. Looking the same
word up again doesn't add a duplicate; it raises the word's lookup count,
which `/vocab` shows -- a word you keep looking up is one worth reviewing.

## Spaced-repetition review

Saved words come back for review on a Leitner schedule. A new word is first
due the day after it's looked up. Each correct answer moves it up a box and
further out; a wrong answer sends it back to box 0, due tomorrow:

| Box after a correct answer | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|
| Next review in | 3 days | 7 days | 14 days | 30 days | 60 days |

- `/vocab review` asks up to 5 due words, most overdue first. A word saved
  with the sentence you saw it in becomes a blank in that sentence (with its
  meaning as a hint); otherwise you're asked for the word from its meaning.
- Answer all of them in **one typed message**. Answers are judged by the same
  position-aware LLM judge as the Saturday cloze quiz, and the card shows when
  each word comes back.
- While a review is open, typed replies go to it and voice replies still go
  to the day's English task, so the two never mix. An unanswered review stops
  capturing replies after 60 minutes.
- On Sunday the English bot also adds that week's chunks you haven't
  learned yet (see `docs/ENGLISH_BOT.md`); they're reviewed like looked-up
  words, from your own sentence when you made one, and don't count as a
  lookup.
- When words are due, each daily task card ends with a **Word review** block
  saying how many -- counted per person.

Each record keeps `review_box`, `next_review`, `last_review_at`,
`correct_count` and `wrong_count`.

## Building the dictionary file

ECDICT's Chinese is Simplified; the build step converts it to Traditional
Chinese with Taiwan phrasing (OpenCC `s2twp`), once, so the gateway only needs
Python's built-in `sqlite3`. Run it anywhere OpenCC can be installed -- a
throwaway container is enough:

```bash
curl -LO https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv

docker run --rm -v "$PWD":/repo -v "$PWD/ecdict.csv":/src/ecdict.csv:ro \
  -v "<profile workspace>":/workspace -w /repo python:3.12-slim \
  sh -c 'pip install -q opencc && python scripts/build_dictionary.py /src/ecdict.csv /workspace/dictionary/ecdict.sqlite'
```

`<profile workspace>` is the host directory mounted at `/workspace` in the
bot's containers (`OPENCLAW_HOST_WORKSPACE`). The result is about 770,000
entries and 116 MB. Then set, in that bot's `.env`:

```text
OPENCLAW_DICTIONARY_ENABLED=true
OPENCLAW_DICTIONARY_PATH=/workspace/dictionary/ecdict.sqlite
```

and recreate its Telegram container. `/w` and `/vocab` appear in the bot's
command menu and `/help` only when the feature is enabled.
