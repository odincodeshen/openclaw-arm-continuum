"""Refuse to run the unit tests where a real Telegram bot token is set.

Inside a bot container the gateway would pick the token up, and a test that
forgets to patch telegram() would reach the real Telegram API with it. Run
the tests in a clean environment instead: bin/verify quick, CI, or a venv.
"""

import os
import re

import pytest

_REAL_TOKEN = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")


def pytest_configure(config):
    token = os.environ.get("OPENCLAW_TELEGRAM_BOT_TOKEN", "").strip()
    if _REAL_TOKEN.match(token):
        pytest.exit("A real Telegram bot token is set (OPENCLAW_TELEGRAM_BOT_TOKEN); the unit tests won't run "
                    "here. Use bin/verify quick or a clean environment.", returncode=2)
