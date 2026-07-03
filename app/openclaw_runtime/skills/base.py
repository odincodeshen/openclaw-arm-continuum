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

