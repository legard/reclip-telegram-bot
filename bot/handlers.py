import asyncio
from functools import wraps
import hashlib
import logging
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from reclip_client import (
    ReclipDownloadError,
    ReclipError,
    ReclipInfoError,
    ReclipServiceDown,
    ReclipJobDeadlineExceeded,
    ReclipJobLost,
    ReclipServiceOutage,
    get_info,
    start_download,
    cancel_download,
    wait_for_job,
)
import event_client
from upload import TelegramUploadError, send_local_path
from preferences import PreferenceStore
from presentation import (
    RetryStore,
    StatusCard,
    TEXT,
    error_text,
    format_info,
    format_progress,
    is_retryable,
    retry_callback_data,
)

logger = logging.getLogger(__name__)

DOWNLOADS_PATH = os.environ.get("DOWNLOADS_PATH", "/downloads")
URL_REGEX = re.compile(r"https?://[^\s<>\"']+")
STATE_TTL = 600  # 10 minutes
CAPTION_MAX = 1000  # Telegram caption limit is 1024, leave headroom


def allowed_user_filter(ids: frozenset[int]):
    return filters.User(user_id=ids)


def _truncate_caption(text: str) -> str:
    """Truncate a caption to fit within Telegram's 1024-char limit."""
    if not text:
        return ""
    if len(text) <= CAPTION_MAX:
        return text
    return text[: CAPTION_MAX - 1] + "…"

_state: dict[str, dict] = {}
_retry_store = RetryStore()
_stats = {"downloads": 0, "errors": 0, "started": time.time()}

SUPPORTED_PLATFORMS = [
    "YouTube", "TikTok", "Instagram", "Twitter/X", "Reddit",
    "Facebook", "Vimeo", "Twitch", "Dailymotion", "SoundCloud",
    "Bandcamp", "Bilibili", "Pinterest", "Tumblr", "Threads",
    "LinkedIn", "Loom", "Streamable", "и ещё 1000+ сайтов через yt-dlp",
]


async def load_preferences(
    user_id: int, preference_store: PreferenceStore | None = None,
) -> dict[str, str] | None:
    if preference_store is None:
        return None
    return await preference_store.get(user_id)


async def save_final_selection(
    user_id: int,
    intent: dict,
    preference_store: PreferenceStore | None = None,
) -> None:
    if preference_store is None:
        return
    await preference_store.save(
        user_id,
        format=intent["format"],
        quality=intent["quality"],
        audio_mode=intent["audio_mode"],
    )


def _select_height(formats: list[dict], requested: str) -> int | None:
    if requested == "best":
        return None
    heights = sorted({int(item["height"]) for item in formats if item.get("height")})
    if not heights:
        return None
    target = int(requested)
    return target if target in heights else max(
        (height for height in heights if height <= target), default=min(heights)
    )


def _download_intent(
    format: str, quality: str, audio_mode: str, info: dict,
) -> dict:
    intent = {
        "format": format,
        "format_id": None,
        "audio_language": None,
        "height": None,
    }
    if format == "audio":
        return intent

    formats = info.get("formats", [])
    if audio_mode == "ru_if_available":
        russian_audio = info.get("russian_audio") or {}
        if russian_audio.get("available") and russian_audio.get("formats"):
            intent["audio_language"] = "ru"
            formats = russian_audio["formats"]
        else:
            intent["fallback_note"] = TEXT["russian_fallback"]
    intent["height"] = _select_height(formats, quality)
    return intent


async def _wait_for_download_job(job_id: str, message, entry: dict | None = None):
    """Relay new ReClip progress states without duplicating Telegram edits."""
    card = message if isinstance(message, StatusCard) else StatusCard(message)

    async def on_status(status):
        if status.get("status") != "downloading":
            return

        stage = status.get("stage") or "downloading"
        progress = status.get("progress")
        if entry is None:
            await card.replace(format_progress(status))
        else:
            await card.replace(format_progress(status), reply_markup=_cancel_markup(entry))

        try:
            await event_client.send_progress(
                job_id=job_id,
                percent=progress.get("percent") if isinstance(progress, dict) else None,
                speed=progress.get("speed") if isinstance(progress, dict) else None,
                eta=progress.get("eta") if isinstance(progress, dict) else None,
                downloaded_bytes=progress.get("downloaded_bytes") if isinstance(progress, dict) else None,
                total_bytes=progress.get("total_bytes") if isinstance(progress, dict) else None,
                stage=stage,
            )
        except Exception:
            pass

    return await wait_for_job(job_id, on_status)


