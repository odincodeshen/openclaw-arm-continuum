import unittest

from openclaw_runtime.agents.base import Task
from openclaw_runtime.agents.registry import AgentRegistry


def make_task(text: str) -> Task:
    return Task(task_id="t1", source="test", text=text)


class FakeSkillAgent:
    def __init__(self, name: str, keywords: list[str]) -> None:
        self.name = name
        self.keywords = keywords

    def can_handle(self, task: Task) -> bool:
        return any(keyword in task.text for keyword in self.keywords)

    def matches_explicit_command(self, task: Task) -> bool:
        stripped = task.text.strip().lower()
        return any(stripped.startswith(keyword) for keyword in self.keywords if keyword.startswith("/"))

    def run(self, task: Task):
        raise NotImplementedError


class FakeChatAgent:
    name = "chat_agent"

    def can_handle(self, task: Task) -> bool:
        return True

    def run(self, task: Task):
        raise NotImplementedError


class AgentRegistryExplicitCommandPriorityTest(unittest.TestCase):
    def test_search_command_wins_over_weather_keyword_overlap(self) -> None:
        # Regression guard: this mirrors the SkillRouter fix, but for the
        # AgentRegistry/TaskDispatcher path used by normal Telegram chat,
        # voice messages, and manual /cron run. A user typing
        # "/search <query mentioning weather>" must not get silently routed
        # to the weather agent.
        weather = FakeSkillAgent("weather_agent", ["天氣", "weather"])
        web_search = FakeSkillAgent("browser_search_agent", ["搜尋", "/search"])
        registry = AgentRegistry([weather, web_search, FakeChatAgent()])

        agent = registry.find(make_task("/search 劍橋本日天氣預報"))
        self.assertEqual(agent.name, "browser_search_agent")

    def test_natural_language_weather_query_still_routes_to_weather(self) -> None:
        weather = FakeSkillAgent("weather_agent", ["天氣", "weather"])
        web_search = FakeSkillAgent("browser_search_agent", ["搜尋", "/search"])
        registry = AgentRegistry([weather, web_search, FakeChatAgent()])

        agent = registry.find(make_task("英國明天天氣如何"))
        self.assertEqual(agent.name, "weather_agent")

    def test_agent_without_matches_explicit_command_falls_back_to_can_handle(self) -> None:
        registry = AgentRegistry([FakeChatAgent()])
        agent = registry.find(make_task("hello"))
        self.assertEqual(agent.name, "chat_agent")

    def test_no_match_raises_lookup_error(self) -> None:
        weather = FakeSkillAgent("weather_agent", ["天氣"])
        registry = AgentRegistry([weather])
        with self.assertRaises(LookupError):
            registry.find(make_task("unrelated text"))


if __name__ == "__main__":
    unittest.main()
