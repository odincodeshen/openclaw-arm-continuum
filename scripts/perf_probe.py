#!/usr/bin/env python3
"""Performance probe: how long the model takes on a bot's typical requests.

Runs inside a bot's Telegram container against its real model, embeddings
and Qdrant, like scripts/e2e_run.py. /rag runs on throwaway collections
filled with fixed documents (deleted at the end), so the numbers are
comparable between runs and machines, and the bot's own data is never
touched. No Telegram messages are sent.

    docker exec -i openclaw-telegram-<bot> python3 - < scripts/perf_probe.py
    docker exec -i openclaw-telegram-<bot> python3 - --rounds 3 --label "before cache" < scripts/perf_probe.py
    docker exec -i openclaw-telegram-<bot> python3 - --rag-context-tokens 1200 --rag-passage-tokens 300 < scripts/perf_probe.py

--rag-context-tokens / --rag-passage-tokens try /rag budgets for this run
only, without changing the bot's settings. The /rag answers are checked
for the fact they ask about, so a budget that loses it shows up.

Each request runs --rounds times (default 2): the first round shows a cold
cache, later rounds what prompt caching can reuse. For every model call it
records the prompt size, how much of it came from the cache, and the time
spent reading the prompt and writing the answer -- from llama.cpp's
"timings" or, on vLLM, "usage" (where only token counts are reported).

Prints a Markdown report.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

for candidate in (Path("/app"), Path(__file__).resolve().parent.parent / "app" if "__file__" in globals() else None):
    if candidate and candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from openclaw_runtime import categories, llm_client  # noqa: E402
from openclaw_runtime.categories import category_slug  # noqa: E402
from openclaw_runtime.checkins import closing_line  # noqa: E402
from openclaw_runtime.config import load_settings  # noqa: E402
from openclaw_runtime.embedding_client import EmbeddingClient  # noqa: E402
from openclaw_runtime.file_ingest import InboxIngestor  # noqa: E402
from openclaw_runtime.llm_client import LlmClient  # noqa: E402
from openclaw_runtime.night_ritual import NIGHT  # noqa: E402
from openclaw_runtime.qdrant_client import QdrantClient  # noqa: E402
from openclaw_runtime.skills.memory import RagRetrieveSkill  # noqa: E402

# Fixed documents for /rag: long enough that a query fills the retrieval
# limit with full-size chunks, as real notes and PDFs do.
TOPICS = {
    "power.md": ("power budget", [
        "The edge server's power budget is 65 watts at the wall, measured over a full day of mixed load.",
        "Idle draw is about 14 watts with the display off and both network ports linked at 1 gigabit.",
        "Under sustained inference the board reaches 48 watts; the remaining headroom covers USB storage.",
        "The power supply is rated at 90 watts, so the design keeps a 25 percent margin for spikes.",
        "Battery backup gives 40 minutes at idle and about 12 minutes under full inference load.",
    ]),
    "network.md": ("network layout", [
        "The home network has three VLANs: trusted devices, cameras, and guests.",
        "The inference server sits on the trusted VLAN and is reachable only from the Docker bridge.",
        "Camera traffic never leaves its VLAN; recordings go to a local NAS over SMB.",
        "Guests get internet access only, with client isolation turned on in the access points.",
        "DNS runs locally with a filtering list, and DHCP reservations pin every server's address.",
    ]),
    "travel.md": ("travel plan", [
        "The spring trip starts with two nights in Edinburgh, then a train north to Inverness.",
        "The Inverness leg includes a day on Loch Ness and a half-day walk at Culloden.",
        "Rail tickets are booked as advance singles; the sleeper back south leaves at 20:45.",
        "Accommodation is a mix of a city hotel and a guesthouse with breakfast included.",
        "The budget sets aside a fixed daily amount for food and a separate fund for museum entry.",
    ]),
    "garden.md": ("garden notes", [
        "Tomatoes go in the greenhouse in early May, after the last frost date has passed.",
        "The raised beds rotate each year: legumes, then brassicas, then roots, then potatoes.",
        "Compost is turned every three weeks and is ready after about four months in summer.",
        "Watering is automatic in the greenhouse, with a moisture sensor on the drip line.",
        "Slugs are kept down with copper tape on the bed edges and evening checks after rain.",
    ]),
}


def topic_text(title: str, sentences: list[str], paragraphs: int = 10) -> str:
    """About 6,000 characters on one topic: several paragraphs that reuse
    the facts in a different order, so every chunk reads naturally."""
    out = [f"# Notes on the {title}", ""]
    for i in range(paragraphs):
        rotated = sentences[i % len(sentences):] + sentences[:i % len(sentences)]
        out.append(f"Section {i + 1}. " + " ".join(rotated))
        out.append("")
    return "\n".join(out)


@dataclasses.dataclass
class Call:
    prompt_tokens: int
    cached_tokens: int | None
    prompt_ms: float | None
    output_tokens: int
    output_ms: float | None


class Recorder:
    """Wraps llm_client.request_json to keep the timing fields of every
    chat completion."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._original = llm_client.request_json
        llm_client.request_json = self._request_json

    def _request_json(self, method, url, payload=None, timeout=60):
        response = self._original(method, url, payload, timeout=timeout)
        if url.endswith("/chat/completions") and isinstance(response, dict):
            self.calls.append(self._parse(response))
        return response

    @staticmethod
    def _parse(response: dict) -> Call:
        timings = response.get("timings") or {}
        usage = response.get("usage") or {}
        if timings:  # llama.cpp: prompt_n = tokens evaluated, cache_n = reused
            cached = int(timings.get("cache_n") or 0)
            return Call(
                prompt_tokens=int(timings.get("prompt_n") or 0) + cached,
                cached_tokens=cached,
                prompt_ms=float(timings.get("prompt_ms") or 0),
                output_tokens=int(timings.get("predicted_n") or 0),
                output_ms=float(timings.get("predicted_ms") or 0),
            )
        details = usage.get("prompt_tokens_details") or {}
        return Call(
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            cached_tokens=details.get("cached_tokens"),
            prompt_ms=None,
            output_tokens=int(usage.get("completion_tokens") or 0),
            output_ms=None,
        )

    def take(self) -> list[Call]:
        calls, self.calls = self.calls, []
        return calls


