import asyncio
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import handlers
from preferences import PreferenceStore
from presentation import RetryStore
from reclip_client import ReclipDownloadError, ReclipInfoError


def button_texts(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def button_callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


class FakeQuery:
    def __init__(self, *, data, chat_id, message_id, photo=False, user_id=42):
        self.data = data
        self.message = SimpleNamespace(chat_id=chat_id, message_id=message_id, photo=photo)
        self.from_user = SimpleNamespace(id=user_id)
        self.reply_markup = None
        self.edited_text = None
        self.edited_caption = None
        self.answer_count = 0

    async def answer(self):
        self.answer_count += 1

    async def edit_message_reply_markup(self, *, reply_markup):
        self.reply_markup = reply_markup

    async def edit_message_text(self, text, **kwargs):
        self.edited_text = text
        self.reply_markup = kwargs.get("reply_markup", self.reply_markup)

    async def edit_message_caption(self, caption, **kwargs):
        self.edited_caption = caption
        self.reply_markup = kwargs.get("reply_markup", self.reply_markup)


class FakeUpdate:
    def __init__(self, query, user_id=42):
        self.callback_query = query
        self.effective_user = SimpleNamespace(id=user_id)
        query.from_user = self.effective_user


class SharedMessage:
    photo = False

    def __init__(self, *, chat_id=10, message_id=7):
        self.chat_id = chat_id
        self.message_id = message_id
        self.chat = object()
        self.text = None
        self.reply_markup = None
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.text = text
        self.reply_markup = kwargs.get("reply_markup", self.reply_markup)
        self.edits.append((text, self.reply_markup))

    async def edit_reply_markup(self, *, reply_markup):
        self.reply_markup = reply_markup

    async def delete(self):
        pass


class SharedQuery:
    def __init__(self, message, *, data="cancel:7:abcd", user_id=42):
        self.message = message
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answer_count = 0

    async def answer(self):
        self.answer_count += 1

    async def edit_message_text(self, text, **kwargs):
        await self.message.edit_text(text, **kwargs)

    async def edit_message_caption(self, caption, **kwargs):
        await self.message.edit_text(caption, **kwargs)

    async def edit_message_reply_markup(self, *, reply_markup):
        await self.message.edit_reply_markup(reply_markup=reply_markup)


@pytest.fixture(autouse=True)
def clear_handler_state():
    handlers._state.clear()
    if hasattr(handlers, "_retry_store"):
        handlers._retry_store = RetryStore()
    yield
    handlers._state.clear()


class Message:
    photo = False

    def __init__(self):
        self.edits = []

    async def edit_text(self, text):
        self.edits.append(text)


@pytest.mark.asyncio
async def test_wait_helper_edits_only_when_download_stage_or_progress_changes(monkeypatch):
    message = Message()
    statuses = [
        {"status": "downloading", "stage": "downloading", "progress": {"percent": 10}},
        {"status": "downloading", "stage": "downloading", "progress": {"percent": 10}},
        {"status": "postprocessing", "stage": "postprocessing", "progress": {"percent": 10}},
        {"status": "done", "file_path": "/downloads/video.mp4"},
    ]
    progress_events = []

    async def fake_wait_for_job(job_id, on_status):
        for status in statuses:
            await on_status(status)
        return statuses[-1]

    async def capture_progress(**kwargs):
        progress_events.append(kwargs)

    monkeypatch.setattr(handlers, "wait_for_job", fake_wait_for_job)
    monkeypatch.setattr(handlers.event_client, "send_progress", capture_progress)

    result = await handlers._wait_for_download_job("job-1", message)

    assert result["status"] == "done"
    assert message.edits == ["Загрузка: 10%", "Обработка файла…"]
    assert [event["stage"] for event in progress_events] == [
        "downloading", "downloading", "postprocessing",
    ]


@pytest.mark.asyncio
async def test_wait_helper_keeps_download_alive_when_a_normal_card_edit_fails(monkeypatch):
    class FailingMessage:
        photo = False

        async def edit_text(self, text, **kwargs):
            raise RuntimeError("Telegram edit failed")

    async def fake_wait_for_job(job_id, on_status):
        await on_status({"status": "downloading", "progress": {"percent": 10}})
        return {"status": "done", "file_path": "/downloads/video.mp4"}

    async def ignore_progress(**kwargs):
        pass

    monkeypatch.setattr(handlers, "wait_for_job", fake_wait_for_job)
    monkeypatch.setattr(handlers.event_client, "send_progress", ignore_progress)

    result = await handlers._wait_for_download_job("job-1", FailingMessage())

    assert result["status"] == "done"


@pytest.mark.asyncio
async def test_wait_helper_keeps_cancel_button_on_active_progress_edits(monkeypatch):
    class ProgressMessage:
        photo = False

        def __init__(self):
            self.edits = []

        async def edit_text(self, text, **kwargs):
            self.edits.append((text, kwargs))

    message = ProgressMessage()
    entry = {"cancel_callback_data": "cancel:7:abcd"}

    async def fake_wait_for_job(job_id, on_status):
        await on_status({"status": "downloading", "progress": {"percent": 10}})
        return {"status": "done", "file_path": "/downloads/video.mp4"}

    async def ignore_progress(**kwargs):
        pass

    monkeypatch.setattr(handlers, "wait_for_job", fake_wait_for_job)
    monkeypatch.setattr(handlers.event_client, "send_progress", ignore_progress)

    await handlers._wait_for_download_job("job-1", message, entry)

    text, kwargs = message.edits[-1]
    assert text == "Загрузка: 10%"
    assert button_callbacks(kwargs["reply_markup"]) == ["cancel:7:abcd"]


def test_format_buttons_show_ru_only_when_available():
    hidden = handlers._build_format_buttons(7, "abcd", {"available": False, "formats": []})
    shown = handlers._build_format_buttons(
        7, "abcd", {"available": True, "formats": [{"height": 720, "label": "720p"}]}
    )

    assert button_texts(hidden) == ["MP4", "MP3", "Отменить"]
    assert button_texts(shown) == ["MP4", "MP4 • RU", "MP3", "Отменить"]
    assert "fmt:7:abcd:video_ru" in button_callbacks(shown)
    assert button_callbacks(hidden)[-1] == "cancel:7:abcd"


def test_russian_quality_buttons_store_height_not_format_id():
    markup = handlers._build_russian_quality_buttons(
        7, "abcd",
        [{"height": 1080, "label": "1080p"}, {"height": 720, "label": "720p"}],
    )

    assert button_callbacks(markup) == [
        "ruqty:7:abcd:1080", "ruqty:7:abcd:720", "ruqty:7:abcd:best",
        "fmt:7:abcd:back", "cancel:7:abcd",
    ]
    assert button_texts(markup) == ["1080p", "720p", "Лучшее качество", "Назад", "Отменить"]


def test_quality_buttons_include_cancel():
    markup = handlers._build_quality_buttons(
        7, "abcd", [{"id": "22", "label": "720p", "height": 720}],
    )

    assert button_callbacks(markup) == [
        "qty:7:abcd:22", "qty:7:abcd:best", "fmt:7:abcd:back", "cancel:7:abcd",
    ]
    assert button_texts(markup) == ["720p", "Лучшее качество", "Назад", "Отменить"]


def test_quality_menus_offer_russian_back_navigation():
    normal = handlers._build_quality_buttons(
        7, "abcd", [{"id": "22", "label": "720p", "height": 720}],
    )
    russian = handlers._build_russian_quality_buttons(7, "abcd", [{"height": 720, "label": "720p"}])

    assert "Назад" in button_texts(normal)
    assert "fmt:7:abcd:back" in button_callbacks(normal)
    assert "Назад" in button_texts(russian)
    assert "fmt:7:abcd:back" in button_callbacks(russian)


@pytest.mark.asyncio
async def test_retry_refetches_info_and_resolves_current_semantic_height(monkeypatch):
    store = RetryStore()
    token = store.put({
        "url": "https://youtu.be/x", "format": "video", "quality": "480",
        "audio_mode": "original", "user_id": 42,
    })
    handlers._retry_store = store
    query = FakeQuery(data=f"retry:{token}", chat_id=10, message_id=7)
    started = []

    async def fresh_info(url):
        return {
            "title": "Новая версия", "formats": [{"id": "new-360", "height": 360}],
        }

    async def capture_download(query_arg, entry, **kwargs):
        started.append((entry, kwargs))

    monkeypatch.setattr(handlers, "get_info", fresh_info)
    monkeypatch.setattr(handlers, "download_and_send", capture_download)

    await handlers.retry_callback(FakeUpdate(query), None)
    await asyncio.sleep(0)

    assert len(started) == 1
    entry, intent = started[0]
    assert entry["url"] == "https://youtu.be/x"
    assert entry["info"]["title"] == "Новая версия"
    assert intent == {
        "format": "video", "format_id": "new-360",
        "audio_language": None, "height": None,
    }
    assert handlers._state[entry["state_key"]] is entry
    assert entry["user_id"] == 42
    assert entry["cancel_callback_data"] == "cancel:7:2f0683ba"

    await handlers.retry_callback(FakeUpdate(query), None)
    await asyncio.sleep(0)
    assert len(started) == 1


@pytest.mark.asyncio
async def test_retry_honors_ru_if_available_when_track_is_currently_available(monkeypatch):
    token = handlers._retry_store.put({
        "url": "https://youtu.be/x", "format": "video", "quality": "480",
        "audio_mode": "ru_if_available", "user_id": 42,
    })
    query = FakeQuery(data=f"retry:{token}", chat_id=10, message_id=7)
    started = []

    async def fresh_info(url):
        return {
            "title": "Video",
            "formats": [{"id": "normal-480", "height": 480}],
            "russian_audio": {
                "available": True,
                "formats": [
                    {"height": 720, "label": "720p"},
                    {"height": 360, "label": "360p"},
                ],
            },
        }

    async def capture_download(query_arg, entry, **kwargs):
        started.append(kwargs)

    monkeypatch.setattr(handlers, "get_info", fresh_info)
    monkeypatch.setattr(handlers, "download_and_send", capture_download)

    await handlers.retry_callback(FakeUpdate(query), None)
    await asyncio.sleep(0)

    assert started == [{
        "format": "video", "format_id": None,
        "audio_language": "ru", "height": 360,
    }]


@pytest.mark.asyncio
async def test_non_owner_retry_callback_cannot_consume_the_owners_token(monkeypatch):
    token = handlers._retry_store.put({
        "url": "https://youtu.be/x", "format": "audio", "quality": "best",
        "audio_mode": "original", "user_id": 42,
    })
    wrong_user = FakeQuery(data=f"retry:{token}", chat_id=10, message_id=7)
    owner = FakeQuery(data=f"retry:{token}", chat_id=10, message_id=7)
    started = []

    async def fresh_info(url):
        return {"title": "Новая версия"}

    async def capture_download(*args, **kwargs):
        started.append(kwargs)

    monkeypatch.setattr(handlers, "get_info", fresh_info)
    monkeypatch.setattr(handlers, "download_and_send", capture_download)

    await handlers.retry_callback(FakeUpdate(wrong_user, user_id=7), None)
    await handlers.retry_callback(FakeUpdate(owner, user_id=42), None)
    await asyncio.sleep(0)

    assert wrong_user.edited_text == "Время повтора истекло. Отправьте ссылку ещё раз."
    assert len(started) == 1


@pytest.mark.asyncio
async def test_first_metadata_retry_returns_to_manual_picker_without_autodownload(monkeypatch):
    class StatusMessage:
        photo = False
        message_id = 7

        def __init__(self):
            self.edits = []

        async def edit_text(self, text, **kwargs):
            self.edits.append((text, kwargs))

    status = StatusMessage()

    class Message:
        text = "https://youtu.be/x"

        async def reply_text(self, text, **kwargs):
            return status

    info_calls = 0

    async def unavailable_then_fresh(url):
        nonlocal info_calls
        info_calls += 1
        if info_calls == 1:
            raise ReclipInfoError("backend timeout", error_code="network")
        return {
            "title": "Video",
            "formats": [{"id": "22", "height": 720, "label": "720p"}],
            "russian_audio": {"available": False, "formats": []},
        }

    started = []

    async def capture_download(*args, **kwargs):
        started.append((args, kwargs))

    monkeypatch.setattr(handlers, "get_info", unavailable_then_fresh)
    monkeypatch.setattr(handlers, "download_and_send", capture_download)
    update = SimpleNamespace(
        message=Message(), effective_user=SimpleNamespace(id=42), effective_chat=SimpleNamespace(id=10),
    )

    await handlers.url_handler(update, None)

    text, kwargs = status.edits[-1]
    assert text == "Не удалось загрузить файл. Попробуйте ещё раз."
    token = button_callbacks(kwargs["reply_markup"])[0].removeprefix("retry:")
    query = FakeQuery(data=f"retry:{token}", chat_id=10, message_id=7)

    await handlers.retry_callback(FakeUpdate(query), None)
    await asyncio.sleep(0)

    assert started == []
    assert button_callbacks(query.reply_markup) == [
        "fmt:7:2f0683ba:video", "fmt:7:2f0683ba:audio", "cancel:7:2f0683ba",
    ]
    key = handlers._state_key(10, 7, "2f0683ba")
    assert handlers._state[key]["user_id"] == 42
    assert handlers._state[key]["url"] == "https://youtu.be/x"


@pytest.mark.asyncio
async def test_direct_download_shows_safe_russian_coded_error(monkeypatch):
    class StatusMessage:
        photo = False

        def __init__(self):
            self.edits = []

        async def edit_text(self, text, **kwargs):
            self.edits.append((text, kwargs))

    status = StatusMessage()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=42))

    async def unavailable(url):
        raise ReclipInfoError("socket timeout at internal host", error_code="network")

    monkeypatch.setattr(handlers, "get_info", unavailable)

    await handlers._direct_download(update, status, "https://youtu.be/x", "audio", None)

    text, kwargs = status.edits[0]
    assert text == "Не удалось загрузить файл. Попробуйте ещё раз."
    assert button_texts(kwargs["reply_markup"]) == ["Повторить"]


