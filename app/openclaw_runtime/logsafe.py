"""Keep chat IDs out of logs.

Logs are read while debugging and pasted into chats and issues; a full
Telegram chat ID there identifies an account. ``redact_ids`` keeps the last
three digits of every ID in a ``chat_id=`` / ``owner=`` / ``owners=`` /
``recipients=`` / ``chat_ids=`` field (enough to tell accounts apart) and
leaves everything else alone.

One line keeps the full ID: ``[telegram] rejected chat_id=<id>`` -- an
account not on the allowlist yet. That line is how a new user finds the ID
to add (docs/TELEGRAM_SETUP.md).
"""

from __future__ import annotations

import re

_FIELD = re.compile(r"\b(chat_ids?|owners?|recipients)=(\[[^\]]*\]|\{[^}]*\}|-?\d+)")
_ID = re.compile(r"-?\d{5,}")


def mask_id(value) -> str:
    text = str(value)
    return f"…{text[-3:]}" if len(text.lstrip("-")) >= 5 else text


def redact_ids(message: str) -> str:
    if "rejected chat_id=" in message:
        return message
    return _FIELD.sub(lambda m: f"{m.group(1)}={_ID.sub(lambda d: mask_id(d.group()), m.group(2))}", message)
