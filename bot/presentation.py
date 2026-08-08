"""Russian Telegram presentation primitives for download status cards."""

import secrets
import time
import logging


logger = logging.getLogger(__name__)


ERROR_TEXT = {
    "info_timeout": "Не удалось получить информацию. Попробуйте ещё раз.",
    "unavailable": "Сервис временно недоступен. Попробуйте позже.",
    "service_unavailable": "Сервис временно недоступен. Попробуйте позже.",
    "auth_required": "Для этой ссылки требуется авторизация.",
    "network": "Не удалось загрузить файл. Попробуйте ещё раз.",
    "busy": "Сервис занят. Попробуйте позже.",
    "format_unavailable": "Выбранное качество больше недоступно. Выберите другое.",
    "russian_audio_unavailable": "Русская дорожка недоступна для этого видео.",
    "job_timeout": "Загрузка заняла слишком много времени. Попробуйте ещё раз.",
    "file_missing": "Файл не найден. Попробуйте загрузить его ещё раз.",
    "download_failed": "Не удалось загрузить файл. Попробуйте ещё раз.",
    "job_lost": "Загрузка прервана. Отправьте ссылку ещё раз.",
    "upload_failed": "Не удалось отправить файл. Попробуйте ещё раз.",
}

RETRYABLE_CODES = frozenset({
    "info_timeout", "unavailable", "service_unavailable", "network", "busy",
    "job_timeout", "file_missing", "download_failed", "upload_failed",
})
RETRY_TTL_SECONDS = 24 * 60 * 60
_UNSET = object()

TEXT = {
    "russian_fallback": "Русская дорожка недоступна — скачиваем оригинал.",
    "start": "Привет! Я бот ReClip.\n\nОтправьте мне ссылку на видео или аудио — я скачаю файл.\n\n"
    "Поддерживаются YouTube, TikTok, Instagram, Twitter, Reddit и ещё 1000+ сайтов.\n\n"
    "Команды:\n/help — помощь\n/platforms — поддерживаемые сайты\n/settings — ваши настройки\n"
    "/stats — статистика бота\n/mp3 <ссылка> — скачать MP3\n/mp4 <ссылка> — скачать MP4 в лучшем качестве\n",
    "help": "*Как пользоваться ботом:*\n\n1\\. Отправьте ссылку \\(YouTube, TikTok и другие\\)\n"
    "2\\. Выберите формат \\(MP4 или MP3\\)\n3\\. Выберите качество\n4\\. Получите файл в чате\\!\n\n"
    "*Быстрые команды:*\n/mp3 \\<ссылка\\> \\- скачать MP3\n/mp4 \\<ссылка\\> \\- скачать MP4 в лучшем качестве\n"
    "/best \\<ссылка\\> \\- лучшее доступное качество\n\n*Настройки:*\n"
    "/setquality \\<best/1080/720/480/360\\> \\- качество по умолчанию\n"
    "/setformat \\<video/audio\\> \\- формат по умолчанию\n/settings \\- показать настройки\n\n"
    "*Другое:*\n/platforms \\- поддерживаемые сайты\n/stats \\- статистика\n\n"
    "Можно отправить несколько ссылок одним сообщением\\!",
    "platforms": "Поддерживаемые сайты:\n\n",
    "stats": "Статистика ReClip:\n\n  Время работы: {hours}ч {mins}м {secs}с\n"
    "  Загрузок: {downloads}\n  Ошибок: {errors}\n  Файлов в кеше: {files}\n"
    "  Использование диска: {disk:.1f} МБ\n  Активных сессий: {sessions}\n",
    "settings": "{reset}Ваши настройки:\n\nФормат: {format}\nКачество: {quality}\nАудио: {audio}",
    "settings_reset": "Настройки сброшены.\n\n",
    "settings_reset_button": "Сбросить настройки",
    "best": "Лучшее",
    "original": "Оригинал",
    "ru_if_available": "Русское, если доступно",
    "setquality_usage": "Использование: /setquality <best/1080/720/480/360>",
    "invalid_quality": "Недопустимое качество. Варианты: {options}",
    "quality_saved": "Качество по умолчанию: {quality}",
    "setformat_usage": "Использование: /setformat <video/audio>",
    "invalid_format": "Недопустимый формат. Варианты: video/audio или mp4/mp3",
    "format_saved": "Формат по умолчанию: {format}",
    "mp3_usage": "Использование: /mp3 <ссылка>\nИли ответьте на сообщение со ссылкой.",
    "mp4_usage": "Использование: /mp4 <ссылка>\nИли ответьте на сообщение со ссылкой.",
    "mp3_start": "Начинаем загрузку MP3…",
    "mp4_start": "Начинаем загрузку MP4…",
    "retry": "Повторить",
    "cancel": "Отменить",
    "best_quality": "Лучшее качество",
    "back": "Назад",
    "info_loading": "Получаем информацию…",
    "selection_expired": "Время выбора истекло. Отправьте ссылку ещё раз.",
    "retry_expired": "Время повтора истекло. Отправьте ссылку ещё раз.",
    "cancelled": "Отменено.",
    "download_start": "Начинаем загрузку…",
}


def error_text(code: str) -> str:
    """Return safe Russian copy without leaking backend exception details."""
    return ERROR_TEXT.get(code, ERROR_TEXT["download_failed"])


def is_retryable(code: str) -> bool:
    return code in RETRYABLE_CODES


def retry_callback_data(token: str) -> str:
    return f"retry:{token}"


def escape_markdown(text: object) -> str:
    special = r"_*[]()~`>#+-=|{}.!\\"
    return "".join(f"\\{char}" if char in special else char for char in str(text))


