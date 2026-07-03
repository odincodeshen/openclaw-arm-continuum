import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from openclaw_runtime.agents.base import Task
from openclaw_runtime.agents.registry import AgentRegistry
from openclaw_runtime.skills.base import SkillResult
from openclaw_runtime.task_history import TaskHistory


@dataclass(frozen=True)
class DispatchResult:
    task_id: str
    agent_name: str
    skill_result: SkillResult
    duration_ms: int

    @property
    def answer(self) -> str:
        return self.skill_result.answer


class TaskDispatcher:
    def __init__(self, registry: AgentRegistry, history: TaskHistory) -> None:
        self.registry = registry
        self.history = history

    def dispatch(self, text: str, *, source: str, chat_id: int | None = None, metadata: dict | None = None) -> DispatchResult:
        task = Task(
            task_id=self._new_task_id(),
            source=source,
            chat_id=chat_id,
            text=text,
            metadata=metadata or {},
        )
        started = time.time()
        started_at = self._now()
        agent = self.registry.find(task)
        self.history.append(
            {
                "task_id": task.task_id,
                "source": source,
                "chat_id": chat_id,
                "agent": agent.name,
                "status": "started",
                "started_at": started_at,
                "input_summary": self._summary(text),
                "metadata": task.metadata,
            }
        )
        try:
            result = agent.run(task)
        except Exception as exc:
            duration_ms = int((time.time() - started) * 1000)
            self.history.append(
                {
                    "task_id": task.task_id,
                    "source": source,
                    "chat_id": chat_id,
                    "agent": agent.name,
                    "status": "failed",
                    "started_at": started_at,
                    "ended_at": self._now(),
                    "duration_ms": duration_ms,
                    "input_summary": self._summary(text),
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(limit=8),
                }
            )
            raise
        duration_ms = int((time.time() - started) * 1000)
        self.history.append(
            {
                "task_id": task.task_id,
                "source": source,
                "chat_id": chat_id,
                "agent": agent.name,
                "status": "success",
                "started_at": started_at,
                "ended_at": self._now(),
                "duration_ms": duration_ms,
                "input_summary": self._summary(text),
                "output_summary": self._summary(result.answer),
            }
        )
        return DispatchResult(task.task_id, agent.name, result, duration_ms)

    @staticmethod
    def _new_task_id() -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return f"task_{stamp}_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    @staticmethod
    def _summary(text: str, limit: int = 320) -> str:
        clean = " ".join(str(text or "").split())
        if len(clean) <= limit:
            return clean
        return clean[: limit - 1] + "…"
