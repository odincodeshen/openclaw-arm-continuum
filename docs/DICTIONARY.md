# Word lookup and word list (optional, per-bot)

An English-learning bot can look words up with `/w` and keep a personal word
list with `/vocab`. It's off by default (`OPENCLAW_DICTIONARY_ENABLED=false`)
and meant for a bot running the daily English coach.

```
/w resilient                           look a word up; it's added to your word list
/w resilient | She's remarkably resilient.
                                       ...and say what it means in that sentence
/vocab                                 your word list, newest first
/vocab rm resilient                    remove a word (or tap Remove words… under /vocab)
/vocab review                          review the saved words due today
/vocab export                          your word list as an Anki deck (.apkg, with UK / US audio)
/vocab export tsv                      ...or as a plain Anki import file
/vocab quiz                            a button quiz on this week's lookups
/say resilient                         pronunciation practice (/say alone picks a word)
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

`/vocab export tsv` (and `/vocab export` when pronunciation is off -- see
below for the `.apkg` with audio) sends the whole list as a tab-separated
`.txt` file ready for Anki's File > Import: the front is the word and its phonetic, the back the
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
- Or tap **Self-check with buttons** on the review card: one word at a time,
  **Show answer**, then **✅ Remembered** / **❌ Forgot** -- no typing, no
  model call. Each tap moves that word to its next box straight away; a
  summary card closes the review. While a self-check is running, typed
  replies aren't taken as answers.
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

## Anki deck, quiz and pronunciation practice

- **`/vocab export`** sends an Anki package (`.apkg`) when pronunciation is
  on: each note has the word, phonetic, meaning, sentence and UK / US audio
  (MP3, so every Anki app plays it), in an "OpenClaw words" deck, tagged
  `openclaw_lookup` or `openclaw_chunk`. Note ids come from the word, so
  importing a newer export updates the same cards. New words take about a
  second each for their audio. `/vocab export tsv` keeps the plain file.
- **`/vocab quiz`**: up to 5 questions on the words you looked up in the
  last 7 days -- the meaning is shown, you tap the word among four taken from
  your own list. Offered automatically on Sundays. It doesn't change review
  dates.
- **`/say <word>`**: record a voice message saying it; speech recognition
  checks what it heard (every word, in order; longer words may be one letter
  off, since that's the recognizer's spelling, not you). It's a clarity
  check, not a phonetic score. **Try again** and 🔊 are on the result.
- On the `/vocab` card, **🔊 Pronounce…** shows a button per word.

## Pronunciation (🔊)

With `OPENCLAW_TTS_ENABLED=true`, a lookup card has **🔊 UK / 🔊 US**
buttons for the word, and **🔊 UK sentence / 🔊 US sentence** for your
sentence (or the example when you didn't give one). The self-check review
shows the same UK / US buttons once you tap Show answer. A tap sends the
audio as a Telegram voice message.

The audio is synthesized on the host by the shared `openclaw-tts` service
(`app/openclaw_tts_service.py`, Kokoro-82M, Apache-2.0) on CPU -- the GPU
stays with the main model, and nothing is sent to an outside service; the
voice model is downloaded once into the Hugging Face cache. British English
uses the `bf_emma` voice, American `af_heart`
(`OPENCLAW_TTS_VOICE_UK` / `_US` on the service). Each clip is cached by
voice and text under `.cache/tts`, so a word is only synthesized once
(about a second on the GB10's CPU the first time).

It's synthetic speech: reliable for how a word sounds and where the stress
falls, but flatter than a real speaker in sentences (less natural rhythm,
linking and weak forms), and rare names can come out wrong -- check against
the phonetic on the card. Real speech for listening and shadowing still
comes from the BBC clips.

```text
OPENCLAW_TTS_ENABLED=true
OPENCLAW_TTS_BASE_URL=http://openclaw-tts:8766   # default
```

Start the service once with `docker compose up -d --build openclaw-tts`.

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