def format_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "Неизвестно"
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def format_info(info: dict) -> str:
    title = str(info.get("title") or "Без названия")
    title = title if len(title) <= 800 else title[:799] + "…"
    lines = [
        f"*{escape_markdown(title)}*",
        f"Платформа: {escape_markdown(info.get('extractor') or 'Неизвестно')}",
        f"Длительность: {escape_markdown(format_duration(info.get('duration')))}",
    ]
    if info.get("uploader"):
        lines.append(f"Автор: {escape_markdown(info['uploader'])}")
    return "\n".join(lines)


def format_progress(status: dict) -> str:
    if status.get("stage") == "postprocessing":
        return "Обработка файла…"
    progress = status.get("progress")
    if isinstance(progress, dict) and progress.get("percent") is not None:
        return f"Загрузка: {float(progress['percent']):.0f}%"
    return "Загрузка…"


class StatusCard:
    """One Telegram message, edited in place throughout a download lifecycle."""

    def __init__(self, message, *, query=None):
        self.message = message
        self.query = query
        self._last_text = None
        self._buttons = _UNSET

    @property
    def is_photo(self) -> bool:
        return bool(getattr(self.message, "photo", False))

    async def _edit(self, text: str, **kwargs) -> bool:
        method = None
        call_kwargs = kwargs
        if self.query is not None:
            if self.is_photo:
                method = self.query.edit_message_caption
                call_kwargs = {"caption": text, **kwargs}
            else:
                method = self.query.edit_message_text
                call_kwargs = kwargs
        elif self.is_photo:
            method = self.message.edit_caption
            call_kwargs = {"caption": text, **kwargs}
        else:
            method = self.message.edit_text
        try:
            if self.is_photo:
                await method(**call_kwargs)
            else:
                await method(text, **call_kwargs)
        except TypeError:
            # Small test doubles and older adapters may not accept markup kwargs.
            try:
                if self.query is not None and not self.is_photo:
                    await method(text)
                elif self.is_photo:
                    await method(caption=text)
                else:
                    await method(text)
            except Exception:
                logger.debug("Telegram card edit failed", exc_info=True)
                return False
        except Exception:
            logger.debug("Telegram card edit failed", exc_info=True)
            return False
        return True

    async def show_info(self, text: str, buttons=None, *, photo=None) -> None:
        """Show metadata on this card, upgrading it to a thumbnail card when possible."""
        if photo and self.query is None and hasattr(self.message, "reply_photo"):
            previous = self.message
            try:
                await previous.delete()
            except Exception:
                await self.replace(text, reply_markup=buttons, parse_mode="MarkdownV2")
                return
            try:
                sent = await previous.reply_photo(
                    photo=photo,
                    caption=text,
                    parse_mode="MarkdownV2",
                    reply_markup=buttons,
                )
                self.message = sent
                self._last_text = text
                self._buttons = buttons
                return
            except Exception:
                try:
                    self.message = await previous.reply_text(
                        text, parse_mode="MarkdownV2", reply_markup=buttons,
                    )
                    self._last_text = text
                    self._buttons = buttons
                    return
                except Exception:
                    logger.debug("Telegram thumbnail promotion failed", exc_info=True)
        await self.replace(text, reply_markup=buttons, parse_mode="MarkdownV2")

    async def replace(self, text: str, *, reply_markup=_UNSET, parse_mode=None) -> None:
        if text == self._last_text and reply_markup is _UNSET:
            return
        kwargs = {}
        if reply_markup is not _UNSET:
            kwargs["reply_markup"] = reply_markup
            self._buttons = reply_markup
        if parse_mode is not None:
            kwargs["parse_mode"] = parse_mode
        if await self._edit(text, **kwargs):
            self._last_text = text

    async def set_buttons(self, buttons) -> None:
        if buttons == self._buttons:
            return
        try:
            if self.query is not None:
                await self.query.edit_message_reply_markup(reply_markup=buttons)
            else:
                await self.message.edit_reply_markup(reply_markup=buttons)
        except Exception:
            logger.debug("Telegram card button edit failed", exc_info=True)
            return
        self._buttons = buttons

    async def remove_buttons(self) -> None:
        await self.set_buttons(None)

    async def complete(self) -> None:
        try:
            await self.message.delete()
        except Exception:
            await self.replace("Готово", reply_markup=None)


class RetryStore:
    """Ephemeral, semantic retry intents. Restarting the bot empties this store."""

    def __init__(self, *, ttl_seconds: int = RETRY_TTL_SECONDS, now=time.time):
        self.ttl_seconds = ttl_seconds
        self.now = now
        self._items: dict[str, tuple[float, dict]] = {}

    def _cleanup(self) -> None:
        cutoff = self.now() - self.ttl_seconds
        for token, (created, _) in list(self._items.items()):
            if created <= cutoff:
                del self._items[token]

    def put(self, intent: dict) -> str:
        self._cleanup()
        semantic = {
            key: intent[key]
            for key in ("url", "format", "quality", "audio_mode", "user_id")
            if key in intent
        }
        token = secrets.token_urlsafe(18)
        self._items[token] = (self.now(), semantic)
        return token

    def get(self, token: str) -> dict | None:
        self._cleanup()
        item = self._items.get(token)
        return dict(item[1]) if item is not None else None

    def take(self, token: str) -> dict | None:
        """Atomically consume a retry token so callbacks cannot replay it."""
        self._cleanup()
        item = self._items.pop(token, None)
        return dict(item[1]) if item is not None else None
