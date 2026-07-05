from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SkillResult:
    skill_name: str
    answer: str


class Skill(Protocol):
    name: str

    def can_handle(self, text: str) -> bool:
        ...

    def run(self, text: str) -> SkillResult:
        ...


def has_explicit_command_prefix(keywords, text: str) -> bool:
    # An explicit slash command (e.g. "/search ...") must always win over a
    # keyword-based skill like weather, even if the query text also contains
    # that skill's keyword (e.g. "/search today's weather").
    stripped = text.strip().lower()
    return any(stripped.startswith(keyword) for keyword in keywords if keyword.startswith("/"))

