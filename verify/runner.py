#!/usr/bin/env python3
"""Run one verification scenario inside the sandbox container (bin/verify).

The real gateway code runs in-process against this machine's real model,
embedding model, Qdrant, Whisper and TTS, but:

- Telegram is fake: every outgoing message is recorded, files come from
  verify/fixtures/, and api.telegram.org resolves to 127.0.0.1 in the
  sandbox, so nothing can reach Telegram;
- the workspace is a throwaway tmpfs, and the Qdrant collections carry this
  run's prefix and are deleted at the end;
- no profile is mounted: no personal settings, data or chat IDs.

    python /src/verify/runner.py /src/verify/scenarios/<name>.yaml --prefix verify_<run>_
    python /src/verify/runner.py --sweep --prefix verify_   # delete stale collections

Prints one JSON line with the result as the last line of output.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import traceback
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "verify" / "fixtures"
PRESETS = REPO / "app" / "openclaw_runtime" / "checkin_presets"
OWNER = 900000001  # a made-up chat ID; the only "user" the sandbox knows
SAFE_TOKEN = "0:verify-sandbox"
CJK = re.compile(r"[㐀-鿿]")


class Skip(Exception):
    pass


class StepFailed(AssertionError):
    pass


# --- before importing the gateway ---------------------------------------------
def prepare_environment(spec: dict, prefix: str) -> None:
    token = os.environ.get("OPENCLAW_TELEGRAM_BOT_TOKEN", "")
    if token and token != SAFE_TOKEN:
        raise SystemExit("refusing to run: a real Telegram bot token is set in the sandbox")
    os.environ.update({
        "OPENCLAW_TELEGRAM_BOT_TOKEN": SAFE_TOKEN,
        "OPENCLAW_TELEGRAM_ALLOWED_CHAT_IDS": str(OWNER),
        "OPENCLAW_CRON_CHAT_IDS": str(OWNER),
        "OPENCLAW_ALERT_CHAT_IDS": "",
        "OPENCLAW_CHECKIN_OWNER": str(OWNER),
        "OPENCLAW_NIGHT_RITUAL_OWNER": str(OWNER),
        "OPENCLAW_ENGLISH_BOT_OWNERS": str(OWNER),
        "OPENCLAW_RUNTIME_LABEL": "verify sandbox",
        "OPENCLAW_TRACKER_COLLECTION": f"{prefix}tracker",
        "OPENCLAW_KNOWLEDGE_COLLECTION": f"{prefix}knowledge",
        "OPENCLAW_CATEGORY_COLLECTION_PREFIX": f"{prefix}cat_",
        "OPENCLAW_PENDING_STATE_PATH": "/workspace/.openclaw/pending.json",
        "OPENCLAW_PREPARE_WAIT_MINUTES": "0",  # the scenario clock drives schedules; never sleep for real
    })
    for key, value in (spec.get("env") or {}).items():
        if not str(key).startswith("OPENCLAW_") or any(word in key for word in ("TOKEN", "CHAT_IDS", "OWNER")):
            raise SystemExit(f"scenario env may not set {key}")
        os.environ[str(key)] = str(value)


# --- the fake Telegram ----------------------------------------------------------
class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.files: dict[str, Path] = {}
        self.next_id = 1000

    def call(self, method: str, payload: dict | None = None, timeout: int = 60) -> dict:
        payload = payload or {}
        if method in ("sendMessage", "editMessageText", "sendAudio", "sendDocument", "sendPhoto",
                      "editMessageReplyMarkup"):
            if payload.get("chat_id") not in (None, OWNER, str(OWNER)):
                raise AssertionError(f"a message to an unknown chat: {payload.get('chat_id')}")
            self.next_id += 1
            buttons = [{"text": b.get("text", ""), "data": b.get("callback_data", "")}
                       for row in (payload.get("reply_markup") or {}).get("inline_keyboard") or [] for b in row]
            self.sent.append({"method": method, "id": payload.get("message_id") or self.next_id,
                              "text": str(payload.get("text") or payload.get("caption") or ""), "buttons": buttons})
            return {"ok": True, "result": {"message_id": self.next_id}}
        if method == "getFile":
            path = self.files.get(payload.get("file_id", ""))
            return {"ok": True, "result": {"file_path": path.name if path else ""}}
        if method == "getMe":
            return {"ok": True, "result": {"username": "verify_sandbox_bot"}}
        return {"ok": True, "result": True}

    def download(self, file_id: str, destination: Path) -> tuple[Path, int]:
        source = self.files[file_id]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return destination, destination.stat().st_size


class InlineThread:
    """Runs a background task at once, so each step's replies are complete
    when the step ends."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        if self.target:
            self.target(*self.args, **self.kwargs)

    def join(self, timeout=None):
        return None

    def is_alive(self):
        return False


