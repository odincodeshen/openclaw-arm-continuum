from openclaw_runtime.agents.base import Task
from openclaw_runtime.llm_client import LlmClient
from openclaw_runtime.skills.base import SkillResult, has_explicit_command_prefix


SKILL_AGENT_NAMES = {
    "memory_write": ("memory_agent", "Write user memory into personal_tracker_memory.", "local_embedding"),
    "rag_retrieve": ("rag_agent", "Retrieve local memory and document knowledge.", "local_default"),
    "web_search": ("browser_search_agent", "Search the web with local Playwright scraper.", "local_default"),
    "weather": ("weather_agent", "Fetch weather data and format an answer.", "local_tool"),
}


class SkillAgent:
    def __init__(self, skill) -> None:
        agent_name, description, model_policy = SKILL_AGENT_NAMES.get(
            skill.name,
            (f"{skill.name}_agent", f"Run skill {skill.name}.", "local_default"),
        )
        self.skill = skill
        self.name = agent_name
        self.description = description
        self.model_policy = getattr(skill, "model_policy", None) or model_policy
        self.capabilities = (skill.name,)

    @property
    def endpoint_id(self):
        return getattr(self.skill, "endpoint_id", None)

    def can_handle(self, task: Task) -> bool:
        return self.skill.can_handle(task.text)

    def matches_explicit_command(self, task: Task) -> bool:
        return has_explicit_command_prefix(getattr(self.skill, "keywords", ()), task.text)

    def health_check(self) -> str:
        check = getattr(self.skill, "health_check", None)
        return check() if check else "ready"

    def run(self, task: Task) -> SkillResult:
        result = self.skill.run(task.text)
        return SkillResult(self.name, result.answer)


class ChatAgent:
    name = "chat_agent"
    description = "Answer general messages with the local vLLM model."
    model_policy = "local_default"
    capabilities = ("chat",)

    def __init__(self, llm: LlmClient, memory=None) -> None:
        self.llm = llm
        self.memory = memory

    def can_handle(self, task: Task) -> bool:
        return True

    @property
    def endpoint_id(self) -> str:
        return self.llm.endpoint_id

    def health_check(self) -> str:
        return "ready" if self.llm.is_reachable() else "error: vLLM unreachable"

    def run(self, task: Task) -> SkillResult:
        history = self.memory.load(task.chat_id) if self.memory else None
        answer = self.llm.chat(task.text, history=history)
        if self.memory and answer:
            self.memory.record(task.chat_id, task.text, answer)
        return SkillResult(self.name, answer)