@pytest.mark.asyncio
async def test_start_card_is_presented_in_russian():
    replies = []

    class Message:
        async def reply_text(self, text, **kwargs):
            replies.append(text)

    await handlers.cmd_start(SimpleNamespace(message=Message()), None)

    assert "Отправьте мне ссылку" in replies[0]
    assert "Send me" not in replies[0]


def test_select_height_prefers_exact_then_lower_then_smallest():
    formats = [{"height": 1080}, {"height": 720}, {"height": 360}]

    assert handlers._select_height(formats, "720") == 720
    assert handlers._select_height(formats, "480") == 360
    assert handlers._select_height([{ "height": 720 }, {"height": 1080}], "480") == 720
    assert handlers._select_height(formats, "best") is None


def test_download_intent_uses_available_russian_audio_or_notes_original_fallback():
    info_with_russian = {
        "formats": [{"height": 1080}, {"height": 720}],
        "russian_audio": {"available": True, "formats": [{"height": 720}, {"height": 360}]},
    }

    assert handlers._download_intent("video", "480", "ru_if_available", info_with_russian) == {
        "format": "video", "format_id": None, "audio_language": "ru", "height": 360,
    }
    assert handlers._download_intent(
        "video", "480", "ru_if_available",
        {"formats": [{"id": "normal-720", "height": 720}]},
    ) == {
        "format": "video", "format_id": "normal-720", "audio_language": None, "height": None,
        "fallback_note": "Русская дорожка недоступна — скачиваем оригинал.",
    }
    assert handlers._download_intent("audio", "720", "original", info_with_russian) == {
        "format": "audio", "format_id": None, "audio_language": None, "height": None,
    }


