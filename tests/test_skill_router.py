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


def make_router(skills: list) -> SkillRouter:
    router = SkillRouter.__new__(SkillRouter)
    router.settings = None
    router.llm = FakeLlm()
    router.config = {"skills": {}}
    router.skills = skills
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


if __name__ == "__main__":
    unittest.main()