# --- the scenario ----------------------------------------------------------------
class Harness:
    def __init__(self, gateway, spec: dict) -> None:
        self.gw = gateway
        self.spec = spec
        self.tg = FakeTelegram()
        self.logs: list[str] = []
        self.tz = ZoneInfo(spec.get("timezone", "Europe/London"))
        self.now = datetime.now(self.tz)
        self.states: dict[str, dict] = {}
        gw = gateway
        gw.telegram = self.tg.call
        gw.download_telegram_file = self.tg.download
        gw.send_audio_file = lambda chat_id, path, caption="": self.tg.sent.append(
            {"method": "sendAudio", "id": 0, "text": caption, "buttons": []})
        gw.threading.Thread = InlineThread
        # the scenario's tick steps drive check-in schedules; never start the real loops
        gw.start_checkin_loop = lambda runtime: None
        original_log = gw.log

        def log(message: str) -> None:
            self.logs.append(message)
            original_log(message)

        gw.log = log

    # setup ------------------------------------------------------------------
    def setup(self) -> None:
        gw = self.gw
        gw.qdrant.ensure_collections()
        gw.settings.checkin_dir.mkdir(parents=True, exist_ok=True)
        for name in self.spec.get("checkins") or []:
            shutil.copyfile(PRESETS / f"{name}.toml", gw.settings.checkin_dir / f"{name}.toml")
        for name, text in (self.spec.get("files") or {}).items():  # e.g. checkins/holidays.txt
            target = Path("/workspace") / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(text), encoding="utf-8")
        gw.CHECKIN_RUNTIMES[:] = gw.load_checkin_runtimes()
        if self.spec.get("clock"):
            self.set_clock(self.spec["clock"])

    def set_clock(self, value: str) -> None:
        self.now = datetime.fromisoformat(str(value)).replace(tzinfo=self.tz)

    def runtimes(self):
        for runtime in self.gw.active_checkins():
            runtime.now_fn = lambda: self.now
            yield runtime

    # steps --------------------------------------------------------------------
    def message(self, **fields) -> None:
        self.gw.handle_message({"chat": {"id": OWNER}, "message_id": self.tg.next_id + 1, **fields})

    def register_file(self, name: str) -> str:
        path = FIXTURES / name
        if not path.exists():
            raise StepFailed(f"fixture {name} not found")
        file_id = f"fixture-{len(self.tg.files)}-{path.name}"
        self.tg.files[file_id] = path
        return file_id

    @staticmethod
    def fill(text: str) -> str:
        """{today} / {today+N} / {today-N}: real dates, for commands such as
        /mem due: that read the real calendar rather than the scenario clock."""
        def repl(m):
            return (date.today() + timedelta(days=int(m.group(1) or 0))).isoformat()
        return re.sub(r"\{today([+-]\d+)?\}", repl, text)

    def do(self, step: dict) -> None:
        gw = self.gw
        list(self.runtimes())  # every check-in sees the scenario clock
        if "clock" in step:
            self.set_clock(step["clock"])
        if "say" in step:
            self.message(text=self.fill(str(step["say"])))
        if "upload" in step:
            item = step["upload"] if isinstance(step["upload"], dict) else {"file": step["upload"]}
            file_id = self.register_file(item["file"])
            self.message(document={"file_id": file_id, "file_name": Path(item["file"]).name},
                         caption=item.get("caption", ""))
        if "photo" in step:
            item = step["photo"] if isinstance(step["photo"], dict) else {"file": step["photo"]}
            file_id = self.register_file(item["file"])
            self.message(photo=[{"file_id": file_id, "width": 1200, "height": 800}], caption=item.get("caption", ""))
        if "inbox" in step:  # a file the memory watcher would find
            item = step["inbox"]
            target = gw.settings.inbox_path / item.get("to", "knowledge") / Path(item["file"]).name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(FIXTURES / item["file"], target)
        if step.get("ingest"):
            from openclaw_runtime.file_ingest import InboxIngestor
            InboxIngestor(gw.settings, gw.embeddings, gw.qdrant).scan_once()
        if "tap" in step:
            self.tap(str(step["tap"]))
        if step.get("tick"):
            for runtime in self.runtimes():
                state = self.states.setdefault(runtime.spec.id, {})
                runtime.tick(self.now, state)

    def tap(self, wanted: str) -> None:
        for message in reversed(self.tg.sent):
            for button in message["buttons"]:
                if wanted.lower() in button["text"].lower() or wanted in button["data"]:
                    self.gw.handle_callback_query({"id": "verify", "data": button["data"],
                                                   "message": {"message_id": message["id"], "chat": {"id": OWNER}}})
                    return
        raise StepFailed(f"no button matching {wanted!r}")

    # checks ---------------------------------------------------------------------
    def check(self, expect: dict, replies: list[dict]) -> None:
        text = "\n".join(r["text"] for r in replies)
        low = text.lower()
        buttons = [b for r in replies for b in r["buttons"]]

        def fail(why: str) -> None:
            shown = text[-600:] if text else "(no reply)"
            raise StepFailed(f"{why}\n--- replies ---\n{shown}")

        if expect.get("none") and replies:
            fail("expected no reply")
        if "count" in expect and len(replies) != int(expect["count"]):
            fail(f"expected {expect['count']} replies, got {len(replies)}")
        for item in expect.get("contains") or []:
            if str(item).lower() not in low:
                fail(f"missing {item!r}")
        if expect.get("contains_any") and not any(str(i).lower() in low for i in expect["contains_any"]):
            fail(f"none of {expect['contains_any']!r}")
        for item in expect.get("not_contains") or []:
            if str(item).lower() in low:
                fail(f"should not contain {item!r}")
        if expect.get("matches") and not re.search(expect["matches"], text, re.S):
            fail(f"no match for {expect['matches']!r}")
        for item in expect.get("buttons") or []:
            if not any(str(item).lower() in b["text"].lower() or str(item) in b["data"] for b in buttons):
                fail(f"no button {item!r}")
        if expect.get("language"):
            letters = [c for c in text if c.isalpha()]
            share = sum(1 for c in letters if CJK.match(c)) / (len(letters) or 1)
            if (expect["language"] == "zh") != (share > 0.3):
                fail(f"reply is not in {expect['language']}")
        if expect.get("status"):
            want = expect["status"]
            runtime = next((rt for rt in self.gw.active_checkins() if rt.spec.id == want["checkin"]), None)
            if runtime is None:
                fail(f"no check-in {want['checkin']}")
            entry = runtime.store_fn().load(str(OWNER), date.fromisoformat(str(want["date"])))
            got = (entry or {}).get("status")
            if got != want.get("status"):
                fail(f"{want['checkin']} {want['date']} is {got!r}, expected {want.get('status')!r}")
            if "answered" in want and len([v for v in (entry or {}).get("answers", {}).values() if v]) != want["answered"]:
                fail(f"{want['checkin']} {want['date']} has the wrong number of answers")

    def run(self) -> dict:
        started = time.time()
        steps = self.spec.get("steps") or []
        self.setup()
        for index, step in enumerate(steps, start=1):
            before = len(self.tg.sent)
            try:
                self.do(step)
                if step.get("expect"):
                    self.check(step["expect"], self.tg.sent[before:])
            except StepFailed as exc:
                return self.result("fail", started, index, str(exc), step)
            except Exception:  # noqa: BLE001 - a crash in a step is a failure of that step
                return self.result("fail", started, index, traceback.format_exc()[-1500:], step)
        errors = [line for line in self.logs if "Traceback" in line or "loop error" in line]
        if errors:
            return self.result("fail", started, len(steps), "errors in the log:\n" + "\n".join(errors)[-1500:], {})
        return self.result("pass", started, len(steps), "", {})

    def result(self, status: str, started: float, step: int, reason: str, detail: dict) -> dict:
        return {"name": self.spec.get("name"), "status": status, "step": step,
                "step_detail": {k: v for k, v in detail.items() if k != "expect"} if detail else {},
                "reason": reason, "seconds": round(time.time() - started, 1), "replies": len(self.tg.sent)}


