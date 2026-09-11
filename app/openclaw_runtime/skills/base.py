from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SkillResult:
    skill_name: str
    answer: str
    status: str = "success"
    # A skill sets this when `answer` is a routine "nothing new" result (e.g.
    # a reminder digest with nothing due). Interactive callers (Telegram)
    # still show `answer` as normal; a scheduled caller (cron) can check this
    # to skip pushing a notification nobody needs to see.
    suppress_if_routine: bool = False


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
