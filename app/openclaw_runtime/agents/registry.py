from openclaw_runtime.agents.base import Agent, AgentStatus


class AgentRegistry:
    def __init__(self, agents: list[Agent]) -> None:
        self._agents = list(agents)

    @property
    def agents(self) -> list[Agent]:
        return list(self._agents)

    def find(self, task) -> Agent:
        for agent in self._agents:
            if agent.can_handle(task):
                return agent
        raise LookupError("no agent can handle this task")

    def statuses(self) -> list[AgentStatus]:
        return [
            AgentStatus(
                name=agent.name,
                description=agent.description,
                status=self._health_check(agent),
                model_policy=getattr(agent, "model_policy", "local_default"),
            )
            for agent in self._agents
        ]

    @staticmethod
    def _health_check(agent) -> str:
        check = getattr(agent, "health_check", None)
        if not check:
            return "ready"
        try:
            return check()
        except Exception as exc:
            return f"error: health check failed: {exc}"
