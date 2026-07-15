import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot


def test_build_application_enables_local_mode():
    application = bot.build_application("123456:test-token", "http://telegram-bot-api:8081")

    assert application.bot.local_mode is True
    assert application.bot.base_url == "http://telegram-bot-api:8081/bot123456:test-token"
    assert application.bot.base_file_url == "http://telegram-bot-api:8081/file/bot123456:test-token"


def test_configure_logging_suppresses_http_clients_and_redacts_token():
    token = "123456:secret-test-token"
    script = f"""
import logging
import bot

token = {token!r}
bot.configure_logging(token)
logging.getLogger("httpx").info("routine-httpx-request https://api/bot%s/getMe", token)
logging.getLogger("httpcore").info("routine-httpcore-request https://api/bot%s/getMe", token)
logging.getLogger("application-test").error("application-error https://api/bot%s/getMe", token)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(bot.__file__).parent,
        check=False,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert token not in output
    assert "routine-httpx-request" not in output
    assert "routine-httpcore-request" not in output
    assert "application-error https://api/bot[REDACTED]/getMe" in output
