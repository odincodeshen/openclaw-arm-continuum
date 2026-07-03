from dataclasses import dataclass, field
from typing import Any, Protocol

from openclaw_runtime.skills.base import SkillResult


@dataclass(frozen=True)
class Task:
    task_id: str
    source: str
    text: str
    chat_id: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentStatus:
    name: str
    description: str
    status: str = "ready"
    model_policy: str = "local_default"


class Agent(Protocol):
    name: str
    description: str
    model_policy: str

    def can_handle(self, task: Task) -> bool:
        ...

    def run(self, task: Task) -> SkillResult:
        ...
