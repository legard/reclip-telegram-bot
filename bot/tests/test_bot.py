import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot


def test_build_application_enables_local_mode():
    application = bot.build_application("123456:test-token", "http://telegram-bot-api:8081")

    assert application.bot.local_mode is True
    assert application.bot.base_url == "http://telegram-bot-api:8081/bot123456:test-token"
    assert application.bot.base_file_url == "http://telegram-bot-api:8081/file/bot123456:test-token"