def _wait_error_message(error: ReclipError) -> str:
    return getattr(error, "error_code", "download_failed")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(TEXT["start"])


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(TEXT["help"], parse_mode="MarkdownV2")


async def cmd_platforms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = TEXT["platforms"] + "\n".join(f"  {p}" for p in SUPPORTED_PLATFORMS)
    await update.message.reply_text(text)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uptime_s = int(time.time() - _stats["started"])
    hours, remainder = divmod(uptime_s, 3600)
    mins, secs = divmod(remainder, 60)

    downloads_dir = Path(DOWNLOADS_PATH)
    disk_mb = 0
    file_count = 0
    if downloads_dir.exists():
        for f in downloads_dir.iterdir():
            if f.is_file():
                disk_mb += f.stat().st_size / (1024 * 1024)
                file_count += 1

    text = TEXT["stats"].format(
        hours=hours, mins=mins, secs=secs, downloads=_stats["downloads"],
        errors=_stats["errors"], files=file_count, disk=disk_mb, sessions=len(_state),
    )
    await update.message.reply_text(text)


async def cmd_settings(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    preference_store: PreferenceStore | None = None,
):
    uid = update.effective_user.id
    prefs = await load_preferences(uid, preference_store)
    text, keyboard = _settings_card(prefs)
    await update.message.reply_text(text, reply_markup=keyboard)


def _settings_card(prefs: dict[str, str] | None, reset: bool = False):
    prefs = prefs or {"format": "video", "quality": "best", "audio_mode": "original"}
    quality = prefs["quality"]
    fmt = prefs["format"]
    audio_mode = prefs["audio_mode"]
    fmt_label = "MP4" if fmt == "video" else "MP3"
    quality_label = TEXT["best"].lower() if quality == "best" else f"{quality}p"
    audio_label = TEXT["original"].lower() if audio_mode == "original" else TEXT["ru_if_available"].lower()
    text = TEXT["settings"].format(
        reset=TEXT["settings_reset"] if reset else "", format=fmt_label,
        quality=quality_label, audio=audio_label,
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("MP4", callback_data="settings:format:video"),
            InlineKeyboardButton("MP3", callback_data="settings:format:audio"),
        ],
        [
            InlineKeyboardButton(TEXT["best"], callback_data="settings:quality:best"),
            InlineKeyboardButton("1080p", callback_data="settings:quality:1080"),
            InlineKeyboardButton("720p", callback_data="settings:quality:720"),
        ],
        [
            InlineKeyboardButton("480p", callback_data="settings:quality:480"),
            InlineKeyboardButton("360p", callback_data="settings:quality:360"),
        ],
        [
            InlineKeyboardButton(TEXT["original"], callback_data="settings:audio:original"),
            InlineKeyboardButton(TEXT["ru_if_available"], callback_data="settings:audio:ru_if_available"),
        ],
        [InlineKeyboardButton(TEXT["settings_reset_button"], callback_data="settings:reset")],
    ])
    return text, keyboard


async def settings_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    preference_store: PreferenceStore | None = None,
):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    if parts == ["settings", "reset"]:
        if preference_store:
            await preference_store.clear(update.effective_user.id)
        text, keyboard = _settings_card(None, reset=True)
    elif len(parts) == 3 and parts[1] in {"format", "quality", "audio"}:
        field = {"format": "format", "quality": "quality", "audio": "audio_mode"}[parts[1]]
        try:
            if preference_store:
                await preference_store.update(update.effective_user.id, **{field: parts[2]})
        except ValueError:
            return
        text, keyboard = _settings_card(
            await load_preferences(update.effective_user.id, preference_store)
        )
    else:
        return
    await query.edit_message_text(text, reply_markup=keyboard)