@dataclasses.dataclass
class Result:
    name: str
    round: int
    seconds: float
    calls: list[Call]
    error: str = ""
    answer_ok: bool | None = None


class Probe:
    def __init__(self, overrides: dict | None = None) -> None:
        base = dataclasses.replace(load_settings(), **(overrides or {}))
        self.tmp = tempfile.TemporaryDirectory(prefix="openclaw-perf-")
        root = Path(self.tmp.name)
        inbox = root / "inbox"
        inbox.mkdir()
        self.prefix = f"perf_{uuid.uuid4().hex[:8]}_"
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
        self.recorder = Recorder()
        self.results: list[Result] = []

    # -- setup / teardown -----------------------------------------------
    def seed(self) -> None:
        self.qdrant.ensure_collections()
        knowledge = self.settings.inbox_path / "knowledge"
        knowledge.mkdir(parents=True)
        for name, (title, sentences) in TOPICS.items():
            (knowledge / name).write_text(topic_text(title, sentences), encoding="utf-8")
        slug = category_slug("homelab")
        folder = self.settings.inbox_path / self.settings.category_inbox_dirname / slug
        folder.mkdir(parents=True)
        for name in ("power.md", "network.md"):
            title, sentences = TOPICS[name]
            (folder / name).write_text(topic_text(title, sentences), encoding="utf-8")
            (folder / (name + ".meta.json")).write_text(
                json.dumps({"category": "homelab", "category_slug": slug, "original_file_name": name}),
                encoding="utf-8",
            )
        categories.upsert_registry_entry(self.settings, "homelab")
        InboxIngestor(self.settings, self.embeddings, self.qdrant).scan_once()

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

    # -- requests -------------------------------------------------------
    def rag(self, query: str) -> str:
        return RagRetrieveSkill(self.settings, {}, self.embeddings, self.qdrant, self.llm).run(f"/rag {query}").answer

    def requests(self) -> list[tuple[str, callable, tuple[str, ...]]]:
        history = [
            {"role": "user", "content": "I'm planning a garden for next spring. What should I think about first?"},
            {"role": "assistant", "content": "Start with sunlight, soil and how much time you can give it each "
                                             "week. Then pick a few easy crops and plan where each bed goes."},
        ]
        entry = {"answers": {"wins": "Finished the report; cooked dinner with friends",
                             "better": "Asked for help early", "adjust": "Sleep before midnight",
                             "first": "Plan next week's priorities"}}
        return [
            ("Short chat", lambda: self.llm.chat("Give me one tip for staying focused while working from home.",
                                                 max_tokens=120), ()),
            ("Chat, 2nd turn", lambda: self.llm.chat("Which crops are easiest for a first year?", history=history,
                                                     max_tokens=160), ()),
            # the bot may answer in its reply language
            ("/rag", lambda: self.rag("how long does the battery backup last under full load?"),
             ("12", "十二")),
            ("/rag #category", lambda: self.rag("#homelab which VLAN is the inference server on?"),
             ("trusted", "信任", "可信")),
            ("Check-in closing line", lambda: closing_line(NIGHT, self.llm, entry), ()),
        ]

    def run(self, rounds: int) -> None:
        print("seeding throwaway collections ...", file=sys.stderr)
        started = time.time()
        self.seed()
        print(f"seeded in {time.time() - started:.0f}s", file=sys.stderr)
        self.recorder.take()
        for round_no in range(1, rounds + 1):
            for name, func, expected in self.requests():
                started = time.time()
                error, answer = "", ""
                try:
                    answer = func() or ""
                except Exception as exc:  # noqa: BLE001 - a failed request is reported, not fatal
                    error = f"{type(exc).__name__}: {exc}"
                answer_ok = any(word in answer.lower() for word in expected) if expected and not error else None
                result = Result(name, round_no, time.time() - started, self.recorder.take(), error, answer_ok)
                self.results.append(result)
                print(f"round {round_no}  {name}: {result.seconds:.1f}s  {error}", file=sys.stderr)
                if answer_ok is False:
                    print(f"    answer missed {expected}: {answer[:200]!r}", file=sys.stderr)

    # -- report ---------------------------------------------------------
    def report(self, label: str) -> str:
        s = self.settings
        lines = [
            f"# OpenClaw performance probe -- {s.runtime_label}" + (f" -- {label}" if label else ""),
            "",
            f"{time.strftime('%Y-%m-%d %H:%M %Z')} · model `{self.llm.model}` · context "
            f"{self.llm.context_tokens or 'unset'} · retrieval limit {s.retrieval_limit} · chunk {s.ingest_chunk_chars} chars"
            f" · /rag budget {s.rag_context_tokens or 'none'} (passage {s.rag_passage_tokens or 'whole'})",
            "",
            "| Request | Round | Total | Model calls | Prompt tokens | From cache | Reading prompt | Answer tokens | "
            "Writing answer | Answer right |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for r in self.results:
            prompt = sum(c.prompt_tokens for c in r.calls)
            cached = [c.cached_tokens for c in r.calls if c.cached_tokens is not None]
            read_ms = [c.prompt_ms for c in r.calls if c.prompt_ms is not None]
            write_ms = [c.output_ms for c in r.calls if c.output_ms is not None]
            out = sum(c.output_tokens for c in r.calls)
            cache_cell = f"{sum(cached)} ({100 * sum(cached) / prompt:.0f}%)" if cached and prompt else "n/a"
            read_cell = f"{sum(read_ms) / 1000:.1f}s" if read_ms else "n/a"
            write_cell = f"{sum(write_ms) / 1000:.1f}s" if write_ms else "n/a"
            total = f"{r.seconds:.1f}s" + (f" **{r.error}**" if r.error else "")
            right = {True: "yes", False: "**no**", None: ""}[r.answer_ok]
            lines.append(f"| {r.name} | {r.round} | {total} | {len(r.calls)} | {prompt} | {cache_cell} | "
                         f"{read_cell} | {out} | {write_cell} | {right} |")
        return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--label", default="")
    parser.add_argument("--out")
    parser.add_argument("--rag-context-tokens", type=int)
    parser.add_argument("--rag-passage-tokens", type=int)
    args = parser.parse_args(argv)
    overrides = {key: value for key, value in (("rag_context_tokens", args.rag_context_tokens),
                                               ("rag_passage_tokens", args.rag_passage_tokens)) if value is not None}
    probe = Probe(overrides)
    try:
        probe.run(max(1, args.rounds))
    finally:
        probe.cleanup()
    report = probe.report(args.label)
    print(report)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    return 0 if not any(r.error or r.answer_ok is False for r in probe.results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
