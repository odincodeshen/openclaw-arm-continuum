import tempfile
import time
import unittest
from pathlib import Path

from openclaw_runtime.conversation_memory import ConversationMemory

from tests.support import build_settings


def _memory(tmp: Path, **overrides) -> ConversationMemory:
    kwargs = dict(
        conversation_memory_enabled=True,
        conversation_store_path=tmp / "conversations",
        conversation_history_turns=3,
        conversation_context_chars=6000,
        conversation_retention_hours=72,
    )
    kwargs.update(overrides)
    return ConversationMemory(build_settings(**kwargs))


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


if __name__ == "__main__":
    unittest.main()