async def cmd_setquality(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    preference_store: PreferenceStore | None = None,
):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text(TEXT["setquality_usage"])
        return
    q = {"лучшее": "best"}.get(context.args[0].lower(), context.args[0].lower())
    valid = ["best", "1080", "720", "480", "360"]
    if q not in valid:
        await update.message.reply_text(TEXT["invalid_quality"].format(options=", ".join(valid)))
        return
    if preference_store:
        await preference_store.update(uid, quality=q)
    await update.message.reply_text(TEXT["quality_saved"].format(quality=q))


async def cmd_setformat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    preference_store: PreferenceStore | None = None,
):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text(TEXT["setformat_usage"])
        return
    f = {
        "mp4": "video", "видео": "video", "video": "video",
        "mp3": "audio", "аудио": "audio", "audio": "audio",
    }.get(context.args[0].lower())
    if f not in ("video", "audio"):
        await update.message.reply_text(TEXT["invalid_format"])
        return
    if preference_store:
        await preference_store.update(uid, format=f)
    await update.message.reply_text(TEXT["format_saved"].format(format=f))


def _extract_urls_from_command(update: Update) -> list[str]:
    """Extract URLs from command text or from the replied-to message."""
    text = update.message.text or ""
    urls = URL_REGEX.findall(text)
    if not urls and update.message.reply_to_message:
        reply_text = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
        urls = URL_REGEX.findall(reply_text)
    return urls


async def cmd_mp3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Direct MP3 download without format picker."""
    urls = _extract_urls_from_command(update)
    if not urls:
        await update.message.reply_text(TEXT["mp3_usage"])
        return
    for url in urls:
        msg = await update.message.reply_text(TEXT["mp3_start"])
        await _direct_download(update, msg, url, "audio", None)


async def cmd_mp4(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Direct best-quality MP4 download without format picker."""
    urls = _extract_urls_from_command(update)
    if not urls:
        await update.message.reply_text(TEXT["mp4_usage"])
        return
    for url in urls:
        msg = await update.message.reply_text(TEXT["mp4_start"])
        await _direct_download(update, msg, url, "video", None)


