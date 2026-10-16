import dataclasses
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import openclaw_telegram_gateway as gateway
from openclaw_runtime.itinerary import (
    Plan, PlanSuggestions, detect_plans, mem_command, mentions_date, none_reason, parse_plans, render_card,
)

TODAY = date(2026, 10, 11)
TICKET = "Outbound Journey: CAMBRIDGE TO MANCHESTER PICCADILLY\nTravel on Tuesday 13 October 2026"


class FakeLlm:
    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.prompts = []

    def chat_json(self, prompt, schema, *, schema_name, max_tokens=None, persona=True):
        self.prompts.append((prompt, persona))
        return json.dumps(self.reply)


class ParsePlansTest(unittest.TestCase):
    def test_keeps_valid_upcoming_plans_soonest_first(self) -> None:
        data = {"is_plan": True, "plans": [
            {"date": "2026-10-14", "title": "Hotel: Premier Inn Manchester"},
            {"date": "2026-10-13", "title": "Train Cambridge -> Manchester  09:15"},
            {"date": "2026-10-13", "title": "Train Cambridge -> Manchester 09:15"},
            {"date": "2026-10-01", "title": "Already gone"},
            {"date": "13 Oct", "title": "Bad date"},
            {"date": "2026-10-20", "title": "  "},
        ]}
        self.assertEqual(parse_plans(data, TODAY), [
            Plan(date(2026, 10, 13), "Train Cambridge -> Manchester 09:15"),
            Plan(date(2026, 10, 14), "Hotel: Premier Inn Manchester"),
        ])

    def test_not_a_plan(self) -> None:
        self.assertEqual(parse_plans({"is_plan": False, "plans": [{"date": "2026-10-13", "title": "x"}]}, TODAY), [])
        self.assertEqual(parse_plans("nonsense", TODAY), [])

    def test_at_most_three(self) -> None:
        data = {"is_plan": True, "plans": [{"date": f"2026-10-{day}", "title": f"Day {day}"} for day in range(12, 18)]}
        self.assertEqual(len(parse_plans(data, TODAY)), 3)


# A screenshot that cut the outbound date off: only the return is dated.
CUT_TICKET = """10:00 | 12:08 | CrossCountry
Cambridge | Nuneaton
Return Journey: MANCHESTER PICCADILLY TO CAMBRIDGE
Travel on Thursday 15 October 2026
10:34 | 11:42 | Avanti West Coast"""


class MentionsDateTest(unittest.TestCase):
    def test_written_forms(self) -> None:
        day = date(2026, 10, 15)
        for text in ["Travel on Thursday 15 October 2026", "Thu 15 Oct", "Oct. 15", "October 15th",
                     "15th of October", "2026-10-15", "15/10/2026", "10/15", "10月15日"]:
            self.assertTrue(mentions_date(text, day), text)
        for text in ["16 October 2026", "115 Oct", "10:15", "Oct 2015", "5 October", "13:15 15:10"]:
            self.assertFalse(mentions_date(text, day), text)
        self.assertTrue(mentions_date("Sept 5", date(2026, 9, 5)))

    def test_a_date_the_text_never_gives_is_dropped(self) -> None:
        data = {"is_plan": True, "plans": [
            {"date": "2026-10-12", "title": "Train Cambridge -> Manchester Piccadilly 10:00"},
            {"date": "2026-10-15", "title": "Train Manchester Piccadilly -> Cambridge 10:34"},
        ]}
        self.assertEqual(parse_plans(data, date(2026, 10, 12), CUT_TICKET),
                         [Plan(date(2026, 10, 15), "Train Manchester Piccadilly -> Cambridge 10:34")])
        self.assertEqual(len(parse_plans(data, date(2026, 10, 12))), 2)
        only_guess = {"is_plan": True, "plans": [data["plans"][0]]}
        self.assertEqual(parse_plans(only_guess, date(2026, 10, 12), CUT_TICKET), [])
        self.assertEqual(none_reason(only_guess, date(2026, 10, 12), CUT_TICKET), "not_in_text")


