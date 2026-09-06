import unittest

from openclaw_runtime.skill_router import SkillRouter
from openclaw_runtime.skills.base import SkillResult


class FakeSkill:
    def __init__(self, name: str, keywords: list[str]) -> None:
        self.name = name
        self.keywords = keywords

    def can_handle(self, text: str) -> bool:
        return any(keyword in text for keyword in self.keywords)

    def run(self, text: str) -> SkillResult:
        return SkillResult(self.name, f"{self.name} handled: {text}")


class FakeLlm:
    def chat(self, text: str, **_: object) -> str:
        return f"llm answer: {text}"


def make_router(skills: list, intent_router=None) -> SkillRouter:
    router = SkillRouter.__new__(SkillRouter)
    router.settings = None
    router.llm = FakeLlm()
    router.model_clients = None
    router.config = {"skills": {}}
    router.skills = skills
    router.intent_router = intent_router
    return router


class SkillRouterExplicitCommandPriorityTest(unittest.TestCase):
    def test_search_command_wins_over_weather_keyword_overlap(self) -> None:
        # Regression guard: "/search <query containing 天氣>" used to get
        # hijacked by WeatherSkill because weather is checked before
        # web_search and both can match on a shared keyword substring.
        weather = FakeSkill("weather", ["天氣", "weather"])
        web_search = FakeSkill("web_search", ["搜尋", "/search"])
        router = make_router([weather, web_search])

        result = router.route("/search 劍橋本日天氣預報")
        self.assertEqual(result.skill_name, "web_search")

    def test_natural_language_weather_query_still_routes_to_weather(self) -> None:
        weather = FakeSkill("weather", ["天氣", "weather"])
        web_search = FakeSkill("web_search", ["搜尋", "/search"])
        router = make_router([weather, web_search])

        result = router.route("英國明天天氣如何")
        self.assertEqual(result.skill_name, "weather")

    def test_mem_command_wins_over_later_skill_keyword_overlap(self) -> None:
        memory = FakeSkill("memory_write", ["/mem", "/remember"])
        weather = FakeSkill("weather", ["天氣"])
        router = make_router([memory, weather])

        result = router.route("/mem 記得今天天氣很好")
        self.assertEqual(result.skill_name, "memory_write")

    def test_colon_style_keyword_is_not_treated_as_explicit_command(self) -> None:
        # "rag:" is not slash-prefixed, so it stays in the normal
        # keyword-scan fallback and does not jump the priority queue.
        weather = FakeSkill("weather", ["天氣"])
        rag = FakeSkill("rag_retrieve", ["rag:"])
        router = make_router([weather, rag])

        result = router.route("今天天氣如何 rag: test")
        self.assertEqual(result.skill_name, "weather")

    def test_falls_back_to_llm_when_nothing_matches(self) -> None:
        weather = FakeSkill("weather", ["天氣"])
        router = make_router([weather])

        result = router.route("random unrelated text")
        self.assertEqual(result.skill_name, "llm")


class FakeIntentRouter:
    def __init__(self, intent):
        self.intent = intent
        self.seen = None

    def classify(self, text):
        self.seen = text
        return self.intent


class SkillRouterIntentFallbackTest(unittest.TestCase):
    def test_explicit_command_never_reaches_intent_router(self) -> None:
        rag = FakeSkill("rag_retrieve", ["/rag"])
        intent = FakeIntentRouter("web_search")
        router = make_router([rag], intent_router=intent)
        result = router.route("/rag what did I save")
        self.assertEqual(result.skill_name, "rag_retrieve")
        self.assertIsNone(intent.seen)

    def test_keyword_match_never_reaches_intent_router(self) -> None:
        weather = FakeSkill("weather", ["天氣"])
        intent = FakeIntentRouter("chat")
        router = make_router([weather], intent_router=intent)
        router.route("明天天氣如何")
        self.assertIsNone(intent.seen)

    def test_intent_knowledge_base_routes_residual_to_rag(self) -> None:
        rag = FakeSkill("rag_retrieve", ["/rag"])
        router = make_router([rag], intent_router=FakeIntentRouter("knowledge_base"))
        result = router.route("我上週存的那份筆記重點是什麼")
        self.assertEqual(result.skill_name, "rag_retrieve")

    def test_intent_chat_falls_through_to_llm(self) -> None:
        rag = FakeSkill("rag_retrieve", ["/rag"])
        router = make_router([rag], intent_router=FakeIntentRouter("chat"))
        result = router.route("跟我聊聊天")
        self.assertEqual(result.skill_name, "llm")

    def test_no_intent_router_keeps_llm_fallback(self) -> None:
        router = make_router([FakeSkill("weather", ["天氣"])], intent_router=None)
        result = router.route("something unroutable")
        self.assertEqual(result.skill_name, "llm")


class SkillRouterExpertModelTest(unittest.TestCase):
    def _router(self, model_clients=None) -> SkillRouter:
        router = SkillRouter.__new__(SkillRouter)
        router.settings = None
        router.llm = FakeLlm()
        router.model_clients = model_clients
        router.config = {"skills": {}}
        router.intent_router = None
        return router

    def test_no_model_policy_uses_default_client(self) -> None:
        router = self._router(model_clients=None)
        self.assertIs(router._client_for({}), router.llm)

    def test_model_policy_resolves_through_catalog(self) -> None:
        class FakeFactory:
            def __init__(self):
                self.asked = None

            def get_or_default(self, policy):
                self.asked = policy
                return f"client:{policy}"

        factory = FakeFactory()
        router = self._router(model_clients=factory)
        self.assertEqual(router._client_for({"model_policy": "local_reasoner"}), "client:local_reasoner")
        self.assertEqual(factory.asked, "local_reasoner")

    def test_model_policy_ignored_without_factory(self) -> None:
        router = self._router(model_clients=None)
        self.assertIs(router._client_for({"model_policy": "local_reasoner"}), router.llm)


if __name__ == "__main__":
    unittest.main()