async def cmd_best(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias for /mp4."""
    await cmd_mp4(update, context)


async def _direct_download(update: Update, status_msg, url: str, fmt: str, format_id: str | None):
    """Download without the interactive picker flow."""
    card = StatusCard(status_msg)
    semantic = {
        "url": url, "format": fmt, "quality": "best",
        "audio_mode": "original", "user_id": update.effective_user.id,
    }
    try:
        info = await get_info(url)
    except ReclipError as error:
        await _present_error(card, getattr(error, "error_code", "download_failed"), intent=semantic)
        _stats["errors"] += 1
        return

    entry = {
        "url": url, "info": info, "user_id": update.effective_user.id,
        "created": time.time(), "retry_intent": semantic,
    }
    await download_and_send(
        SimpleNamespace(message=status_msg), entry, format=fmt, format_id=format_id,
    )


def _state_key(chat_id: int, message_id: int, url_hash: str) -> str:
    return f"{chat_id}:{message_id}:{url_hash}"


def _url_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:8]


def _evict_stale():
    now = time.time()
    expired = [
        key for key, entry in _state.items()
        if now - entry["created"] > STATE_TTL
        and not entry.get("selection_started")
        and not entry.get("job_id")
    ]
    for k in expired:
        del _state[k]


def _remove_active_entry(entry: dict) -> None:
    """Forget an active download without removing a newer card at the same key."""
    state_key = entry.get("state_key")
    if state_key and _state.get(state_key) is entry:
        _state.pop(state_key, None)
        return
    for key, active_entry in tuple(_state.items()):
        if active_entry is entry:
            _state.pop(key, None)
            return


def _cancel_markup(entry: dict) -> InlineKeyboardMarkup | None:
    callback_data = entry.get("cancel_callback_data")
    if not callback_data:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(TEXT["cancel"], callback_data=callback_data)
    ]])


def _semantic_quality(info: dict, format_id: str | None, height: int | None) -> str:
    selected_height = height
    if selected_height is None and format_id:
        selected = next(
            (item for item in info.get("formats", []) if str(item.get("id")) == str(format_id)),
            {},
        )
        selected_height = selected.get("height")
    quality = str(selected_height) if selected_height is not None else "best"
    return quality if quality in {"best", "1080", "720", "480", "360"} else "best"


def _retry_intent(entry: dict, format: str, format_id: str | None, audio_language: str | None, height: int | None) -> dict:
    saved = entry.get("retry_intent")
    if saved:
        return dict(saved)
    return {
        "url": entry["url"],
        "format": format,
        "quality": "best" if format == "audio" else _semantic_quality(entry["info"], format_id, height),
        "audio_mode": "ru" if audio_language == "ru" else "original",
        "user_id": entry["user_id"],
    }


def _retry_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(TEXT["retry"], callback_data=retry_callback_data(token))
    ]])


async def _present_error(card: StatusCard, code: str, *, intent: dict | None = None) -> None:
    if intent is not None and is_retryable(code):
        token = _retry_store.put(intent)
        await card.replace(error_text(code), reply_markup=_retry_keyboard(token))
    else:
        await card.replace(error_text(code), reply_markup=None)


def _resolve_retry_intent(semantic: dict, info: dict) -> tuple[dict | None, str | None]:
    if semantic["format"] == "audio":
        return {
            "format": "audio", "format_id": None,
            "audio_language": None, "height": None,
        }, None
    if semantic["audio_mode"] == "ru":
        russian_audio = info.get("russian_audio") or {}
        formats = russian_audio.get("formats", [])
        if not russian_audio.get("available") or not formats:
            return None, "russian_audio_unavailable"
        return {
            "format": "video", "format_id": None,
            "audio_language": "ru", "height": _select_height(formats, semantic["quality"]),
        }, None
    if semantic["format"] == "video":
        formats = info.get("formats", [])
        height = _select_height(formats, semantic["quality"])
        if height is None:
            return {
                "format": "video", "format_id": None,
                "audio_language": None, "height": None,
            }, None
        current = next(
            (item for item in formats if item.get("height") == height and item.get("id")),
            None,
        )
        if current is None:
            return None, "format_unavailable"
        return {
            "format": "video", "format_id": str(current["id"]),
            "audio_language": None, "height": None,
        }, None
    intent = _download_intent(
        semantic["format"], semantic["quality"], semantic["audio_mode"], info,
    )
    note = intent.pop("fallback_note", None)
    if note:
        intent["start_note"] = note
    return intent, None


def _build_format_buttons(
    message_id: int, url_hash: str, russian_audio: dict | None = None
) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton("MP4", callback_data=f"fmt:{message_id}:{url_hash}:video")
    ]
    if (russian_audio or {}).get("available"):
        buttons.append(
            InlineKeyboardButton("MP4 • RU", callback_data=f"fmt:{message_id}:{url_hash}:video_ru")
        )
    buttons.append(InlineKeyboardButton("MP3", callback_data=f"fmt:{message_id}:{url_hash}:audio"))
    return InlineKeyboardMarkup([buttons, [
        InlineKeyboardButton(TEXT["cancel"], callback_data=f"cancel:{message_id}:{url_hash}")
    ]])


def _build_quality_buttons(message_id: int, url_hash: str, formats: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for fmt in formats:
        label = fmt.get("label", fmt.get("id", "?"))
        buttons.append(
            InlineKeyboardButton(label, callback_data=f"qty:{message_id}:{url_hash}:{fmt['id']}")
        )
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton(TEXT["best_quality"], callback_data=f"qty:{message_id}:{url_hash}:best")])
    rows.append([InlineKeyboardButton(TEXT["back"], callback_data=f"fmt:{message_id}:{url_hash}:back")])
    rows.append([InlineKeyboardButton(TEXT["cancel"], callback_data=f"cancel:{message_id}:{url_hash}")])
    return InlineKeyboardMarkup(rows)


def _build_russian_quality_buttons(
    message_id: int, url_hash: str, formats: list[dict]
) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            fmt.get("label", f'{fmt["height"]}p'),
            callback_data=f'ruqty:{message_id}:{url_hash}:{fmt["height"]}',
        )
        for fmt in formats
    ]
    rows = [buttons[index:index + 3] for index in range(0, len(buttons), 3)]
    rows.append([
        InlineKeyboardButton(TEXT["best_quality"], callback_data=f"ruqty:{message_id}:{url_hash}:best")
    ])
    rows.append([InlineKeyboardButton(TEXT["back"], callback_data=f"fmt:{message_id}:{url_hash}:back")])
    rows.append([InlineKeyboardButton(TEXT["cancel"], callback_data=f"cancel:{message_id}:{url_hash}")])
    return InlineKeyboardMarkup(rows)


async def url_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    preference_store: PreferenceStore | None = None,
):
    _evict_stale()
    text = update.message.text or ""
    urls = URL_REGEX.findall(text)
    if not urls:
        return

    for url in urls:
        uhash = _url_hash(url)
        status_msg = await update.message.reply_text(TEXT["info_loading"])
        card = StatusCard(status_msg)
        preferences = await load_preferences(update.effective_user.id, preference_store)
        retry_intent = {
            "url": url,
            "format": (preferences or {}).get("format", "video"),
            "quality": (preferences or {}).get("quality", "best"),
            "audio_mode": (preferences or {}).get("audio_mode", "original"),
            "user_id": update.effective_user.id,
        }

        try:
            info = await get_info(url)
        except ReclipError as error:
            await _present_error(
                card, getattr(error, "error_code", "download_failed"), intent=retry_intent,
            )
            continue

        if preferences:
            entry = {
                "url": url,
                "user_id": update.effective_user.id,
                "info": info,
                "created": time.time(),
                "retry_intent": {
                    "url": url, "format": preferences["format"],
                    "quality": preferences["quality"], "audio_mode": preferences["audio_mode"],
                    "user_id": update.effective_user.id,
                },
            }
            intent = _download_intent(
                preferences["format"], preferences["quality"],
                preferences["audio_mode"], info,
            )
            fallback_note = intent.pop("fallback_note", None)
            if fallback_note:
                intent["start_note"] = fallback_note
            asyncio.create_task(
                download_and_send(SimpleNamespace(message=status_msg), entry, **intent)
            )
            continue

        keyboard = _build_format_buttons(
            status_msg.message_id, uhash, info.get("russian_audio")
        )
        await card.show_info(format_info(info), keyboard, photo=info.get("thumbnail"))
        shown_message = card.message
        key = _state_key(update.effective_chat.id, shown_message.message_id, uhash)
        _state[key] = {
            "url": url,
            "user_id": update.effective_user.id,
            "info": info,
            "message_id": shown_message.message_id,
            "created": time.time(),
        }
        _state[key]["state_key"] = key
        _state[key]["cancel_callback_data"] = f"cancel:{shown_message.message_id}:{uhash}"


async def format_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    preference_store: PreferenceStore | None = None,
):
    _evict_stale()
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 4:
        return
    _, msg_id_str, uhash, fmt = parts
    if not msg_id_str.isdecimal():
        return
    msg_id = int(msg_id_str)

    key = _state_key(query.message.chat_id, msg_id, uhash)
    entry = _state.get(key)
    if not entry:
        key = _state_key(query.message.chat_id, query.message.message_id, uhash)
        entry = _state.get(key)
    if not entry:
        await StatusCard(query.message, query=query).replace(TEXT["selection_expired"])
        return
    if entry.get("selection_started"):
        return

    if fmt == "back":
        keyboard = _build_format_buttons(
            query.message.message_id, uhash, entry["info"].get("russian_audio")
        )
        await StatusCard(query.message, query=query).set_buttons(keyboard)
        return

    if fmt == "audio":
        entry["selection_started"] = True
        entry["retry_intent"] = {
            "url": entry["url"], "format": "audio", "quality": "best",
            "audio_mode": "original", "user_id": entry["user_id"],
        }
        await save_final_selection(
            entry["user_id"],
            {"format": "audio", "quality": "best", "audio_mode": "original"},
            preference_store,
        )
        asyncio.create_task(
            download_and_send(query, entry, format="audio", format_id=None)
        )
    elif fmt == "video":
        formats = entry["info"].get("formats", [])
        if not formats:
            entry["selection_started"] = True
            await save_final_selection(
                entry["user_id"],
                {"format": "video", "quality": "best", "audio_mode": "original"},
                preference_store,
            )
            asyncio.create_task(
                download_and_send(query, entry, format="video", format_id=None)
            )
            return

        keyboard = _build_quality_buttons(query.message.message_id, uhash, formats[:6])
        await StatusCard(query.message, query=query).set_buttons(keyboard)
    elif fmt == "video_ru":
        russian_audio = entry["info"].get("russian_audio") or {}
        formats = russian_audio.get("formats", [])
        if not formats:
            await StatusCard(query.message, query=query).replace(error_text("russian_audio_unavailable"))
            return

        keyboard = _build_russian_quality_buttons(query.message.message_id, uhash, formats)
        await StatusCard(query.message, query=query).set_buttons(keyboard)


async def quality_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    preference_store: PreferenceStore | None = None,
):
    _evict_stale()
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 4:
        return
    _, msg_id_str, uhash, format_id = parts
    if not msg_id_str.isdecimal():
        return
    msg_id = int(msg_id_str)

    key = _state_key(query.message.chat_id, msg_id, uhash)
    entry = _state.get(key)
    if not entry:
        key = _state_key(query.message.chat_id, query.message.message_id, uhash)
        entry = _state.get(key)
    if not entry:
        await StatusCard(query.message, query=query).replace(TEXT["selection_expired"])
        return
    if entry.get("selection_started"):
        return

    fid = None if format_id == "best" else format_id
    entry["selection_started"] = True
    selected = next(
        (item for item in entry["info"].get("formats", []) if str(item.get("id")) == format_id),
        {},
    )
    selected_quality = str(selected.get("height", "best"))
    saved_quality = selected_quality if selected_quality in {"best", "1080", "720", "480", "360"} else "best"
    await save_final_selection(
        entry["user_id"],
        {"format": "video", "quality": saved_quality, "audio_mode": "original"},
        preference_store,
    )
    entry["retry_intent"] = {
        "url": entry["url"], "format": "video", "quality": selected_quality,
        "audio_mode": "original", "user_id": entry["user_id"],
    }
    asyncio.create_task(
        download_and_send(query, entry, format="video", format_id=fid)
    )


async def russian_quality_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    preference_store: PreferenceStore | None = None,
):
    _evict_stale()
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 4:
        return
    _, msg_id_str, uhash, height_value = parts
    if not msg_id_str.isdecimal():
        await StatusCard(query.message, query=query).replace(TEXT["selection_expired"])
        return
    msg_id = int(msg_id_str)

    key = _state_key(query.message.chat_id, msg_id, uhash)
    entry = _state.get(key)
    if not entry:
        key = _state_key(query.message.chat_id, query.message.message_id, uhash)
        entry = _state.get(key)
    if not entry:
        await StatusCard(query.message, query=query).replace(TEXT["selection_expired"])
        return
    if entry.get("selection_started"):
        return

    if height_value == "best":
        height = None
    elif height_value.isdecimal():
        height = int(height_value)
    else:
        await StatusCard(query.message, query=query).replace(TEXT["selection_expired"])
        return

    entry["selection_started"] = True
    await save_final_selection(
        entry["user_id"],
        {
            "format": "video",
            "quality": (
                str(height) if str(height) in {"1080", "720", "480", "360"} else "best"
            ),
            "audio_mode": "ru_if_available",
        },
        preference_store,
    )
    entry["retry_intent"] = {
        "url": entry["url"], "format": "video",
        "quality": str(height) if height is not None else "best",
        "audio_mode": "ru", "user_id": entry["user_id"],
    }
    asyncio.create_task(
        download_and_send(
            query,
            entry,
            format="video",
            format_id=None,
            audio_language="ru",
            height=height,
        )
    )


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _evict_stale()
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 3 or not parts[1].isdecimal():
        return
    _, msg_id_str, uhash = parts
    key = _state_key(query.message.chat_id, int(msg_id_str), uhash)
    if key not in _state:
        key = _state_key(query.message.chat_id, query.message.message_id, uhash)
    entry = _state.get(key)
    if entry is None:
        return

    if entry.get("upload_started") or entry.get("cancelling"):
        return

    job_id = entry.get("job_id")
    if not job_id:
        if not entry.get("selection_started"):
            _state.pop(key, None)
            await StatusCard(query.message, query=query).replace(TEXT["cancelled"], reply_markup=None)
            return
        entry["cancel_requested"] = True
        await StatusCard(query.message, query=query).replace(TEXT["cancelled"], reply_markup=None)
        return

    entry["cancelling"] = True
    try:
        result = await cancel_download(job_id)
    except ReclipError:
        entry["cancelling"] = False
        logger.debug("Cancellation failed for ReClip job %s", job_id, exc_info=True)
        return

    if result.get("status") != "cancelled":
        entry["cancelling"] = False
        return

    entry["cancelled"] = True
    _remove_active_entry(entry)
    await event_client.send_download_cancelled(job_id=job_id)

    await StatusCard(query.message, query=query).replace(TEXT["cancelled"], reply_markup=None)


async def retry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, _, token = query.data.partition(":")
    semantic = _retry_store.take(token, update.effective_user.id)
    await query.answer()
    card = StatusCard(query.message, query=query)
    if not token or semantic is None:
        await card.replace(TEXT["retry_expired"], reply_markup=None)
        return

    await card.replace(TEXT["info_loading"], reply_markup=None)
    try:
        info = await get_info(semantic["url"])
    except ReclipError as error:
        await _present_error(card, getattr(error, "error_code", "download_failed"), intent=semantic)
        return
    intent, error_code = _resolve_retry_intent(semantic, info)
    if error_code:
        await _present_error(card, error_code, intent=semantic)
        return
    entry = {
        "url": semantic["url"], "user_id": semantic["user_id"], "info": info,
        "created": time.time(), "retry_intent": semantic,
    }
    asyncio.create_task(download_and_send(query, entry, **intent))


async def download_and_send(
    query,
    entry: dict,
    format: str,
    format_id: str | None,
    *,
    audio_language: str | None = None,
    height: int | None = None,
    start_note: str | None = None,
):
    chat_id = query.message.chat_id
    message = query.message
    url = entry["url"]
    title = entry["info"].get("title", "download")
    card = StatusCard(message)
    semantic = _retry_intent(entry, format, format_id, audio_language, height)

    try:
        start_text = f"{start_note}\n\n{TEXT['download_start']}" if start_note else TEXT["download_start"]
        await card.replace(start_text, reply_markup=_cancel_markup(entry))
    except Exception:
        pass

    try:
        job_id = await start_download(
            url,
            format,
            format_id,
            title,
            audio_language=audio_language,
            height=height,
        )
    except ReclipError as error:
        await _present_error(card, getattr(error, "error_code", "download_failed"), intent=semantic)
        _stats["errors"] += 1
        _remove_active_entry(entry)
        return

    entry["job_id"] = job_id

    try:
        await event_client.send_download_start(
            job_id=job_id,
            user_id=entry["user_id"],
            username=str(entry.get("user_id", "")),
            chat_id=chat_id,
            url=url,
            platform=entry["info"].get("extractor", "unknown"),
            format=format,
            quality=str(height) if height is not None else (format_id or "best"),
            title=title,
        )
    except Exception:
        pass

    if entry.get("cancel_requested"):
        try:
            result = await cancel_download(job_id)
        except ReclipError:
            logger.debug("Cancellation failed for newly created ReClip job %s", job_id, exc_info=True)
        else:
            if result.get("status") == "cancelled":
                entry["cancelled"] = True
                await event_client.send_download_cancelled(job_id=job_id)
                _remove_active_entry(entry)
                return

    try:
        status = await _wait_for_download_job(job_id, card, entry)
    except ReclipError as error:
        error_code = _wait_error_message(error)
        await _present_error(card, error_code, intent=semantic)
        _stats["errors"] += 1
        await event_client.send_download_error(job_id=job_id, error_message=error_code)
        _remove_active_entry(entry)
        return

    if status.get("status") == "cancelled":
        _remove_active_entry(entry)
        return

    if status.get("status") == "error":
        error_code = status.get("error_code", "download_failed")
        await _present_error(card, error_code, intent=semantic)
        _stats["errors"] += 1
        await event_client.send_download_error(job_id=job_id, error_message=error_code)
        _remove_active_entry(entry)
        return

    file_path = status.get("file_path") or status.get("filename")
    video_meta = {
        "width": status.get("width"),
        "height": status.get("height"),
        "duration": status.get("duration"),
    }

    local_path = Path(DOWNLOADS_PATH) / Path(file_path).name
    if not local_path.exists():
        local_path = Path(file_path)
    if not local_path.exists():
        await _present_error(card, "file_missing", intent=semantic)
        _stats["errors"] += 1
        await event_client.send_download_error(job_id=job_id, error_message="file_missing")
        _remove_active_entry(entry)
        return

    try:
        entry["upload_started"] = True
        await card.remove_buttons()
        file_size = await send_local_path(
            query.message.chat,
            local_path,
            caption=_truncate_caption(title),
            video_meta=video_meta if format == "video" else None,
        )
    except TelegramUploadError:
        logger.exception("Upload failed after retry")
        await _present_error(card, "upload_failed", intent=semantic)
        _stats["errors"] += 1
        await event_client.send_download_error(
            job_id=job_id, error_message="upload_failed"
        )
        _remove_active_entry(entry)
        return

    _stats["downloads"] += 1
    try:
        await event_client.send_download_done(
            job_id=job_id,
            file_size_bytes=file_size,
            duration_seconds=time.time() - entry["created"],
            filename=local_path.name,
        )
    except Exception:
        pass

    await card.complete()
    _remove_active_entry(entry)

def _with_preferences(callback, preference_store: PreferenceStore | None):
    @wraps(callback)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        return await callback(update, context, preference_store)

    return wrapped


def _authorized_callback(
    callback,
    allowed_user_ids: frozenset[int],
    preference_store: PreferenceStore | None = None,
):
    @wraps(callback)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user = update.effective_user or query.from_user
        if user is None or user.id not in allowed_user_ids:
            await query.answer()
            return
        if preference_store is None:
            return await callback(update, context)
        return await callback(update, context, preference_store)

    return wrapped


def register_handlers(
    application,
    preference_store: PreferenceStore | None,
    allowed_user_ids: frozenset[int],
):
    allowed = allowed_user_filter(allowed_user_ids)
    application.add_handler(CommandHandler("start", cmd_start, filters=allowed))
    application.add_handler(CommandHandler("help", cmd_help, filters=allowed))
    application.add_handler(CommandHandler("platforms", cmd_platforms, filters=allowed))
    application.add_handler(CommandHandler("stats", cmd_stats, filters=allowed))
    application.add_handler(CommandHandler("settings", _with_preferences(cmd_settings, preference_store), filters=allowed))
    application.add_handler(CommandHandler("setquality", _with_preferences(cmd_setquality, preference_store), filters=allowed))
    application.add_handler(CommandHandler("setformat", _with_preferences(cmd_setformat, preference_store), filters=allowed))
    application.add_handler(CommandHandler("mp3", cmd_mp3, filters=allowed))
    application.add_handler(CommandHandler("mp4", cmd_mp4, filters=allowed))
    application.add_handler(CommandHandler("best", cmd_best, filters=allowed))
    application.add_handler(MessageHandler(
        allowed & filters.TEXT & ~filters.COMMAND,
        _with_preferences(url_handler, preference_store),
    ))
    application.add_handler(CallbackQueryHandler(
        _authorized_callback(format_callback, allowed_user_ids, preference_store), pattern=r"^fmt:"
    ))
    application.add_handler(CallbackQueryHandler(
        _authorized_callback(quality_callback, allowed_user_ids, preference_store), pattern=r"^qty:"
    ))
    application.add_handler(CallbackQueryHandler(
        _authorized_callback(russian_quality_callback, allowed_user_ids, preference_store), pattern=r"^ruqty:"
    ))
    application.add_handler(CallbackQueryHandler(
        _authorized_callback(cancel_callback, allowed_user_ids), pattern=r"^cancel:"
    ))
    application.add_handler(CallbackQueryHandler(
        _authorized_callback(retry_callback, allowed_user_ids), pattern=r"^retry:"
    ))
    application.add_handler(CallbackQueryHandler(
        _authorized_callback(settings_callback, allowed_user_ids, preference_store), pattern=r"^settings:"
    ))