@pytest.mark.parametrize(
    ("quality", "formats", "expected_format_id"),
    [
        ("720", [{"id": "1080", "height": 1080}, {"id": "720", "height": 720}], "720"),
        ("720", [{"id": "1080", "height": 1080}, {"id": "480", "height": 480}], "480"),
        ("480", [{"id": "1080", "height": 1080}, {"id": "720", "height": 720}], "720"),
        ("best", [{"id": "1080", "height": 1080}], None),
    ],
)
def test_saved_fixed_video_intent_resolves_current_format_id(
    quality, formats, expected_format_id,
):
    intent = handlers._download_intent(
        "video", quality, "original", {"formats": formats},
    )

    assert intent == {
        "format": "video",
        "format_id": expected_format_id,
        "audio_language": None,
        "height": None,
    }


@pytest.mark.asyncio
async def test_settings_callbacks_update_and_reset_durable_preferences(tmp_path):
    store = PreferenceStore(str(tmp_path / "bot.db"))
    await store.initialize()
    query = FakeQuery(data="settings:format:audio", chat_id=10, message_id=7)

    await handlers.settings_callback(FakeUpdate(query), None, store)
    assert await store.get(42) == {
        "format": "audio", "quality": "best", "audio_mode": "original",
    }

    query.data = "settings:quality:720"
    await handlers.settings_callback(FakeUpdate(query), None, store)
    query.data = "settings:audio:ru_if_available"
    await handlers.settings_callback(FakeUpdate(query), None, store)
    assert await store.get(42) == {
        "format": "audio", "quality": "720", "audio_mode": "ru_if_available",
    }

    query.data = "settings:reset"
    await handlers.settings_callback(FakeUpdate(query), None, store)
    assert await store.get(42) is None
    assert "Настройки сброшены" in query.edited_text


@pytest.mark.asyncio
async def test_first_russian_mp4_choice_saves_soft_future_audio_mode(tmp_path, monkeypatch):
    store = PreferenceStore(str(tmp_path / "bot.db"))
    await store.initialize()
    query = FakeQuery(data="ruqty:7:abcd:720", chat_id=10, message_id=7)
    handlers._state[handlers._state_key(10, 7, "abcd")] = {
        "url": "https://youtu.be/x", "info": {"title": "Video"},
        "created": time.time(), "user_id": 42,
    }

    async def ignore_download(*args, **kwargs):
        pass

    monkeypatch.setattr(handlers, "download_and_send", ignore_download)

    await handlers.russian_quality_callback(FakeUpdate(query), None, store)
    await asyncio.sleep(0)

    assert await store.get(42) == {
        "format": "video", "quality": "720", "audio_mode": "ru_if_available",
    }


