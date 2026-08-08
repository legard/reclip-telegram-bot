import asyncio
import logging
import os
import sys

from telegram.ext import ApplicationBuilder

from cleanup import cleanup_loop
from handlers import allowed_user_filter, register_handlers
from preferences import PreferenceStore

LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
logger = logging.getLogger(__name__)


def parse_allowed_user_ids(value: str | None) -> frozenset[int]:
    values = (value or "").split(",")
    if not value or any(not item.strip().isdigit() or int(item) <= 0 for item in values):
        raise ValueError("ALLOWED_USER_IDS must contain positive integer IDs")
    return frozenset(int(item.strip()) for item in values)


class SecretRedactingFormatter(logging.Formatter):
    def __init__(self, *secrets: str):
        super().__init__(LOG_FORMAT)
        self._secrets = tuple(secret for secret in secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        for secret in self._secrets:
            rendered = rendered.replace(secret, "[REDACTED]")
        return rendered


def configure_logging(bot_token: str | None) -> None:
    logging.basicConfig(level=logging.INFO, force=True)
    formatter = SecretRedactingFormatter(bot_token or "")
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)

    # HTTP clients include full request URLs in routine logs. Telegram request
    # URLs contain the bot token, so keep those libraries above INFO while the
    # formatter remains a second line of defence for warnings and tracebacks.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def wait_for_bot_api(url: str, max_wait: int = 60):
    """Wait for the self-hosted Bot API server to become reachable."""
    import httpx
    for i in range(max_wait):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=2)
                logger.info("Bot API server ready (attempt %d)", i + 1)
                return
        except Exception:
            if i % 5 == 0:
                logger.info("Waiting for Bot API server at %s... (attempt %d)", url, i + 1)
            await asyncio.sleep(1)
    logger.warning("Bot API server not reachable after %ds, starting anyway", max_wait)


def build_application(bot_token: str, api_url: str):
    return (
        ApplicationBuilder()
        .token(bot_token)
        .base_url(f"{api_url}/bot")
        .base_file_url(f"{api_url}/file/bot")
        .local_mode(True)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(30)
        .concurrent_updates(True)
        .build()
    )


def bot_commands():
    from telegram import BotCommand

    return [
        BotCommand("start", "Запустить бота"),
        BotCommand("help", "Помощь и команды"),
        BotCommand("mp3", "Скачать в MP3"),
        BotCommand("mp4", "Скачать MP4 в лучшем качестве"),
        BotCommand("best", "Лучшее доступное качество"),
        BotCommand("platforms", "Поддерживаемые сайты"),
        BotCommand("settings", "Ваши настройки"),
        BotCommand("setquality", "Задать качество по умолчанию"),
        BotCommand("setformat", "Задать формат по умолчанию"),
        BotCommand("stats", "Статистика бота"),
    ]


def main():
    bot_token = os.environ.get("BOT_TOKEN")
    configure_logging(bot_token)
    if not bot_token:
        logger.error("BOT_TOKEN environment variable is required")
        sys.exit(1)

    try:
        allowed_user_ids = parse_allowed_user_ids(os.environ.get("ALLOWED_USER_IDS"))
    except ValueError as error:
        logger.error("%s", error)
        sys.exit(1)

    api_url = os.environ.get("TELEGRAM_BOT_API_URL", "http://telegram-bot-api:8081")
    preference_store = PreferenceStore(os.environ.get("BOT_DB_PATH", "/data/bot.db"))

    logger.info("Starting bot with API server: %s", api_url)

    asyncio.get_event_loop().run_until_complete(wait_for_bot_api(api_url))

    app = build_application(bot_token, api_url)

    register_handlers(app, preference_store, allowed_user_ids)

    async def post_init(application):
        await preference_store.initialize()
        asyncio.create_task(cleanup_loop())
        await application.bot.set_my_commands(bot_commands())
        logger.info("Bot commands registered")

    app.post_init = post_init
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