class NoneReasonTest(unittest.TestCase):
    def test_reasons(self) -> None:
        self.assertEqual(none_reason({"is_plan": False, "plans": []}, TODAY), "not_a_plan")
        self.assertEqual(none_reason("nonsense", TODAY), "not_a_plan")
        self.assertEqual(none_reason({"is_plan": True, "plans": []}, TODAY), "no_dates")
        past = {"is_plan": True, "plans": [{"date": "2026-10-01", "title": "Train"},
                                           {"date": "2026-10-10", "title": "Hotel"}]}
        self.assertEqual(none_reason(past, TODAY), "past")
        self.assertEqual(none_reason({"is_plan": True, "plans": [{"date": "13 Oct", "title": "Train"}]}, TODAY),
                         "invalid")


class DetectPlansTest(unittest.TestCase):
    def test_asks_without_the_persona_and_with_today(self) -> None:
        llm = FakeLlm({"is_plan": True, "plans": [{"date": "2026-10-13", "title": "Train Cambridge -> Manchester"}]})
        plans = detect_plans(llm, TICKET, TODAY)
        self.assertEqual(plans, [Plan(date(2026, 10, 13), "Train Cambridge -> Manchester")])
        prompt, persona = llm.prompts[0]
        self.assertFalse(persona)
        self.assertIn("Today is 2026-10-11", prompt)
        self.assertIn("CAMBRIDGE TO MANCHESTER", prompt)
        self.assertIn("If the text gives no date for a part", prompt)

    def test_no_text_no_call(self) -> None:
        llm = FakeLlm({})
        self.assertEqual(detect_plans(llm, "  ", TODAY), [])
        self.assertEqual(llm.prompts, [])

    def test_mem_command_and_card(self) -> None:
        plan = Plan(date(2026, 10, 13), "Train <A> -> B")
        self.assertEqual(mem_command(plan), "/mem Train <A> -> B due:2026-10-13 tag:plan")
        card = render_card([plan], "Added.")
        self.assertIn("【加入行程？】", card)
        self.assertIn("Tue 13 Oct", card)
        self.assertIn("Train &lt;A&gt; -&gt; B", card)
        self.assertIn("<i>Added.</i>", card)


class PlanSuggestionsTest(unittest.TestCase):
    def test_round_trip_and_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PlanSuggestions(Path(tmp) / "plans.json")
            old = store.add(1, [Plan(date(2026, 10, 13), "Old")])
            data = json.loads(store.path.read_text(encoding="utf-8"))
            data[old]["created_at"] = 0
            store.path.write_text(json.dumps(data), encoding="utf-8")
            new = store.add(1, [Plan(date(2026, 10, 13), "New")])
            self.assertIsNone(store.get(old))
            self.assertEqual(store.get(new)["status"], "offered")
            store.set_status(new, "added")
            self.assertEqual(store.get(new)["status"], "added")


class FakeQdrant:
    def __init__(self) -> None:
        self.points = []

    def scroll_by_filters(self, collection, filters, limit=64, **_):
        return [{"payload": p} for c, p in self.points
                if c == collection and all(p.get(k) == v for k, v in filters.items())]

    def upsert_text(self, collection, text, vector, metadata, point_id=None):
        self.points.append((collection, {"text": text, **metadata}))


class FakeEmbeddings:
    def embed(self, text):
        return [0.0]


class GatewayPlanButtonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = dataclasses.replace(
            gateway.settings, plan_button_enabled=True, plan_schedule_collection="family_tracker",
            plan_state_path=Path(self.tmp.name) / "plans.json", cron_timezone="UTC",
        )
        self.cards, self.edits = [], []
        self.qdrant = FakeQdrant()
        self.patches = [
            patch.object(gateway, "settings", self.settings),
            patch.object(gateway, "PLAN_SUGGESTIONS", PlanSuggestions(self.settings.plan_state_path)),
            patch.object(gateway, "local_today", lambda tz: TODAY),
            patch.object(gateway, "send_card_with_buttons",
                         lambda chat_id, html, rows: self.cards.append((chat_id, html, rows)) or 99),
            patch.object(gateway, "_edit_card", lambda chat_id, message_id, html: self.edits.append(html)),
            patch.object(gateway, "qdrant", self.qdrant),
            patch.object(gateway, "embeddings", FakeEmbeddings()),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def offer(self, reply: dict, text: str = TICKET) -> None:
        with patch.object(gateway, "llm", FakeLlm(reply)):
            gateway.offer_plan_button(7, text)

    def tap(self, action: str, chat_id: int = 7) -> list[str]:
        toasts = []
        data = self.cards[-1][2][0][0 if action == "add" else 1]["callback_data"]
        gateway.handle_plan_callback(chat_id, 99, data, lambda text="": toasts.append(text))
        return toasts

    def test_plan_is_offered_and_added_only_on_tap(self) -> None:
        self.offer({"is_plan": True, "plans": [{"date": "2026-10-13", "title": "Train Cambridge -> Manchester"}]})
        self.assertEqual(len(self.cards), 1)
        self.assertEqual([b["text"] for b in self.cards[0][2][0]], ["Add to schedule", "No thanks"])
        self.assertEqual(self.qdrant.points, [])

        self.assertEqual(self.tap("add"), ["Added to the schedule"])
        collection, payload = self.qdrant.points[0]
        self.assertEqual(collection, "family_tracker")
        self.assertEqual((payload["text"], payload["due"], payload["tags"]),
                         ("Train Cambridge -> Manchester", "2026-10-13", ["plan"]))
        self.assertIn("Added to the schedule", self.edits[-1])
        self.assertEqual(self.tap("add"), ["Already answered."])
        self.assertEqual(len(self.qdrant.points), 1)

    def test_same_plan_twice_is_not_duplicated(self) -> None:
        reply = {"is_plan": True, "plans": [{"date": "2026-10-13", "title": "Train Cambridge -> Manchester"}]}
        self.offer(reply)
        self.tap("add")
        self.offer(reply)
        self.assertEqual(self.tap("add"), ["Already on the schedule"])
        self.assertEqual(len(self.qdrant.points), 1)

    def test_no_thanks_adds_nothing(self) -> None:
        self.offer({"is_plan": True, "plans": [{"date": "2026-10-13", "title": "Train"}]})
        self.tap("no")
        self.assertEqual(self.qdrant.points, [])
        self.assertIn("Not added.", self.edits[-1])

    def test_another_chat_cannot_answer(self) -> None:
        self.offer({"is_plan": True, "plans": [{"date": "2026-10-13", "title": "Train"}]})
        self.assertEqual(self.tap("add", chat_id=8), ["This suggestion has expired."])
        self.assertEqual(self.qdrant.points, [])

    def test_no_button_when_not_a_plan_off_or_model_fails(self) -> None:
        self.offer({"is_plan": False, "plans": []})
        with patch.object(gateway, "settings", dataclasses.replace(self.settings, plan_button_enabled=False)):
            self.offer({"is_plan": True, "plans": [{"date": "2026-10-13", "title": "Train"}]})

        class BrokenLlm:
            def chat_json(self, *args, **kwargs):
                raise RuntimeError("model down")

        with patch.object(gateway, "llm", BrokenLlm()):
            gateway.offer_plan_button(7, TICKET)
        self.assertEqual(self.cards, [])

    def test_why_no_button_is_logged(self) -> None:
        logs = []
        with patch.object(gateway, "log", logs.append):
            self.offer({"is_plan": True, "plans": [{"date": "2026-10-01", "title": "Train"},
                                                   {"date": "2026-10-02", "title": "Hotel"}]})
            self.offer({"is_plan": False, "plans": []})
        self.assertEqual(self.cards, [])
        self.assertEqual(logs, ["[plan] none reason=past plans=2", "[plan] none reason=not_a_plan plans=0"])


if __name__ == "__main__":
    unittest.main()