@pytest.mark.asyncio
async def test_russian_quality_callback_rejects_non_persistable_height(
    tmp_path, monkeypatch,
):
    store = PreferenceStore(str(tmp_path / "bot.db"))
    await store.initialize()
    query = FakeQuery(data="ruqty:7:abcd:2160", chat_id=10, message_id=7)
    entry = {
        "url": "https://youtu.be/x", "info": {"title": "Video"},
        "created": time.time(), "user_id": 42,
    }
    handlers._state[handlers._state_key(10, 7, "abcd")] = entry
    calls = []

    async def fake_download(*args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(handlers, "download_and_send", fake_download)

    await handlers.russian_quality_callback(FakeUpdate(query), None, store)
    await asyncio.sleep(0)

    assert calls == []
    assert await store.get(42) is None
    assert entry.get("selection_started") is not True
    assert query.edited_text == "Время выбора истекло. Отправьте ссылку ещё раз."
    assert query.reply_markup is None


@pytest.mark.asyncio
async def test_normal_quality_callback_rejects_non_persistable_height(tmp_path, monkeypatch):
    store = PreferenceStore(str(tmp_path / "bot.db"))
    await store.initialize()
    entry = {
        "url": "https://youtu.be/x",
        "info": {"title": "Video", "formats": [{"id": "high-id", "height": 1440}]},
        "created": time.time(), "user_id": 42,
    }
    query = FakeQuery(data="qty:7:abcd:high-id", chat_id=10, message_id=7)
    handlers._state[handlers._state_key(10, 7, "abcd")] = entry

    async def ignore_download(*args, **kwargs):
        pass

    monkeypatch.setattr(handlers, "download_and_send", ignore_download)

    await handlers.quality_callback(FakeUpdate(query), None, store)
    await asyncio.sleep(0)

    assert await store.get(42) is None
    assert entry.get("selection_started") is not True
    assert query.edited_text == "Время выбора истекло. Отправьте ссылку ещё раз."
    assert query.reply_markup is None


def test_manual_picker_only_offers_persistable_qualities():
    normal = handlers._build_quality_buttons(7, "abcd", [
        {"id": "high", "height": 1440, "label": "1440p"},
        {"id": "1080", "height": 1080, "label": "1080p"},
        {"id": "odd", "height": 540, "label": "540p"},
        {"id": "480", "height": 480, "label": "480p"},
    ])
    russian = handlers._build_russian_quality_buttons(7, "abcd", [
        {"height": 2160, "label": "2160p"},
        {"height": 720, "label": "720p"},
        {"height": 540, "label": "540p"},
        {"height": 360, "label": "360p"},
    ])

    assert button_texts(normal) == [
        "1080p", "480p", "Лучшее качество", "Назад", "Отменить",
    ]
    assert button_texts(russian) == [
        "720p", "360p", "Лучшее качество", "Назад", "Отменить",
    ]


@pytest.mark.asyncio
async def test_download_start_keeps_russian_audio_fallback_note_visible(monkeypatch):
    edits = []

    class Message:
        photo = False
        chat = object()
        chat_id = 10

        async def edit_text(self, text):
            edits.append(text)

    async def fake_start(*args, **kwargs):
        return "job-1"

    async def fake_wait(*args, **kwargs):
        return {"status": "error", "error": "stop"}

    async def ignore_event(**kwargs):
        pass

    monkeypatch.setattr(handlers, "start_download", fake_start)
    monkeypatch.setattr(handlers, "_wait_for_download_job", fake_wait)
    monkeypatch.setattr(handlers.event_client, "send_download_start", ignore_event)
    monkeypatch.setattr(handlers.event_client, "send_download_error", ignore_event)

    await handlers.download_and_send(
        SimpleNamespace(message=Message()),
        {"url": "https://youtu.be/x", "info": {"title": "Video"}, "user_id": 42, "created": time.time()},
        format="video",
        format_id=None,
        start_note="Русская дорожка недоступна — скачиваем оригинал.",
    )

    assert edits[0] == "Русская дорожка недоступна — скачиваем оригинал.\n\nНачинаем загрузку…"


@pytest.mark.asyncio
async def test_saved_preferences_auto_start_with_fresh_quality_and_cancellable_state(
    monkeypatch, tmp_path,
):
    store = PreferenceStore(str(tmp_path / "bot.db"))
    await store.initialize()
    await store.save(42, format="video", quality="480", audio_mode="ru_if_available")
    started = []

    class StatusMessage:
        message_id = 7
        chat_id = 10
        chat = object()
        photo = False

        async def edit_text(self, text, **kwargs):
            self.text = text

    status = StatusMessage()

    class Message:
        text = "https://youtu.be/x"

        async def reply_text(self, text, **kwargs):
            return status

    update = SimpleNamespace(
        message=Message(), effective_user=SimpleNamespace(id=42), effective_chat=SimpleNamespace(id=10),
    )

    async def fake_info(url):
        return {
            "title": "Video",
            "formats": [{"id": "fresh-720", "height": 720}],
        }

    async def fake_download(query, entry, **kwargs):
        started.append((entry, kwargs))

    monkeypatch.setattr(handlers, "get_info", fake_info)
    monkeypatch.setattr(handlers, "download_and_send", fake_download)

    await handlers.url_handler(update, None, store)
    await asyncio.sleep(0)

    assert started[0][1] == {
        "format": "video", "format_id": "fresh-720", "audio_language": None, "height": None,
        "start_note": "Русская дорожка недоступна — скачиваем оригинал.",
    }
    entry = started[0][0]
    assert handlers._state[entry["state_key"]] is entry
    assert entry["user_id"] == 42
    assert entry["cancel_callback_data"] == "cancel:7:2f0683ba"


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["audio", "video"])
async def test_direct_download_registers_owner_bound_cancellable_state(monkeypatch, fmt):
    class StatusMessage:
        message_id = 7
        chat_id = 10
        chat = object()
        photo = False

        async def edit_text(self, text, **kwargs):
            pass

    status = StatusMessage()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=42))
    started = []

    async def fake_info(url):
        return {"title": "Video", "formats": []}

    async def capture_download(query, entry, **kwargs):
        started.append((entry, kwargs))

    monkeypatch.setattr(handlers, "get_info", fake_info)
    monkeypatch.setattr(handlers, "download_and_send", capture_download)

    await handlers._direct_download(
        update, status, "https://youtu.be/x", fmt, None,
    )
    await asyncio.sleep(0)

    entry, intent = started[0]
    assert intent == {"format": fmt, "format_id": None}
    assert handlers._state[entry["state_key"]] is entry
    assert entry["user_id"] == 42
    assert entry["selection_started"] is True
    assert entry["cancel_callback_data"] == "cancel:7:2f0683ba"


@pytest.mark.asyncio
async def test_direct_download_releases_update_loop_while_lifecycle_remains_active(monkeypatch):
    class StatusMessage:
        message_id = 7
        chat_id = 10
        chat = object()
        photo = False

        async def edit_text(self, text, **kwargs):
            pass

    lifecycle_started = asyncio.Event()
    finish_lifecycle = asyncio.Event()

    async def fake_info(url):
        return {"title": "Video", "formats": []}

    async def pending_download(query, entry, **kwargs):
        lifecycle_started.set()
        await finish_lifecycle.wait()

    monkeypatch.setattr(handlers, "get_info", fake_info)
    monkeypatch.setattr(handlers, "download_and_send", pending_download)
    direct_task = asyncio.create_task(handlers._direct_download(
        SimpleNamespace(effective_user=SimpleNamespace(id=42)),
        StatusMessage(),
        "https://youtu.be/x",
        "video",
        None,
    ))

    await lifecycle_started.wait()
    await asyncio.sleep(0)

    assert direct_task.done() is True
    entry = next(iter(handlers._state.values()))
    assert entry["cancel_callback_data"] == "cancel:7:2f0683ba"

    finish_lifecycle.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cancel_removes_session_and_replaces_text_card():
    key = handlers._state_key(10, 7, "abcd")
    handlers._state[key] = {"created": time.time(), "user_id": 42}
    query = FakeQuery(data="cancel:7:abcd", chat_id=10, message_id=7)

    await handlers.cancel_callback(FakeUpdate(query), None)

    assert key not in handlers._state
    assert query.edited_text == "Отменено."
    assert query.reply_markup is None


@pytest.mark.asyncio
async def test_cancel_removes_session_and_replaces_photo_caption():
    key = handlers._state_key(10, 7, "abcd")
    handlers._state[key] = {"created": time.time(), "user_id": 42}
    query = FakeQuery(data="cancel:7:abcd", chat_id=10, message_id=7, photo=True)

    await handlers.cancel_callback(FakeUpdate(query), None)

    assert key not in handlers._state
    assert query.edited_caption == "Отменено."
    assert query.reply_markup is None


@pytest.mark.asyncio
async def test_active_job_cancel_calls_reclip_and_reports_cancelled(monkeypatch):
    key = handlers._state_key(10, 7, "abcd")
    handlers._state[key] = {
        "created": time.time(), "user_id": 42,
        "job_id": "job-1", "upload_started": False,
    }
    query = FakeQuery(data="cancel:7:abcd", chat_id=10, message_id=7)
    cancelled_job_ids = []
    event_types = []

    async def cancel_reclip(job_id):
        cancelled_job_ids.append(job_id)
        return {"job_id": job_id, "status": "cancelled"}

    async def report_cancelled(*, job_id):
        event_types.append(("download_cancelled", job_id))

    monkeypatch.setattr(handlers, "cancel_download", cancel_reclip)
    monkeypatch.setattr(handlers.event_client, "send_download_cancelled", report_cancelled)

    await handlers.cancel_callback(FakeUpdate(query), None)

    assert cancelled_job_ids == ["job-1"]
    assert event_types == [("download_cancelled", "job-1")]
    assert key not in handlers._state
    assert query.edited_text == "Отменено."


@pytest.mark.asyncio
async def test_active_job_cancel_remains_available_after_selection_ttl(monkeypatch):
    key = handlers._state_key(10, 7, "abcd")
    handlers._state[key] = {
        "created": time.time() - handlers.STATE_TTL - 1,
        "selection_started": True,
        "user_id": 42,
        "job_id": "job-1",
        "upload_started": False,
    }
    query = FakeQuery(data="cancel:7:abcd", chat_id=10, message_id=7)
    cancelled_job_ids = []

    async def cancel_reclip(job_id):
        cancelled_job_ids.append(job_id)
        return {"job_id": job_id, "status": "cancelled"}

    async def ignore_cancelled(**kwargs):
        pass

    monkeypatch.setattr(handlers, "cancel_download", cancel_reclip)
    monkeypatch.setattr(handlers.event_client, "send_download_cancelled", ignore_cancelled)

    await handlers.cancel_callback(FakeUpdate(query), None)

    assert cancelled_job_ids == ["job-1"]
    assert key not in handlers._state


