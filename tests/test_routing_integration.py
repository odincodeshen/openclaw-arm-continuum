import unittest
from pathlib import Path

from openclaw_runtime.agents import AgentRegistry
from openclaw_runtime.agents.base import Task
from openclaw_runtime.agents.skill_agents import ChatAgent, SkillAgent
from openclaw_runtime.skill_router import SkillRouter
from openclaw_runtime.skills.memory import MemoryWriteSkill, RagRetrieveSkill
from openclaw_runtime.skills.weather import WeatherSkill
from openclaw_runtime.skills.web_search import WebSearchSkill

from tests.support import build_settings

REPO_SKILLS_JSON = Path(__file__).resolve().parents[1] / "app" / "skills.json"


def build_real_agent_registry(settings) -> AgentRegistry:
    """Wire up skills/agents the same way SkillRouter._load_skills() and
    openclaw_telegram_gateway.py do in production, using the real
    app/skills.json keyword config. Routing decisions (can_handle /
    matches_explicit_command) never touch the network, only run() does, so
    this skips ensure_collections() and passes None for the network clients.
    """
    router = SkillRouter.__new__(SkillRouter)
    router.settings = settings
    router.llm = None
    router.config = router._load_config()
    skills_config = router.config.get("skills", {})

    skills = []
    if settings.memory_enabled:
        memory_config = skills_config.get("memory_write", {})
        rag_config = skills_config.get("rag_retrieve", {})
        if memory_config.get("enabled", True):
            skills.append(MemoryWriteSkill(settings, memory_config, embeddings=None, qdrant=None))
        if rag_config.get("enabled", True):
            skills.append(RagRetrieveSkill(settings, rag_config, embeddings=None, qdrant=None, llm=None))

    if settings.web_enabled:
        weather_config = skills_config.get("weather", {})
        if weather_config.get("enabled", True):
            skills.append(WeatherSkill(settings, weather_config))
        web_search_config = skills_config.get("web_search", {})
        if web_search_config.get("enabled", True):
            skills.append(WebSearchSkill(settings, web_search_config, llm=None))

    return AgentRegistry([SkillAgent(skill) for skill in skills] + [ChatAgent(llm=None)])


# (input text, expected agent name, why this case matters)
ROUTING_MATRIX = [
    ("/search 劍橋本日天氣預報", "browser_search_agent", "explicit /search must beat weather keyword overlap"),
    ("/search fifa 今天的賽程", "browser_search_agent", "explicit /search with a 今天 keyword overlap"),
    ("/search", "browser_search_agent", "bare /search with no query still routes to web_search"),
    ("英國明天天氣如何", "weather_agent", "natural-language weather query with no slash command"),
    ("劍橋本日天氣如何", "weather_agent", "natural-language weather query using 本日 phrasing"),
    ("/mem 記得今天天氣很好", "memory_agent", "explicit /mem must beat weather keyword overlap"),
    ("/remember buy milk", "memory_agent", "explicit /remember routes to memory"),
    ("/rag 今天天氣的紀錄", "rag_agent", "explicit /rag must beat weather keyword overlap"),
    ("mem: buy milk", "memory_agent", "colon-style memory keyword (natural-language fallback)"),
    ("rag: what did I save", "rag_agent", "colon-style rag keyword (natural-language fallback)"),
    ("跟我聊聊你最近好嗎", "chat_agent", "no keyword match falls back to chat agent"),
]


class RealRoutingMatrixTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = build_settings(skills_config_path=REPO_SKILLS_JSON)
        self.registry = build_real_agent_registry(self.settings)

    def test_routing_matrix(self) -> None:
        for text, expected_agent, reason in ROUTING_MATRIX:
            with self.subTest(text=text, reason=reason):
                task = Task(task_id="t", source="test", text=text)
                agent = self.registry.find(task)
                self.assertEqual(agent.name, expected_agent, reason)


if __name__ == "__main__":
    unittest.main()
