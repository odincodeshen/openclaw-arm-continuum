import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from openclaw_runtime.agents.base import Task
from openclaw_runtime.model_client_factory import ModelClientFactory
from openclaw_runtime.skills.base import SkillResult
from openclaw_runtime.task_history import TaskHistory


REVIEW_AGENTS = ("code_review", "architecture_review")
SEVERITIES = {"critical", "high", "medium", "low", "info"}

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "task_type": {"type": "string", "enum": ["engineering_review"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "required_agents": {
            "type": "array",
            "items": {"type": "string", "enum": list(REVIEW_AGENTS)},
            "minItems": 1,
            "maxItems": 2,
        },
        "reason": {"type": "string", "minLength": 1},
    },
    "required": ["task_type", "confidence", "required_agents", "reason"],
    "additionalProperties": False,
}

SPECIALIST_SCHEMA = {
    "type": "object",
    "properties": {
        "agent": {"type": "string", "enum": list(REVIEW_AGENTS)},
        "findings": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": sorted(SEVERITIES)},
                    "category": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1},
                    "evidence": {"type": "string", "minLength": 1},
                    "recommendation": {"type": "string", "minLength": 1},
                },
                "required": ["severity", "category", "title", "evidence", "recommendation"],
                "additionalProperties": False,
            },
        },
        "open_questions": {"type": "array", "maxItems": 5, "items": {"type": "string"}},
        "limitations": {"type": "array", "maxItems": 5, "items": {"type": "string"}},
    },
    "required": ["agent", "findings", "open_questions", "limitations"],
    "additionalProperties": False,
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json_object(text: str) -> dict[str, Any]:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"model returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("model output must be a JSON object")
    return value


@dataclass(frozen=True)
class RouteDecision:
    task_type: str
    confidence: float
    required_agents: tuple[str, ...]
    reason: str

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, threshold: float = 0.65) -> "RouteDecision":
        task_type = value.get("task_type")
        confidence = value.get("confidence")
        agents = value.get("required_agents")
        reason = value.get("reason")
        if task_type != "engineering_review":
            raise ValueError("unsupported task_type")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError("confidence must be a number")
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if confidence < threshold:
            raise ValueError(f"router confidence {confidence:.2f} is below threshold {threshold:.2f}")
        if not isinstance(agents, list) or not agents or len(agents) > len(REVIEW_AGENTS):
            raise ValueError("required_agents must contain one or two bounded specialists")
        normalized = tuple(str(agent) for agent in agents)
        if len(set(normalized)) != len(normalized) or any(agent not in REVIEW_AGENTS for agent in normalized):
            raise ValueError("required_agents contains unknown or duplicate agents")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("route reason is required")
        return cls(task_type, confidence, normalized, reason.strip())


@dataclass(frozen=True)
class Finding:
    severity: str
    category: str
    title: str
    evidence: str
    recommendation: str


@dataclass(frozen=True)
class SpecialistResult:
    agent: str
    status: str
    findings: tuple[Finding, ...]
    open_questions: tuple[str, ...]
    limitations: tuple[str, ...]
    model_policy: str
    endpoint_id: str
    fallback_from: str | None = None
    error: str | None = None


def parse_specialist_result(text: str, expected_agent: str, model_policy: str, endpoint_id: str) -> SpecialistResult:
    value = _json_object(text)
    if value.get("agent") != expected_agent:
        raise ValueError(f"specialist output agent must be {expected_agent}")
    raw_findings = value.get("findings")
    if not isinstance(raw_findings, list):
        raise ValueError("specialist findings must be a list")
    findings = []
    for raw in raw_findings[:8]:
        if not isinstance(raw, dict):
            raise ValueError("each finding must be an object")
        severity = str(raw.get("severity", "")).lower()
        if severity not in SEVERITIES:
            raise ValueError(f"unsupported finding severity: {severity}")
        fields = {name: str(raw.get(name, "")).strip() for name in ("category", "title", "evidence", "recommendation")}
        if not all(fields.values()):
            raise ValueError("finding fields must not be empty")
        findings.append(Finding(severity, fields["category"], fields["title"], fields["evidence"], fields["recommendation"]))
    questions = value.get("open_questions", [])
    limitations = value.get("limitations", [])
    if not isinstance(questions, list) or not isinstance(limitations, list):
        raise ValueError("open_questions and limitations must be lists")
    return SpecialistResult(
        expected_agent,
        "success",
        tuple(findings),
        tuple(str(item) for item in questions[:5]),
        tuple(str(item) for item in limitations[:5]),
        model_policy,
        endpoint_id,
    )