@pytest.mark.asyncio
async def test_stale_progress_after_successful_cancel_does_not_restore_cancel_button(monkeypatch):
    class RaceMessage:
        photo = False
        chat_id = 10
        message_id = 7

        def __init__(self):
            self.text = None
            self.reply_markup = None

        async def edit_text(self, text, **kwargs):
            self.text = text
            self.reply_markup = kwargs.get("reply_markup", self.reply_markup)

    class RaceQuery:
        data = "cancel:7:abcd"

        def __init__(self, message):
            self.message = message

        async def answer(self):
            pass

        async def edit_message_text(self, text, **kwargs):
            await self.message.edit_text(text, **kwargs)

        async def edit_message_reply_markup(self, *, reply_markup):
            self.message.reply_markup = reply_markup

    key = handlers._state_key(10, 7, "abcd")
    entry = {
        "created": time.time(),
        "selection_started": True,
        "user_id": 42,
        "job_id": "job-1",
        "cancel_callback_data": "cancel:7:abcd",
    }
    handlers._state[key] = entry
    message = RaceMessage()
    query = RaceQuery(message)
    poll_started = asyncio.Event()
    release_stale_progress = asyncio.Event()

    async def fake_wait_for_job(job_id, on_status):
        poll_started.set()
        await release_stale_progress.wait()
        await on_status({"status": "downloading", "progress": {"percent": 10}})
        return {"status": "cancelled"}

    async def cancel_reclip(job_id):
        return {"job_id": job_id, "status": "cancelled"}

    async def ignore_event(**kwargs):
        pass

    monkeypatch.setattr(handlers, "wait_for_job", fake_wait_for_job)
    monkeypatch.setattr(handlers, "cancel_download", cancel_reclip)
    monkeypatch.setattr(handlers.event_client, "send_download_cancelled", ignore_event)

    wait_task = asyncio.create_task(handlers._wait_for_download_job("job-1", message, entry))
    await poll_started.wait()
    await handlers.cancel_callback(FakeUpdate(query), None)
    release_stale_progress.set()
    await wait_task

    assert message.text == "Отменено."
    assert message.reply_markup is None


@pytest.mark.asyncio
async def test_in_flight_progress_edit_cannot_overwrite_cancelled_card(monkeypatch):
    class BlockingMessage(SharedMessage):
        def __init__(self):
            super().__init__()
            self.progress_edit_started = asyncio.Event()
            self.release_progress_edit = asyncio.Event()

        async def edit_text(self, text, **kwargs):
            if text.startswith("Загрузка"):
                self.progress_edit_started.set()
                await self.release_progress_edit.wait()
            await super().edit_text(text, **kwargs)

    message = BlockingMessage()
    query = SharedQuery(message)
    key = handlers._state_key(10, 7, "abcd")
    entry = {
        "created": time.time(),
        "selection_started": True,
        "user_id": 42,
        "job_id": "job-1",
        "state_key": key,
        "cancel_callback_data": "cancel:7:abcd",
    }
    handlers._state[key] = entry

    async def fake_wait_for_job(job_id, on_status):
        await on_status({"status": "downloading", "progress": {"percent": 10}})
        return {"status": "cancelled"}

    async def cancel_reclip(job_id):
        return {"job_id": job_id, "status": "cancelled"}

    async def ignore_event(**kwargs):
        pass

    monkeypatch.setattr(handlers, "wait_for_job", fake_wait_for_job)
    monkeypatch.setattr(handlers, "cancel_download", cancel_reclip)
    monkeypatch.setattr(handlers.event_client, "send_download_cancelled", ignore_event)
    monkeypatch.setattr(handlers.event_client, "send_progress", ignore_event)

    wait_task = asyncio.create_task(
        handlers._wait_for_download_job("job-1", message, entry)
    )
    await message.progress_edit_started.wait()
    cancel_task = asyncio.create_task(handlers.cancel_callback(FakeUpdate(query), None))
    await asyncio.sleep(0)
    message.release_progress_edit.set()
    await asyncio.gather(wait_task, cancel_task)

    assert message.text == "Отменено."
    assert message.reply_markup is None


@pytest.mark.asyncio
async def test_successful_cancel_wins_over_late_wait_error(monkeypatch):
    message = SharedMessage()
    query = SharedQuery(message)
    key = handlers._state_key(10, 7, "abcd")
    entry = {
        "url": "https://youtu.be/x",
        "info": {"title": "Video", "extractor": "youtube"},
        "created": time.time(),
        "user_id": 42,
        "state_key": key,
        "selection_started": True,
        "cancel_callback_data": "cancel:7:abcd",
    }
    handlers._state[key] = entry
    wait_started = asyncio.Event()
    release_wait = asyncio.Event()
    cancelled_events = []
    error_events = []

    async def fake_start(*args, **kwargs):
        return "job-1"

    async def late_wait_error(*args, **kwargs):
        wait_started.set()
        await release_wait.wait()
        raise ReclipDownloadError("stale poll failed", error_code="network")

    async def cancel_reclip(job_id):
        return {"job_id": job_id, "status": "cancelled"}

    async def ignore_start(**kwargs):
        pass

    async def record_cancelled(*, job_id):
        cancelled_events.append(job_id)

    async def record_error(**kwargs):
        error_events.append(kwargs)

    monkeypatch.setattr(handlers, "start_download", fake_start)
    monkeypatch.setattr(handlers, "_wait_for_download_job", late_wait_error)
    monkeypatch.setattr(handlers, "cancel_download", cancel_reclip)
    monkeypatch.setattr(handlers.event_client, "send_download_start", ignore_start)
    monkeypatch.setattr(handlers.event_client, "send_download_cancelled", record_cancelled)
    monkeypatch.setattr(handlers.event_client, "send_download_error", record_error)

    download_task = asyncio.create_task(handlers.download_and_send(
        SimpleNamespace(message=message), entry, format="video", format_id=None,
    ))
    await wait_started.wait()
    await handlers.cancel_callback(FakeUpdate(query), None)
    release_wait.set()
    await download_task

    assert message.text == "Отменено."
    assert message.reply_markup is None
    assert cancelled_events == ["job-1"]
    assert error_events == []
    assert key not in handlers._state