# --- services ------------------------------------------------------------------
def available(settings) -> set[str]:
    from openclaw_runtime.http_client import is_reachable
    found = set()
    checks = {
        "model": settings.vllm_base_url.rstrip("/") + "/models",
        "embeddings": settings.ollama_base_url.rstrip("/") + "/api/tags",
        "qdrant": settings.qdrant_base_url.rstrip("/") + "/collections",
        "whisper": settings.whisper_base_url.rstrip("/") + "/health",
        "tts": settings.tts_base_url.rstrip("/") + "/health",
    }
    for name, url in checks.items():
        if is_reachable(url, timeout=5):
            found.add(name)
    if settings.vision_enabled and is_reachable(settings.vlm_base_url.rstrip("/") + "/models", timeout=5):
        found.add("vision")
    return found


def drop_collections(qdrant_url: str, prefix: str, older_than: float = 0) -> int:
    base = qdrant_url.rstrip("/")
    names = [c["name"] for c in json.loads(urllib.request.urlopen(f"{base}/collections", timeout=10).read())
             ["result"]["collections"]]
    dropped = 0
    for name in names:
        if not name.startswith(prefix):
            continue
        stamp = re.match(r"verify_(\d{10})_", name)
        if older_than and (not stamp or time.time() - int(stamp.group(1)) < older_than):
            continue
        request = urllib.request.Request(f"{base}/collections/{name}", method="DELETE")
        urllib.request.urlopen(request, timeout=30).read()
        dropped += 1
    return dropped


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", nargs="?")
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--sweep", action="store_true", help="delete verify collections over an hour old")
    args = parser.parse_args(argv)
    if not args.prefix.startswith("verify_"):
        raise SystemExit("the prefix must start with verify_")
    sys.path.insert(0, str(REPO / "app"))
    os.environ.setdefault("OPENCLAW_MODEL_CATALOG", str(REPO / "app" / "models.json"))
    if args.sweep:
        url = os.environ.get("OPENCLAW_QDRANT_BASE_URL", "http://host.docker.internal:6333")
        print(json.dumps({"swept": drop_collections(url, "verify_", older_than=3600)}))
        return 0
    spec = yaml.safe_load(Path(args.scenario).read_text(encoding="utf-8"))
    prepare_environment(spec, args.prefix)
    import openclaw_telegram_gateway as gateway

    missing = sorted(set(spec.get("requires") or []) - available(gateway.settings))
    if missing:
        print(json.dumps({"name": spec.get("name"), "status": "skip", "reason": f"not available here: {', '.join(missing)}",
                          "step": 0, "seconds": 0, "replies": 0}))
        return 0
    result = {}
    try:  # one attempt per sandbox: bin/verify retries in a fresh one ("retries:" in the scenario)
        result = Harness(gateway, spec).run()
    finally:
        drop_collections(gateway.settings.qdrant_base_url, args.prefix)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