class StructuredEngineeringRouter:
    model_policy = "local_router"

    def __init__(self, clients: ModelClientFactory, confidence_threshold: float = 0.65) -> None:
        self.clients = clients
        self.confidence_threshold = confidence_threshold

    def route(self, request: str) -> RouteDecision:
        prompt = (
            "Classify this request for the bounded private engineering review workflow. "
            "Return one JSON object only, with no Markdown or extra keys. Use exactly this shape: "
            '{"task_type":"engineering_review","confidence":0.90,'
            '"required_agents":["code_review","architecture_review"],"reason":"short explanation"}. '
            "required_agents must be a JSON array, never a string. It may contain only "
            "code_review and architecture_review, at most once each. confidence must be a number from 0 to 1.\n\nRequest:\n"
            + request[:12000]
        )
        client = self.clients.get(self.model_policy)
        try:
            return RouteDecision.from_dict(
                _json_object(client.chat_json(prompt, ROUTE_SCHEMA, schema_name="engineering_route", max_tokens=220)),
                threshold=self.confidence_threshold,
            )
        except Exception:
            fallback = self.clients.fallback_for(self.model_policy)
            if fallback is None:
                raise
            return RouteDecision.from_dict(
                _json_object(fallback.chat_json(prompt, ROUTE_SCHEMA, schema_name="engineering_route", max_tokens=220)),
                threshold=self.confidence_threshold,
            )


