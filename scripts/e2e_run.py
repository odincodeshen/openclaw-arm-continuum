#!/usr/bin/env python3
"""L3 end-to-end run: the real pipelines against the real services.

Runs inside a bot's Telegram container, so it uses that bot's real vLLM,
embedding model, Qdrant, Whisper, Gateway and Telegram token -- but only
throwaway collections and a temporary inbox, which are deleted at the end.
It sends no Telegram messages (only a read-only getMe) and never touches
the bot's own memory, categories or schedules.

    docker exec -i openclaw-telegram-<bot> python3 - < scripts/e2e_run.py
    docker exec -i openclaw-telegram-<bot> python3 - --out /workspace/e2e-report.md < scripts/e2e_run.py

Checks: model reply, embeddings, category RAG (ingest -> isolation ->
Sources), knowledge RAG, /mem round trip with /mem upcoming, Whisper and
Gateway reachability, Telegram getMe, and the model-written parts of the
night ritual and Monday's gist options (their output shape, not taste).

Prints a Markdown report. Exit code 0 = all passed, 1 = something failed.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
import time
import traceback
import urllib.request
import uuid
from datetime import date, timedelta
from pathlib import Path

for candidate in (Path("/app"), Path(__file__).resolve().parent.parent / "app" if "__file__" in globals() else None):
    if candidate and candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from openclaw_runtime import categories  # noqa: E402
from openclaw_runtime.categories import category_slug  # noqa: E402
from openclaw_runtime.config import load_settings  # noqa: E402
from openclaw_runtime.embedding_client import EmbeddingClient  # noqa: E402
from openclaw_runtime.file_ingest import InboxIngestor  # noqa: E402
from openclaw_runtime.gateway_cron import list_gateway_jobs  # noqa: E402
from openclaw_runtime.http_client import is_reachable, request_json  # noqa: E402
from openclaw_runtime.llm_client import LlmClient  # noqa: E402
from openclaw_runtime.night_ritual import FALLBACK_CLOSING, closing_line, summarize_period  # noqa: E402
from openclaw_runtime.qdrant_client import QdrantClient  # noqa: E402
from openclaw_runtime.skills.english_bot import generate_gist_options  # noqa: E402
from openclaw_runtime.skills.memory import MemoryWriteSkill, RagRetrieveSkill  # noqa: E402


class CheckFailed(AssertionError):
    pass


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


class Run:
    def __init__(self) -> None:
        base = load_settings()
        self.tmp = tempfile.TemporaryDirectory(prefix="openclaw-e2e-")
        root = Path(self.tmp.name)
        inbox = root / "inbox"
        inbox.mkdir()
        run_id = uuid.uuid4().hex[:8]
        self.prefix = f"e2e_{run_id}_"
        self.settings = dataclasses.replace(
            base,
            inbox_path=inbox,
            watcher_state_path=root / "watcher_state.json",
            knowledge_collection=f"{self.prefix}knowledge",
            tracker_collection=f"{self.prefix}tracker",
            category_collection_prefix=f"{self.prefix}cat_",
            category_registry_path=inbox / ".openclaw" / "categories.json",
            pending_state_path=None,
        )
        self.embeddings = EmbeddingClient(self.settings)
        self.qdrant = QdrantClient(self.settings)
        self.llm = LlmClient(self.settings)
        self.results: list[tuple[str, bool, float, str]] = []

    # -- helpers --------------------------------------------------------
    def check(self, name: str, func) -> None:
        started = time.time()
        try:
            detail = func() or ""
            ok = True
        except CheckFailed as exc:
            ok, detail = False, str(exc)
        except Exception as exc:  # noqa: BLE001 - a crash is a failed check, not a failed run
            ok, detail = False, f"{type(exc).__name__}: {exc}"
            traceback.print_exc(file=sys.stderr)
        self.results.append((name, ok, time.time() - started, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", file=sys.stderr)

    def put_category_doc(self, category: str, filename: str, text: str) -> None:
        slug = category_slug(category)
        folder = self.settings.inbox_path / self.settings.category_inbox_dirname / slug
        folder.mkdir(parents=True, exist_ok=True)
        (folder / filename).write_text(text, encoding="utf-8")
        (folder / (filename + ".meta.json")).write_text(
            json.dumps({"category": category, "category_slug": slug, "original_file_name": filename}),
            encoding="utf-8",
        )
        categories.upsert_registry_entry(self.settings, category)

    def rag(self, query: str) -> str:
        return RagRetrieveSkill(self.settings, {}, self.embeddings, self.qdrant, self.llm).run(f"/rag {query}").answer

    def cleanup(self) -> None:
        base = self.settings.qdrant_base_url.rstrip("/")
        try:
            names = [c["name"] for c in json.loads(urllib.request.urlopen(f"{base}/collections", timeout=5).read())
                     ["result"]["collections"]]
        except Exception:  # noqa: BLE001
            names = []
        for name in names:
            if name.startswith(self.prefix):
                try:
                    self.qdrant.delete_collection(name)
                except Exception:  # noqa: BLE001
                    pass
        self.tmp.cleanup()

    # -- checks ---------------------------------------------------------
    def model_reply(self) -> str:
        reply = self.llm.chat("Reply with exactly the word OK and nothing else.", max_tokens=20)
        expect("ok" in (reply or "").lower(), f"unexpected reply: {reply!r}")
        return self.settings.vllm_model

    def embedding(self) -> str:
        vector = self.embeddings.embed("OpenClaw end-to-end check")
        expect(len(vector) == self.settings.embedding_vector_size,
               f"vector size {len(vector)} != {self.settings.embedding_vector_size}")
        return f"{len(vector)} dims"

    def category_rag(self) -> str:
        self.qdrant.ensure_collections()
        self.put_category_doc("batteries", "cells.md",
                              "Lithium iron phosphate battery cells have a long cycle life and stable thermal "
                              "behaviour under load.")
        self.put_category_doc("taxes", "return.md",
                              "The self assessment tax return filing deadline in the United Kingdom is the "
                              "thirty first of January.")
        InboxIngestor(self.settings, self.embeddings, self.qdrant).scan_once()
        answer = self.rag("#batteries what cell chemistry is described?")
        # the bot may answer in its reply language (e.g. 磷酸鐵鋰)
        expect(any(w in answer.lower() for w in ("phosphate", "磷酸")), "the battery document wasn't used")
        expect("Sources: cells.md" in answer, "no Sources: line for cells.md")
        expect(not any(w in answer.lower() for w in ("january", "一月", "1月")), "the other category leaked in")
        return "ingest, isolation and Sources: ok"

    def knowledge_rag(self) -> str:
        folder = self.settings.inbox_path / "knowledge"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "neoverse.txt").write_text(
            "The Arm Neoverse V2 platform targets high performance per watt for cloud and HPC workloads.",
            encoding="utf-8",
        )
        InboxIngestor(self.settings, self.embeddings, self.qdrant).scan_once()
        answer = self.rag("what does the Neoverse V2 platform target?")
        # the bot may answer in its reply language (e.g. Traditional Chinese)
        expect(any(word in answer.lower() for word in ("performance", "watt", "性能", "效能", "瓦")),
               f"unexpected answer: {answer[:120]!r}")
        expect("neoverse.txt" in answer, "no source for neoverse.txt")
        return "indexed and answered with its source"

    def mem_round_trip(self) -> str:
        skill = MemoryWriteSkill(self.settings, {}, self.embeddings, self.qdrant)
        due = (date.today() + timedelta(days=1)).isoformat()
        skill.run(f"/mem e2e check item due:{due} tag:e2e")
        upcoming = skill.run("/mem upcoming").answer
        expect("e2e check item" in upcoming, "/mem upcoming doesn't list the new item")
        listed = skill.run("/mem list tag:e2e").answer
        expect("e2e check item" in listed, "/mem list tag:e2e doesn't list it")
        return "write, upcoming, list ok"

    def whisper(self) -> str:
        if not self.settings.whisper_enabled:
            return "skipped (Whisper off for this bot)"
        url = self.settings.whisper_base_url.rstrip("/") + "/health"
        expect(is_reachable(url, timeout=5), f"{url} unreachable")
        return "healthy"

    def gateway(self) -> str:
        jobs = list_gateway_jobs(self.settings)
        return f"{len(jobs)} cron job(s) listed"

    def telegram(self) -> str:
        me = request_json("POST", f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/getMe", {},
                          timeout=20)
        expect(bool(me.get("ok")), "getMe failed")
        return "token valid"

    def night_ritual_model(self) -> str:
        entry = {"answers": {"wins": "Fixed a tricky bug; walked in the sun", "better": "Asked before building",
                             "adjust": "Stop work earlier", "first": "Write the spec"}}
        line = closing_line(self.llm, entry)
        expect(line != FALLBACK_CLOSING, "closing line fell back (model call failed)")
        expect(not any("一" <= ch <= "鿿" for ch in line), f"closing line isn't English: {line!r}")
        summary = summarize_period(self.llm, [{"date": "2026-01-01", **entry}], "week")
        expect(bool(summary.sections.get("highlights")), "weekly summary has no highlights (model call failed?)")
        return f"closing: {line[:60]}"

    def gist_options(self) -> str:
        options, answer = generate_gist_options(
            self.llm,
            "I was nineteen when I moved to London with my brother. We had no money and no plan, "
            "but the city changed everything for me -- the music, the people, the late nights.",
            seed=1,
        )
        expect(len(options) == 3 and 0 <= answer < 3, "no three options")
        return f"answer {'ABC'[answer]}"

    def run(self) -> int:
        try:
            for name, func in [
                ("Model reply", self.model_reply),
                ("Embeddings", self.embedding),
                ("Category RAG", self.category_rag),
                ("Knowledge RAG", self.knowledge_rag),
                ("/mem round trip", self.mem_round_trip),
                ("Whisper", self.whisper),
                ("Gateway RPC", self.gateway),
                ("Telegram", self.telegram),
                ("Night ritual (model)", self.night_ritual_model),
                ("Monday gist options (model)", self.gist_options),
            ]:
                self.check(name, func)
        finally:
            self.cleanup()
        return 0 if all(ok for _, ok, _, _ in self.results) else 1

    def report(self) -> str:
        passed = sum(1 for _, ok, _, _ in self.results if ok)
        lines = [
            f"# OpenClaw L3 e2e -- {self.settings.runtime_label}",
            "",
            f"{passed}/{len(self.results)} passed · {time.strftime('%Y-%m-%d %H:%M %Z')}",
            "",
            "| Check | Result | Time | Detail |",
            "| --- | --- | --- | --- |",
        ]
        for name, ok, seconds, detail in self.results:
            lines.append(f"| {name} | {'PASS' if ok else '**FAIL**'} | {seconds:.1f}s | {detail.replace('|', '/')} |")
        return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    out = None
    if "--out" in argv:
        out = Path(argv[argv.index("--out") + 1])
    run = Run()
    code = run.run()
    report = run.report()
    print(report)
    if out:
        out.write_text(report, encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
