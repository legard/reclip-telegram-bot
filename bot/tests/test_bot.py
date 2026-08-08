import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot
import handlers


def test_build_application_enables_local_mode():
    application = bot.build_application("123456:test-token", "http://telegram-bot-api:8081")

    assert application.bot.local_mode is True
    assert application.bot.base_url == "http://telegram-bot-api:8081/bot123456:test-token"
    assert application.bot.base_file_url == "http://telegram-bot-api:8081/file/bot123456:test-token"


def test_build_application_processes_callbacks_while_multi_url_metadata_is_pending():
    application = bot.build_application("123456:test-token", "http://telegram-bot-api:8081")

    assert application.update_processor.max_concurrent_updates > 1


def test_parse_allowed_user_ids_rejects_empty_and_invalid_values():
    with pytest.raises(ValueError):
        bot.parse_allowed_user_ids("")
    with pytest.raises(ValueError):
        bot.parse_allowed_user_ids("12,nope")


def test_bot_command_descriptions_are_russian():
    commands = bot.bot_commands()

    assert [command.command for command in commands] == [
        "start", "help", "mp3", "mp4", "best", "platforms",
        "settings", "setquality", "setformat", "stats",
    ]
    assert all(
        any("а" <= character.lower() <= "я" or character.lower() == "ё"
            for character in command.description)
        for command in commands
    )


def test_allowed_user_filter_matches_only_configured_user_ids():
    allowed = bot.allowed_user_filter(frozenset({12}))

    assert allowed.filter(SimpleNamespace(from_user=SimpleNamespace(id=12))) is True
    assert allowed.filter(SimpleNamespace(from_user=SimpleNamespace(id=99))) is False


@pytest.mark.asyncio
async def test_unauthorized_callback_is_answered_without_handler_side_effect():
    registered = []

    class Application:
        def add_handler(self, handler):
            registered.append(handler)

    class Query:
        def __init__(self):
            self.data = "fmt:7:abcd:audio"
            self.from_user = SimpleNamespace(id=99)
            self.message = SimpleNamespace(chat_id=10, message_id=7, photo=False)
            self.answer_count = 0

        async def answer(self):
            self.answer_count += 1

    handlers._state[handlers._state_key(10, 7, "abcd")] = {"created": 0}
    handlers.register_handlers(Application(), preference_store=None, allowed_user_ids=frozenset({12}))
    callback = next(
        handler.callback
        for handler in registered
        if isinstance(handler, handlers.CallbackQueryHandler) and handler.pattern.pattern == "^fmt:"
    )
    query = Query()

    await callback(SimpleNamespace(callback_query=query, effective_user=query.from_user), None)

    assert query.answer_count == 1
    assert handlers._state[handlers._state_key(10, 7, "abcd")] == {"created": 0}


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