@pytest.mark.asyncio
async def test_in_flight_cancel_wins_before_poll_error_can_publish(monkeypatch):
    message = SharedMessage()
    query = SharedQuery(message)
    key = handlers._state_key(10, 7, "abcd")
    entry = {
        "url": "https://youtu.be/x",
        "info": {"title": "Video", "extractor": "youtube"},
        "created": time.time(),
        "user_id": 42,
        "state_key": key,
        "selection_started": True,
        "cancel_callback_data": "cancel:7:abcd",
    }
    handlers._state[key] = entry
    wait_started = asyncio.Event()
    release_poll_error = asyncio.Event()
    cancel_started = asyncio.Event()
    release_cancel = asyncio.Event()
    cancelled_events = []
    error_events = []

    async def fake_start(*args, **kwargs):
        return "job-1"

    async def poll_error(*args, **kwargs):
        wait_started.set()
        await release_poll_error.wait()
        raise ReclipDownloadError("poll failed", error_code="network")

    async def delayed_cancel(job_id):
        cancel_started.set()
        await release_cancel.wait()
        return {"job_id": job_id, "status": "cancelled"}

    async def ignore_start(**kwargs):
        pass

    async def record_cancelled(*, job_id):
        cancelled_events.append(job_id)

    async def record_error(**kwargs):
        error_events.append(kwargs)

    monkeypatch.setattr(handlers, "start_download", fake_start)
    monkeypatch.setattr(handlers, "_wait_for_download_job", poll_error)
    monkeypatch.setattr(handlers, "cancel_download", delayed_cancel)
    monkeypatch.setattr(handlers.event_client, "send_download_start", ignore_start)
    monkeypatch.setattr(handlers.event_client, "send_download_cancelled", record_cancelled)
    monkeypatch.setattr(handlers.event_client, "send_download_error", record_error)

    download_task = asyncio.create_task(handlers.download_and_send(
        SimpleNamespace(message=message), entry, format="video", format_id=None,
    ))
    await wait_started.wait()
    cancel_task = asyncio.create_task(handlers.cancel_callback(FakeUpdate(query), None))
    await cancel_started.wait()
    release_poll_error.set()
    await asyncio.sleep(0)
    release_cancel.set()
    await asyncio.gather(download_task, cancel_task)

    assert message.text == "Отменено."
    assert cancelled_events == ["job-1"]
    assert error_events == []
    assert key not in handlers._state


@pytest.mark.asyncio
async def test_lost_cancel_response_is_reconciled_by_polled_cancelled_status(monkeypatch):
    message = SharedMessage()
    query = SharedQuery(message)
    key = handlers._state_key(10, 7, "abcd")
    entry = {
        "url": "https://youtu.be/x",
        "info": {"title": "Video", "extractor": "youtube"},
        "created": time.time(),
        "user_id": 42,
        "state_key": key,
        "selection_started": True,
        "cancel_callback_data": "cancel:7:abcd",
    }
    handlers._state[key] = entry
    wait_started = asyncio.Event()
    release_wait = asyncio.Event()
    cancelled_events = []
    error_events = []

    async def fake_start(*args, **kwargs):
        return "job-1"

    async def cancelled_status(*args, **kwargs):
        wait_started.set()
        await release_wait.wait()
        return {"status": "cancelled"}

    async def lost_response(job_id):
        raise ReclipDownloadError("response lost", error_code="network")

    async def ignore_start(**kwargs):
        pass

    async def record_cancelled(*, job_id):
        cancelled_events.append(job_id)

    async def record_error(**kwargs):
        error_events.append(kwargs)

    monkeypatch.setattr(handlers, "start_download", fake_start)
    monkeypatch.setattr(handlers, "_wait_for_download_job", cancelled_status)
    monkeypatch.setattr(handlers, "cancel_download", lost_response)
    monkeypatch.setattr(handlers.event_client, "send_download_start", ignore_start)
    monkeypatch.setattr(handlers.event_client, "send_download_cancelled", record_cancelled)
    monkeypatch.setattr(handlers.event_client, "send_download_error", record_error)

    download_task = asyncio.create_task(handlers.download_and_send(
        SimpleNamespace(message=message), entry, format="video", format_id=None,
    ))
    await wait_started.wait()
    await handlers.cancel_callback(FakeUpdate(query), None)
    release_wait.set()
    await download_task

    assert message.text == "Отменено."
    assert message.reply_markup is None
    assert cancelled_events == ["job-1"]
    assert error_events == []
    assert key not in handlers._state


@pytest.mark.asyncio
async def test_cancel_response_and_polled_cancelled_emit_one_terminal_outcome(monkeypatch):
    message = SharedMessage()
    query = SharedQuery(message)
    key = handlers._state_key(10, 7, "abcd")
    entry = {
        "url": "https://youtu.be/x",
        "info": {"title": "Video", "extractor": "youtube"},
        "created": time.time(),
        "user_id": 42,
        "state_key": key,
        "selection_started": True,
        "cancel_callback_data": "cancel:7:abcd",
    }
    handlers._state[key] = entry
    wait_started = asyncio.Event()
    release_wait = asyncio.Event()
    cancelled_events = []

    async def fake_start(*args, **kwargs):
        return "job-1"

    async def cancelled_status(*args, **kwargs):
        wait_started.set()
        await release_wait.wait()
        return {"status": "cancelled"}

    async def cancel_reclip(job_id):
        release_wait.set()
        return {"job_id": job_id, "status": "cancelled"}

    async def ignore_start(**kwargs):
        pass

    async def record_cancelled(*, job_id):
        cancelled_events.append(job_id)

    monkeypatch.setattr(handlers, "start_download", fake_start)
    monkeypatch.setattr(handlers, "_wait_for_download_job", cancelled_status)
    monkeypatch.setattr(handlers, "cancel_download", cancel_reclip)
    monkeypatch.setattr(handlers.event_client, "send_download_start", ignore_start)
    monkeypatch.setattr(handlers.event_client, "send_download_cancelled", record_cancelled)

    download_task = asyncio.create_task(handlers.download_and_send(
        SimpleNamespace(message=message), entry, format="video", format_id=None,
    ))
    await wait_started.wait()
    await handlers.cancel_callback(FakeUpdate(query), None)
    await download_task

    assert cancelled_events == ["job-1"]
    assert [text for text, _ in message.edits].count("Отменено.") == 1


@pytest.mark.asyncio
async def test_pre_upload_cancel_request_skips_upload_after_terminal_conflict(monkeypatch, tmp_path):
    downloaded_file = tmp_path / "video.mp4"
    downloaded_file.touch()
    key = handlers._state_key(10, 7, "abcd")

    class DownloadMessage:
        photo = False
        chat = object()
        chat_id = 10

        async def edit_text(self, text, **kwargs):
            pass

        async def edit_reply_markup(self, *, reply_markup):
            pass

        async def delete(self):
            pass

    entry = {
        "url": "https://youtu.be/x",
        "info": {"title": "Video", "extractor": "youtube"},
        "created": time.time(),
        "user_id": 42,
        "state_key": key,
        "cancel_callback_data": "cancel:7:abcd",
        "selection_started": True,
        "cancel_requested": True,
    }
    handlers._state[key] = entry
    cancelled_job_ids = []
    uploaded_paths = []

    async def fake_start(*args, **kwargs):
        return "job-1"

    async def terminal_cancel(job_id):
        cancelled_job_ids.append(job_id)
        raise ReclipDownloadError("Cancel request failed: 409", error_code="download_failed")

    async def fake_wait(*args, **kwargs):
        return {"status": "done", "file_path": str(downloaded_file)}

    async def fake_upload(chat, local_path, **kwargs):
        uploaded_paths.append(local_path)
        return 1

    async def ignore_event(**kwargs):
        pass

    monkeypatch.setattr(handlers, "start_download", fake_start)
    monkeypatch.setattr(handlers, "cancel_download", terminal_cancel)
    monkeypatch.setattr(handlers, "_wait_for_download_job", fake_wait)
    monkeypatch.setattr(handlers, "send_local_path", fake_upload)
    monkeypatch.setattr(handlers.event_client, "send_download_start", ignore_event)
    monkeypatch.setattr(handlers.event_client, "send_download_cancelled", ignore_event)
    monkeypatch.setattr(handlers.event_client, "send_download_done", ignore_event)
    monkeypatch.setattr(handlers, "DOWNLOADS_PATH", str(tmp_path))

    await handlers.download_and_send(
        SimpleNamespace(message=DownloadMessage()), entry, format="video", format_id=None,
    )

    assert cancelled_job_ids == ["job-1"]
    assert uploaded_paths == []
    assert entry["cancelled"] is True
    assert key not in handlers._state