class EngineeringReviewAgent:
    name = "engineering_review_agent"
    description = "Run a bounded private code and architecture review with local specialist models."
    model_policy = "bounded_engineering_review"
    capabilities = ("engineering.review",)

    @property
    def endpoint_id(self) -> str:
        return ",".join(self.clients.get(policy).endpoint_id for policy in ("local_router", "local_coder", "local_reasoner"))

    def __init__(self, clients: ModelClientFactory, history: TaskHistory, *, confidence_threshold: float = 0.65) -> None:
        self.clients = clients
        self.history = history
        self.router = StructuredEngineeringRouter(clients, confidence_threshold)

    def can_handle(self, task: Task) -> bool:
        text = task.text.strip().lower()
        if text.startswith("/review"):
            return True
        engineering = any(word in text for word in ("engineering", "architecture", "implementation", "source code", "design package"))
        review = any(word in text for word in ("review", "risk", "readiness", "audit"))
        return engineering and review

    def matches_explicit_command(self, task: Task) -> bool:
        return task.text.strip().lower().startswith("/review")

    def health_check(self) -> str:
        required = ("local_router", "local_coder", "local_reasoner")
        unreachable = [policy for policy in required if not self.clients.get(policy).is_reachable()]
        return "ready" if not unreachable else "error: unreachable " + ", ".join(unreachable)

    def run(self, task: Task) -> SkillResult:
        decision = self.router.route(task.text)
        results = [self._run_specialist(task, agent, decision) for agent in decision.required_agents]
        answer, synthesis_status = self._synthesize(task, decision, results)
        if synthesis_status == "degraded":
            answer = "DEGRADED REVIEW\n\n" + answer
        return SkillResult(self.name, answer, synthesis_status)

    def _run_specialist(self, task: Task, agent: str, decision: RouteDecision) -> SpecialistResult:
        policy = "local_coder" if agent == "code_review" else "local_reasoner"
        prompt = self._specialist_prompt(agent, task.text)
        started = time.time()
        subtask_id = f"subtask_{uuid.uuid4().hex[:10]}"
        client = self.clients.get(policy)
        fallback_from = None
        try:
            try:
                text = client.chat_json(prompt, SPECIALIST_SCHEMA, schema_name=f"{agent}_result", max_tokens=1600)
                result = parse_specialist_result(text, agent, policy, client.endpoint_id)
            except Exception:
                fallback = self.clients.fallback_for(policy)
                if fallback is None:
                    raise
                fallback_from = client.endpoint_id
                client = fallback
                text = client.chat_json(prompt, SPECIALIST_SCHEMA, schema_name=f"{agent}_result", max_tokens=1600)
                result = parse_specialist_result(text, agent, policy, client.endpoint_id)
            if fallback_from:
                result = SpecialistResult(**{**result.__dict__, "fallback_from": fallback_from})
            self._record_subtask(task, subtask_id, result, decision, int((time.time() - started) * 1000))
            return result
        except Exception as exc:
            result = SpecialistResult(agent, "failed", (), (), (), policy, client.endpoint_id, fallback_from, f"{type(exc).__name__}: {exc}")
            self._record_subtask(task, subtask_id, result, decision, int((time.time() - started) * 1000))
            return result

    def _synthesize(self, task: Task, decision: RouteDecision, results: list[SpecialistResult]) -> tuple[str, str]:
        started = time.time()
        subtask_id = f"subtask_{uuid.uuid4().hex[:10]}"
        usable = [result for result in results if result.status == "success"]
        degraded = len(usable) != len(results)
        if not usable:
            errors = "; ".join(f"{result.agent}: {result.error}" for result in results)
            result = SpecialistResult("synthesis", "degraded", (), (), (), "local_reasoner", "not-run", error=errors)
            self._record_subtask(task, subtask_id, result, decision, int((time.time() - started) * 1000))
            return f"No specialist review completed. {errors}", "degraded"
        payload = [
            {
                "agent": result.agent,
                "findings": [finding.__dict__ for finding in result.findings],
                "open_questions": list(result.open_questions),
                "limitations": list(result.limitations),
            }
            for result in usable
        ]
        failed = [{"agent": result.agent, "error": result.error} for result in results if result.status != "success"]
        prompt = (
            "Synthesize the specialist findings below into a concise engineering review with sections: "
            "Critical findings, Recommended changes, Open questions, Missing tests, Release-readiness concerns, "
            "and Limitations. Keep the complete report under 1,000 words, merge duplicates, and use at most "
            "five bullets per section. Preserve the source agent for every finding. Do not invent evidence. "
            "State that AI-assisted review does not replace testing, security review, or human approval."
            f"\nRoute reason: {decision.reason}\nSuccessful results:\n{json.dumps(payload, ensure_ascii=False)}"
            f"\nFailed sections:\n{json.dumps(failed, ensure_ascii=False)}"
        )
        client = self.clients.get("local_reasoner")
        fallback_from = None
        try:
            answer = client.chat(prompt, max_tokens=1400)
        except Exception:
            fallback = self.clients.fallback_for("local_reasoner")
            if fallback:
                fallback_from = client.endpoint_id
                client = fallback
                answer = client.chat(prompt, max_tokens=1400)
            else:
                raise
        status = "degraded" if degraded else "success"
        result = SpecialistResult("synthesis", status, (), (), (), "local_reasoner", client.endpoint_id, fallback_from)
        self._record_subtask(task, subtask_id, result, decision, int((time.time() - started) * 1000))
        return answer, status

    def _record_subtask(self, task: Task, subtask_id: str, result: SpecialistResult, decision: RouteDecision, duration_ms: int) -> None:
        entry = {
            "task_id": task.task_id,
            "parent_task_id": task.task_id,
            "subtask_id": subtask_id,
            "task_type": decision.task_type,
            "route_reason": decision.reason,
            "route_confidence": decision.confidence,
            "agent": result.agent,
            "model_policy": result.model_policy,
            "endpoint_id": result.endpoint_id,
            "status": result.status,
            "ended_at": _now(),
            "duration_ms": duration_ms,
            "finding_count": len(result.findings),
        }
        if result.fallback_from:
            entry["fallback_from"] = result.fallback_from
        if result.error:
            entry["error"] = result.error[:300]
        self.history.append(entry)

    @staticmethod
    def _specialist_prompt(agent: str, request: str) -> str:
        focus = (
            "Review source code, APIs, error handling, and tests."
            if agent == "code_review"
            else "Review architecture boundaries, dependencies, failure modes, and deployment risks."
        )
        return (
            f"You are the {agent} specialist. {focus} Return JSON only with agent, findings, "
            "open_questions, and limitations. Each finding requires severity, category, title, evidence, "
            "and recommendation. Use only evidence present in the request. Return at most 8 concise findings, "
            "5 concise open questions, and 5 concise limitations.\n\nRequest:\n"
            + request[:24000]
        )
