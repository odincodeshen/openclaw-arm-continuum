# Embedding models: choosing one and switching

Every passage, note and word a bot stores in Qdrant carries a vector from
the embedding model (`OPENCLAW_EMBEDDING_MODEL`, served by Ollama). Questions
are embedded with the same model and matched against those vectors. So:

- the model decides how well `/rag` finds the right passage, above all for
  a Chinese question about English notes or the reverse;
- every vector in a collection must come from the same model. Switching
  models means re-embedding everything the bot has stored.

## Which model (evaluated October 2026)

The candidates were run through `bin/verify gold --env ...` and
`bin/verify standard --env ...`. That is the fixed made-up gold set and
every feature scenario, in throwaway sandboxes (`verify/README.md`). The
bots and their data were not touched.

| Model | Size | GB10 retrieval / answers | O6 retrieval / answers | Embed time GB10 / O6 | Notes |
| --- | --- | --- | --- | --- | --- |
| `nomic-embed-text` (default until now) | 768 | 73% / 73% | 71% / 67% | 18 / 129 ms | English-centred: cross-language answers 41% / 32% |
| **`qwen3-embedding:0.6b`** | 1024 | **100% / 100%** | **100% / 90%** | 74 / 441 ms | multilingual, long passages, Apache 2.0; all 16 scenarios pass on both |
| `embeddinggemma` | 768 | 100% / 100% | -- | 84 / 669 ms | as good on the GB10, slower on both, Gemma licence terms |
| `nomic-embed-text-v2-moe` | 768 | 98% / 98% | -- | 111 ms / -- | reads only ~512 tokens: long Chinese passages are cut |
| `bge-m3` | 1024 | indexing failed | -- | 114 ms / -- | Ollama returned NaN for some texts |

On the O6, every miss with `qwen3-embedding:0.6b` was ERNIE answering wrong
with the right passage in front of it. The cost of the switch:

- each `/rag` question spends about 0.3 s more embedding (out of ~20 s);
- indexing a 100-passage document takes about 44 s instead of 13 s, in the
  background.

Both machines' bots use `qwen3-embedding:0.6b` since 2026-10-07. On
bot2's real documents, the right passage reached the model for 90% of
questions (87% before) and 77% of Chinese questions (70% before); the
rest of the gain shows on the gold set's cross-language questions.

## Switching a bot

Do one machine first (the GB10), check it, then the next. Pick a quiet time:
not during a check-in, and not 04:00-05:30 when replies are prepared.

1. **Get the model** on the machine's Ollama:
   ```bash
   ollama pull qwen3-embedding:0.6b    # on the O6: OLLAMA_HOST=172.17.0.1:11434 ollama pull ...
   ```
2. **Check it here first** without touching the bots:
   ```bash
   bin/verify standard --env OPENCLAW_EMBEDDING_MODEL=qwen3-embedding:0.6b --env OPENCLAW_EMBEDDING_VECTOR_SIZE=1024
   bin/verify gold     --env OPENCLAW_EMBEDDING_MODEL=qwen3-embedding:0.6b --env OPENCLAW_EMBEDDING_VECTOR_SIZE=1024
   ```
3. **See the plan** for each bot (nothing changes):
   ```bash
   docker exec -i openclaw-telegram-<bot> python3 - --model qwen3-embedding:0.6b --dims 1024 < scripts/qdrant_reembed.py
   ```
4. **Re-embed**:
   ```bash
   docker exec -i openclaw-telegram-<bot> python3 - --model qwen3-embedding:0.6b --dims 1024 --apply < scripts/qdrant_reembed.py
   ```
   For each collection (tracker memory, knowledge base, every category):
   - a Qdrant snapshot;
   - every point's stored text embedded with the new model into a temporary
     copy, with a count check;
   - the collection recreated at 1024 dimensions under the same name;
   - ids, payloads, payload indexes and keyword vectors kept.

   A collection is left as it was if the model fails on any text, or if
   something was saved meanwhile; then run it again. The GB10's four bots
   held 722 points in October 2026: about a minute at ~74 ms each. The O6
   takes about six times as long per point.
5. **Point the bot at the new model**: in `profiles/<bot>/.env`,
   ```
   OPENCLAW_EMBEDDING_MODEL=qwen3-embedding:0.6b
   OPENCLAW_EMBEDDING_VECTOR_SIZE=1024
   ```
   and recreate its containers so the setting is read:
   `bin/openclawctl --profile <bot> start` (`restart` keeps the old one).
   Between steps 4 and 5 the bot still embeds questions with the old model,
   and `/rag` and new notes fail until it restarts. Keep the gap short.
6. **Check the result**:
   ```bash
   bin/verify full                 # scenarios and gold set, now with the new model
   bin/verify gold --accept        # the new model's numbers become this machine's baseline
   ```
   Ask the bot a `/rag` question in each language, too.

A memory watcher that indexed a file while the model was being switched may
have failed on it. Its log says so, and saving the file again re-indexes it.

## Notes on stored data

- **Re-embedding uses each point's stored text.** That is what was embedded
  in the first place everywhere except the English bot's "task pushed" daily
  records. Those are only looked up by tag, never by meaning, so their new
  vectors don't matter.
- **The keyword vectors (`kw`) don't depend on the embedding model.** They
  are kept as they are.
- **New collections** are created at `OPENCLAW_EMBEDDING_VECTOR_SIZE`. Set it
  before a bot's first start.

## Going back

Each collection's snapshot is in Qdrant's snapshot folder, named
`<collection>-<id>-<date>.snapshot`. To return to the old model:

1. stop the bot (`bin/openclawctl --profile <bot> stop`);
2. restore each collection from its snapshot through Qdrant's snapshot API
   (`PUT /collections/<name>/snapshots/recover` with the snapshot's
   location, or upload it);
3. set the old `OPENCLAW_EMBEDDING_MODEL` / `OPENCLAW_EMBEDDING_VECTOR_SIZE`
   back in `.env`, and `bin/openclawctl --profile <bot> start`.

The nightly backup (`docs/RUNTIME_LIFECYCLE.md`) also holds every
collection. Delete the migration snapshots once the new model has run well
for a few days.