@pytest.mark.asyncio
async def test_late_cancel_during_upload_does_not_cancel_reclip(monkeypatch, tmp_path):
    uploaded_file = tmp_path / "video.mp4"
    uploaded_file.touch()
    key = handlers._state_key(10, 7, "abcd")
    cancellation_query = FakeQuery(data="cancel:7:abcd", chat_id=10, message_id=7)

    class DownloadMessage:
        photo = False
        chat = object()
        chat_id = 10
        message_id = 7

        def __init__(self):
            self.button_edits = []

        async def edit_text(self, text, **kwargs):
            pass

        async def edit_reply_markup(self, *, reply_markup):
            self.button_edits.append(reply_markup)

        async def delete(self):
            pass

    message = DownloadMessage()
    cancellation_query.message = message
    entry = {
        "url": "https://youtu.be/x",
        "info": {"title": "Video", "extractor": "youtube"},
        "created": time.time(),
        "user_id": 42,
        "cancel_callback_data": "cancel:7:abcd",
    }
    handlers._state[key] = entry
    cancelled_job_ids = []

    async def fake_start(*args, **kwargs):
        return "job-1"

    async def fake_wait(*args, **kwargs):
        return {"status": "done", "file_path": str(uploaded_file)}

    async def ignore_event(**kwargs):
        pass

    async def fake_cancel(job_id):
        cancelled_job_ids.append(job_id)

    async def fake_upload(*args, **kwargs):
        assert entry["upload_started"] is True
        await handlers.cancel_callback(FakeUpdate(cancellation_query), None)
        return 1

    monkeypatch.setattr(handlers, "start_download", fake_start)
    monkeypatch.setattr(handlers, "_wait_for_download_job", fake_wait)
    monkeypatch.setattr(handlers, "send_local_path", fake_upload)
    monkeypatch.setattr(handlers, "cancel_download", fake_cancel)
    monkeypatch.setattr(handlers.event_client, "send_download_start", ignore_event)
    monkeypatch.setattr(handlers.event_client, "send_download_done", ignore_event)
    monkeypatch.setattr(handlers, "DOWNLOADS_PATH", str(tmp_path))

    await handlers.download_and_send(
        SimpleNamespace(message=message), entry, format="video", format_id=None,
    )

    assert cancelled_job_ids == []
    assert message.button_edits == [None]


@pytest.mark.asyncio
async def test_repeated_cancel_does_not_start_download(monkeypatch):
    query = FakeQuery(data="cancel:7:abcd", chat_id=10, message_id=7)
    started = []

    def fail_if_started(*args, **kwargs):
        started.append((args, kwargs))

    monkeypatch.setattr(handlers.asyncio, "create_task", fail_if_started)

    await handlers.cancel_callback(FakeUpdate(query), None)

    assert started == []


@pytest.mark.asyncio
async def test_authorized_cancel_callback_keeps_two_argument_handler_contract():
    query = FakeQuery(data="cancel:7:abcd", chat_id=10, message_id=7)
    handlers._state[handlers._state_key(10, 7, "abcd")] = {
        "created": time.time(), "user_id": 42,
    }
    callback = handlers._authorized_callback(handlers.cancel_callback, frozenset({42}))

    await callback(FakeUpdate(query), None)

    assert handlers._state == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("callback_name", "data"),
    [
        ("format_callback", "fmt:7:abcd:audio"),
        ("format_callback", "fmt:7:abcd:back"),
        ("quality_callback", "qty:7:abcd:22"),
        ("russian_quality_callback", "ruqty:7:abcd:720"),
        ("cancel_callback", "cancel:7:abcd"),
    ],
)
async def test_allowlisted_non_owner_cannot_operate_another_users_card(
    monkeypatch, callback_name, data,
):
    key = handlers._state_key(10, 7, "abcd")
    entry = {
        "url": "https://youtu.be/x",
        "info": {
            "title": "Video",
            "formats": [{"id": "22", "height": 720, "label": "720p"}],
            "russian_audio": {
                "available": True,
                "formats": [{"height": 720, "label": "720p"}],
            },
        },
        "created": time.time(),
        "user_id": 42,
    }
    handlers._state[key] = entry
    query = FakeQuery(data=data, chat_id=10, message_id=7, user_id=7)
    old_markup = object()
    query.reply_markup = old_markup
    downloads = []
    cancellations = []

    async def capture_download(*args, **kwargs):
        downloads.append((args, kwargs))

    async def capture_cancel(job_id):
        cancellations.append(job_id)
        return {"job_id": job_id, "status": "cancelled"}

    monkeypatch.setattr(handlers, "download_and_send", capture_download)
    monkeypatch.setattr(handlers, "cancel_download", capture_cancel)
    callback = handlers._authorized_callback(
        getattr(handlers, callback_name), frozenset({7, 42}),
    )

    await callback(FakeUpdate(query, user_id=7), None)
    await asyncio.sleep(0)

    assert query.answer_count == 1
    assert downloads == []
    assert cancellations == []
    assert handlers._state[key] is entry
    assert entry.get("selection_started") is not True
    assert entry.get("cancel_requested") is not True
    assert query.edited_text is None
    assert query.edited_caption is None
    assert query.reply_markup is old_markup


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("callback", "data"),
    [
        (handlers.format_callback, "fmt:7:abcd:audio"),
        (handlers.quality_callback, "qty:7:abcd:22"),
        (handlers.russian_quality_callback, "ruqty:7:abcd:720"),
    ],
)
async def test_expired_selection_callbacks_remove_stale_controls(callback, data):
    query = FakeQuery(data=data, chat_id=10, message_id=7)
    query.reply_markup = object()

    await callback(FakeUpdate(query), None)

    assert query.edited_text == "Время выбора истекло. Отправьте ссылку ещё раз."
    assert query.reply_markup is None


