import tempfile
import time
import unittest
from pathlib import Path

from openclaw_runtime.conversation_memory import ConversationMemory

from tests.support import build_settings


def _memory(tmp: Path, llm=None, **overrides) -> ConversationMemory:
    kwargs = dict(
        conversation_memory_enabled=True,
        conversation_store_path=tmp / "conversations",
        conversation_history_turns=3,
        conversation_context_chars=6000,
        conversation_retention_hours=72,
        conversation_summary_enabled=True,
        conversation_summary_max_tokens=200,
        conversation_summary_max_chars=2000,
        conversation_keep_max_items=20,
    )
    kwargs.update(overrides)
    return ConversationMemory(build_settings(**kwargs), llm)


class FakeSummarizerLlm:
    """Returns a canned/derived summary and records every prompt it saw."""

    def __init__(self, answer: str | None = None, raises: Exception | None = None):
        self.answer = answer
        self.raises = raises
        self.prompts: list[str] = []

    def chat(self, prompt, *, max_tokens=None, history=None):
        self.prompts.append(prompt)
        if self.raises:
            raise self.raises
        if self.answer is not None:
            return self.answer
        return f"summary#{len(self.prompts)}"


class ConversationMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_record_then_load_roundtrip(self) -> None:
        mem = _memory(self.root)
        self.assertIsNone(mem.load(42))
        mem.record(42, "hi", "hello there")
        self.assertEqual(
            mem.load(42),
            [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello there"}],
        )

    def test_chats_are_isolated(self) -> None:
        mem = _memory(self.root)
        mem.record(1, "one", "a1")
        mem.record(2, "two", "a2")
        self.assertEqual(mem.load(1)[0]["content"], "one")
        self.assertEqual(mem.load(2)[0]["content"], "two")

    def test_window_keeps_only_recent_turns(self) -> None:
        mem = _memory(self.root, conversation_history_turns=2)
        for i in range(5):
            mem.record(7, f"q{i}", f"a{i}")
        loaded = mem.load(7)
        self.assertEqual([t["content"] for t in loaded], ["q3", "a3", "q4", "a4"])

    def test_char_budget_trims_from_the_front_in_pairs(self) -> None:
        mem = _memory(self.root, conversation_history_turns=5, conversation_context_chars=40)
        mem.record(3, "x" * 30, "y" * 30)
        mem.record(3, "short q", "short a")
        loaded = mem.load(3)
        self.assertEqual([t["content"] for t in loaded], ["short q", "short a"])

    def test_clear_removes_history(self) -> None:
        mem = _memory(self.root)
        mem.record(9, "hi", "yo")
        self.assertTrue(mem.clear(9))
        self.assertIsNone(mem.load(9))
        self.assertFalse(mem.clear(9))

    def test_disabled_is_a_noop(self) -> None:
        mem = _memory(self.root, conversation_memory_enabled=False)
        mem.record(1, "hi", "yo")
        self.assertIsNone(mem.load(1))
        self.assertFalse((self.root / "conversations").exists())

    def test_expired_history_is_not_replayed(self) -> None:
        import json

        mem = _memory(self.root, conversation_retention_hours=1)
        mem.record(5, "old", "answer")
        path = self.root / "conversations" / "5.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["updated_at"] = int(time.time()) - 7200
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(mem.load(5))

    def test_empty_turn_is_not_recorded(self) -> None:
        mem = _memory(self.root)
        mem.record(1, "hi", "")
        mem.record(1, "", "yo")
        self.assertIsNone(mem.load(1))

    def test_sweep_deletes_expired_files(self) -> None:
        mem = _memory(self.root, conversation_retention_hours=1)
        mem.record(1, "a", "b")
        mem.record(2, "c", "d")
        old = self.root / "conversations" / "1.json"
        import os

        os.utime(old, (0, 0))
        self.assertEqual(mem.sweep(), 1)
        self.assertFalse(old.exists())
        self.assertTrue((self.root / "conversations" / "2.json").exists())

    def test_corrupt_file_is_treated_as_empty(self) -> None:
        mem = _memory(self.root)
        store = self.root / "conversations"
        store.mkdir(parents=True)
        (store / "1.json").write_text("{ not json", encoding="utf-8")
        self.assertIsNone(mem.load(1))
        mem.record(1, "hi", "yo")
        self.assertEqual(mem.load(1)[0]["content"], "hi")


class ChatAgentMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mem = _memory(Path(self.tmp.name))

    def test_chat_agent_feeds_history_and_records(self) -> None:
        from openclaw_runtime.agents.base import Task
        from openclaw_runtime.agents.skill_agents import ChatAgent

        calls = []

        class FakeLlm:
            endpoint_id = "local_default"

            def chat(self, text, *, max_tokens=None, history=None):
                calls.append(history)
                return f"reply to {text}"

            def is_reachable(self):
                return True

        agent = ChatAgent(FakeLlm(), self.mem)
        agent.run(Task(task_id="t1", source="telegram_text", text="first", chat_id=5))
        agent.run(Task(task_id="t2", source="telegram_text", text="second", chat_id=5))

        self.assertIsNone(calls[0])
        self.assertEqual(
            calls[1],
            [{"role": "user", "content": "first"}, {"role": "assistant", "content": "reply to first"}],
        )

    def test_chat_agent_without_memory_is_unchanged(self) -> None:
        from openclaw_runtime.agents.base import Task
        from openclaw_runtime.agents.skill_agents import ChatAgent

        class FakeLlm:
            endpoint_id = "local_default"

            def chat(self, text, *, max_tokens=None, history=None):
                assert history is None
                return "ok"

        result = ChatAgent(FakeLlm()).run(Task(task_id="t", source="s", text="hi", chat_id=1))
        self.assertEqual(result.answer, "ok")


class RollingSummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_overflow_without_llm_drops_silently_like_before(self) -> None:
        mem = _memory(self.root, llm=None, conversation_history_turns=2)
        for i in range(5):
            mem.record(1, f"q{i}", f"a{i}")
        loaded = mem.load(1)
        self.assertEqual([t["content"] for t in loaded], ["q3", "a3", "q4", "a4"])
        data = mem._read(1)
        self.assertEqual(data.get("summary"), "")

    def test_overflow_with_llm_folds_into_summary_and_is_replayed(self) -> None:
        fake = FakeSummarizerLlm(answer="User asked about q0/a0 and q1/a1.")
        mem = _memory(self.root, llm=fake, conversation_history_turns=2)
        for i in range(3):
            mem.record(1, f"q{i}", f"a{i}")

        self.assertEqual(len(fake.prompts), 1)
        self.assertIn("q0", fake.prompts[0])
        self.assertIn("a0", fake.prompts[0])

        loaded = mem.load(1)
        self.assertEqual(loaded[0]["role"], "user")
        self.assertIn("User asked about q0/a0 and q1/a1.", loaded[0]["content"])
        self.assertEqual(loaded[1]["role"], "assistant")
        # the raw q0/a0 pair rolled out of the window
        self.assertNotIn("q0", [t["content"] for t in loaded[2:]])
        self.assertEqual([t["content"] for t in loaded[2:]], ["q1", "a1", "q2", "a2"])

    def test_summary_keeps_updating_across_multiple_overflows(self) -> None:
        fake = FakeSummarizerLlm()
        mem = _memory(self.root, llm=fake, conversation_history_turns=1)
        for i in range(4):
            mem.record(1, f"q{i}", f"a{i}")
        # 3 overflow events (turns 0,1,2 each roll out one at a time once turn 3 arrives)
        self.assertEqual(len(fake.prompts), 3)
        # the most recent summarizer call should have been told about the *previous* summary
        self.assertIn("Existing summary:", fake.prompts[-1])
        data = mem._read(1)
        self.assertTrue(data["summary"].startswith("summary#"))

    def test_summarizer_failure_keeps_old_summary_and_does_not_raise(self) -> None:
        fake = FakeSummarizerLlm(raises=RuntimeError("endpoint down"))
        mem = _memory(self.root, llm=fake, conversation_history_turns=1)
        for i in range(3):
            mem.record(1, f"q{i}", f"a{i}")
        data = mem._read(1)
        self.assertEqual(data["summary"], "")
        # still functions -- the raw window is intact and usable
        loaded = mem.load(1)
        self.assertTrue(loaded)

    def test_summary_is_capped_to_max_chars(self) -> None:
        fake = FakeSummarizerLlm(answer="x" * 5000)
        mem = _memory(self.root, llm=fake, conversation_history_turns=1, conversation_summary_max_chars=50)
        for i in range(3):
            mem.record(1, f"q{i}", f"a{i}")
        data = mem._read(1)
        self.assertEqual(len(data["summary"]), 50)

    def test_no_summary_and_no_pinned_means_no_context_pair(self) -> None:
        mem = _memory(self.root, llm=None, conversation_history_turns=2)
        mem.record(1, "hi", "yo")
        loaded = mem.load(1)
        self.assertEqual(loaded, [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}])


class PinnedFactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_pin_appears_in_load_even_with_no_turns(self) -> None:
        mem = _memory(self.root)
        self.assertIsNone(mem.load(1))
        self.assertTrue(mem.pin(1, "flight home is on the 23rd"))
        loaded = mem.load(1)
        self.assertEqual(loaded[0]["role"], "user")
        self.assertIn("flight home is on the 23rd", loaded[0]["content"])
        self.assertEqual(loaded[1]["role"], "assistant")

    def test_pin_survives_alongside_real_turns(self) -> None:
        mem = _memory(self.root)
        mem.pin(1, "prefers metric units")
        mem.record(1, "hi", "yo")
        loaded = mem.load(1)
        self.assertIn("prefers metric units", loaded[0]["content"])
        self.assertEqual([t["content"] for t in loaded[2:]], ["hi", "yo"])

    def test_pin_is_capped(self) -> None:
        mem = _memory(self.root, conversation_keep_max_items=2)
        mem.pin(1, "fact1")
        mem.pin(1, "fact2")
        mem.pin(1, "fact3")
        data = mem._read(1)
        self.assertEqual(data["pinned"], ["fact2", "fact3"])

    def test_pin_rejects_empty_and_disabled(self) -> None:
        mem = _memory(self.root)
        self.assertFalse(mem.pin(1, "   "))
        disabled = _memory(self.root, conversation_memory_enabled=False)
        self.assertFalse(disabled.pin(1, "fact"))

    def test_clear_wipes_pinned_facts_too(self) -> None:
        mem = _memory(self.root)
        mem.pin(1, "fact")
        self.assertTrue(mem.clear(1))
        self.assertIsNone(mem.load(1))


class BackwardCompatTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_v1_file_with_no_summary_or_pinned_keys_still_loads(self) -> None:
        import json

        mem = _memory(self.root)
        store = self.root / "conversations"
        store.mkdir(parents=True)
        (store / "9.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "turns": [
                        {"role": "user", "content": "old hi", "ts": int(time.time())},
                        {"role": "assistant", "content": "old yo", "ts": int(time.time())},
                    ],
                    "updated_at": int(time.time()),
                }
            ),
            encoding="utf-8",
        )
        loaded = mem.load(9)
        self.assertEqual([t["content"] for t in loaded], ["old hi", "old yo"])
        # writing still works and upgrades the stored version
        mem.record(9, "new q", "new a")
        data = mem._read(9)
        self.assertEqual(data["version"], 2)


if __name__ == "__main__":
    unittest.main()