@pytest.mark.asyncio
async def test_expired_cancel_callback_only_removes_stale_controls():
    query = FakeQuery(data="cancel:7:abcd", chat_id=10, message_id=7)
    query.reply_markup = object()

    await handlers.cancel_callback(FakeUpdate(query), None)

    assert query.edited_text is None
    assert query.reply_markup is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("callback", "data", "info"),
    [
        ("format_callback", "fmt:7:abcd:audio", {"title": "Video"}),
        ("format_callback", "fmt:7:abcd:video", {"title": "Video", "formats": []}),
        (
            "quality_callback", "qty:7:abcd:22",
            {"title": "Video", "formats": [{"id": "22", "height": 720}]},
        ),
        (
            "russian_quality_callback",
            "ruqty:7:abcd:720",
            {"title": "Video", "russian_audio": {"formats": [{"height": 720}]}},
        ),
    ],
)
async def test_final_selection_starts_one_download_and_keeps_active_cancellation_state(
    monkeypatch, callback, data, info
):
    calls = []
    query = FakeQuery(data=data, chat_id=10, message_id=7)
    handlers._state[handlers._state_key(10, 7, "abcd")] = {
        "url": "https://youtu.be/x", "info": info, "created": time.time(), "user_id": 42,
    }

    async def fake_download(*args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(handlers, "download_and_send", fake_download)

    handler = getattr(handlers, callback)
    await handler(FakeUpdate(query), None)
    await handler(FakeUpdate(query), None)
    await asyncio.sleep(0)

    assert len(calls) == 1
    active_entry = handlers._state[handlers._state_key(10, 7, "abcd")]
    assert active_entry["selection_started"] is True


@pytest.mark.asyncio
async def test_russian_quality_callback_passes_language_and_integer_height(monkeypatch):
    calls = []
    entry = {
        "url": "https://youtu.be/x",
        "info": {"title": "Video"},
        "created": time.time(),
        "user_id": 42,
    }
    query = FakeQuery(data="ruqty:7:abcd:720", chat_id=10, message_id=7)
    handlers._state[handlers._state_key(10, 7, "abcd")] = entry

    async def fake_download(query_arg, entry_arg, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(handlers, "download_and_send", fake_download)

    await handlers.russian_quality_callback(FakeUpdate(query), None)
    await asyncio.sleep(0)

    assert calls == [{
        "format": "video", "format_id": None,
        "audio_language": "ru", "height": 720,
    }]
    assert entry["retry_intent"]["audio_mode"] == "ru_if_available"


@pytest.mark.asyncio
async def test_russian_quality_callback_rejects_malformed_height():
    query = FakeQuery(data="ruqty:7:abcd:not-a-height", chat_id=10, message_id=7)
    handlers._state[handlers._state_key(10, 7, "abcd")] = {
        "url": "https://youtu.be/x",
        "info": {"title": "Video"},
        "created": time.time(),
        "user_id": 42,
    }

    await handlers.russian_quality_callback(FakeUpdate(query), None)

    assert query.edited_text == "Время выбора истекло. Отправьте ссылку ещё раз."


@pytest.mark.asyncio
async def test_russian_quality_callback_rejects_malformed_message_id():
    query = FakeQuery(data="ruqty:not-an-id:abcd:720", chat_id=10, message_id=7)

    await handlers.russian_quality_callback(FakeUpdate(query), None)

    assert query.edited_text == "Время выбора истекло. Отправьте ссылку ещё раз."


@pytest.mark.asyncio
async def test_russian_quality_callback_passes_no_height_for_best(monkeypatch):
    calls = []
    query = FakeQuery(data="ruqty:7:abcd:best", chat_id=10, message_id=7)
    handlers._state[handlers._state_key(10, 7, "abcd")] = {
        "url": "https://youtu.be/x",
        "info": {"title": "Video"},
        "created": time.time(),
        "user_id": 42,
    }

    async def fake_download(query_arg, entry_arg, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(handlers, "download_and_send", fake_download)

    await handlers.russian_quality_callback(FakeUpdate(query), None)
    await asyncio.sleep(0)

    assert calls == [{
        "format": "video", "format_id": None,
        "audio_language": "ru", "height": None,
    }]


@pytest.mark.asyncio
async def test_russian_quality_callback_expires_stale_session():
    query = FakeQuery(data="ruqty:7:abcd:720", chat_id=10, message_id=7)
    handlers._state[handlers._state_key(10, 7, "abcd")] = {
        "url": "https://youtu.be/x",
        "info": {"title": "Video"},
        "created": 0,
        "user_id": 42,
    }

    await handlers.russian_quality_callback(FakeUpdate(query), None)

    assert query.edited_text == "Время выбора истекло. Отправьте ссылку ещё раз."


@pytest.mark.asyncio
async def test_russian_format_callback_rejects_missing_russian_formats():
    query = FakeQuery(data="fmt:7:abcd:video_ru", chat_id=10, message_id=7)
    query.reply_markup = object()
    handlers._state[handlers._state_key(10, 7, "abcd")] = {
        "url": "https://youtu.be/x",
        "info": {"title": "Video", "russian_audio": {"available": True, "formats": []}},
        "created": time.time(),
        "user_id": 42,
    }

    await handlers.format_callback(FakeUpdate(query), None)

    assert query.edited_text == "Русская дорожка недоступна для этого видео."
    assert query.reply_markup is None


@pytest.mark.asyncio
async def test_russian_download_forwards_height_and_reports_selected_quality(monkeypatch, tmp_path):
    calls = {"start": [], "events": []}
    downloaded_file = tmp_path / "video.mp4"
    downloaded_file.touch()

    class DownloadMessage:
        photo = False
        chat = object()
        chat_id = 10

        async def edit_text(self, text):
            pass

    async def fake_start(url, format, format_id, title, **kwargs):
        calls["start"].append((url, format, format_id, title, kwargs))
        return "job-1"

    async def fake_wait(job_id, message, entry):
        return {"status": "done", "file_path": str(downloaded_file)}

    async def fake_event_start(**kwargs):
        calls["events"].append(kwargs)

    async def fake_upload(*args, **kwargs):
        return 12

    async def ignore_done(**kwargs):
        pass

    monkeypatch.setattr(handlers, "start_download", fake_start)
    monkeypatch.setattr(handlers, "_wait_for_download_job", fake_wait)
    monkeypatch.setattr(handlers.event_client, "send_download_start", fake_event_start)
    monkeypatch.setattr(handlers.event_client, "send_download_done", ignore_done)
    monkeypatch.setattr(handlers, "send_local_path", fake_upload)
    monkeypatch.setattr(handlers, "DOWNLOADS_PATH", str(tmp_path))

    await handlers.download_and_send(
        SimpleNamespace(message=DownloadMessage()),
        {
            "url": "https://youtu.be/x",
            "info": {"title": "Video", "extractor": "youtube"},
            "created": time.time(),
            "user_id": 1,
        },
        format="video",
        format_id=None,
        audio_language="ru",
        height=720,
    )

    assert calls["start"] == [
        ("https://youtu.be/x", "video", None, "Video", {"audio_language": "ru", "height": 720})
    ]
    assert calls["events"] == [{
        "job_id": "job-1",
        "user_id": 1,
        "username": "1",
        "chat_id": 10,
        "url": "https://youtu.be/x",
        "platform": "youtube",
        "format": "video",
        "quality": "720",
        "title": "Video",
    }]


def test_register_handlers_registers_selection_callbacks():
    registered = []

    class Application:
        def add_handler(self, handler):
            registered.append(handler)

    handlers.register_handlers(Application(), preference_store=None, allowed_user_ids=frozenset({1}))

    callbacks = [
        handler for handler in registered
        if isinstance(handler, handlers.CallbackQueryHandler)
    ]

    assert {(handler.pattern.pattern, handler.callback.__wrapped__) for handler in callbacks} == {
        ("^fmt:", handlers.format_callback),
        ("^qty:", handlers.quality_callback),
        ("^ruqty:", handlers.russian_quality_callback),
        ("^cancel:", handlers.cancel_callback),
        ("^retry:", handlers.retry_callback),
        ("^settings:", handlers.settings_callback),
    }
