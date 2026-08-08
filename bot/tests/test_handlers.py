import asyncio
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import handlers
from preferences import PreferenceStore


def button_texts(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def button_callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


class FakeQuery:
    def __init__(self, *, data, chat_id, message_id, photo=False):
        self.data = data
        self.message = SimpleNamespace(chat_id=chat_id, message_id=message_id, photo=photo)
        self.reply_markup = None
        self.edited_text = None
        self.edited_caption = None

    async def answer(self):
        pass

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


@pytest.fixture(autouse=True)
def clear_handler_state():
    handlers._state.clear()
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
        {"status": "downloading", "stage": "postprocessing", "progress": {"percent": 10}},
        {"status": "done", "file_path": "/downloads/video.mp4"},
    ]

    async def fake_wait_for_job(job_id, on_status):
        for status in statuses:
            await on_status(status)
        return statuses[-1]

    async def ignore_progress(**kwargs):
        pass

    monkeypatch.setattr(handlers, "wait_for_job", fake_wait_for_job)
    monkeypatch.setattr(handlers.event_client, "send_progress", ignore_progress)

    result = await handlers._wait_for_download_job("job-1", message)

    assert result["status"] == "done"
    assert message.edits == ["Downloading… 10%", "Post-processing…"]


def test_format_buttons_show_ru_only_when_available():
    hidden = handlers._build_format_buttons(7, "abcd", {"available": False, "formats": []})
    shown = handlers._build_format_buttons(
        7, "abcd", {"available": True, "formats": [{"height": 720, "label": "720p"}]}
    )

    assert button_texts(hidden) == ["MP4", "MP3", "Cancel"]
    assert button_texts(shown) == ["MP4", "MP4 • RU", "MP3", "Cancel"]
    assert "fmt:7:abcd:video_ru" in button_callbacks(shown)
    assert button_callbacks(hidden)[-1] == "cancel:7:abcd"


def test_russian_quality_buttons_store_height_not_format_id():
    markup = handlers._build_russian_quality_buttons(
        7, "abcd",
        [{"height": 1080, "label": "1080p"}, {"height": 720, "label": "720p"}],
    )

    assert button_callbacks(markup) == [
        "ruqty:7:abcd:1080", "ruqty:7:abcd:720", "ruqty:7:abcd:best", "cancel:7:abcd",
    ]
    assert button_texts(markup) == ["1080p", "720p", "Best quality", "Cancel"]


def test_quality_buttons_include_cancel():
    markup = handlers._build_quality_buttons(7, "abcd", [{"id": "22", "label": "720p"}])

    assert button_callbacks(markup) == ["qty:7:abcd:22", "qty:7:abcd:best", "cancel:7:abcd"]
    assert button_texts(markup) == ["720p", "Best quality", "Cancel"]


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
    assert handlers._download_intent("video", "480", "ru_if_available", {"formats": [{"height": 720}]}) == {
        "format": "video", "format_id": None, "audio_language": None, "height": 720,
        "fallback_note": "Русская дорожка недоступна — скачиваем оригинал.",
    }
    assert handlers._download_intent("audio", "720", "original", info_with_russian) == {
        "format": "audio", "format_id": None, "audio_language": None, "height": None,
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
async def test_saved_preferences_auto_start_semantic_download_without_session(monkeypatch, tmp_path):
    store = PreferenceStore(str(tmp_path / "bot.db"))
    await store.initialize()
    await store.save(42, format="video", quality="480", audio_mode="ru_if_available")
    started = []

    class StatusMessage:
        message_id = 7
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
        return {"title": "Video", "formats": [{"height": 720}]}

    async def fake_download(query, entry, **kwargs):
        started.append((entry, kwargs))

    monkeypatch.setattr(handlers, "get_info", fake_info)
    monkeypatch.setattr(handlers, "download_and_send", fake_download)

    await handlers.url_handler(update, None, store)
    await asyncio.sleep(0)

    assert started[0][1] == {
        "format": "video", "format_id": None, "audio_language": None, "height": 720,
    }
    assert status.text == "Русская дорожка недоступна — скачиваем оригинал."
    assert handlers._state == {}


@pytest.mark.asyncio
async def test_cancel_removes_session_and_replaces_text_card():
    key = handlers._state_key(10, 7, "abcd")
    handlers._state[key] = {"created": time.time()}
    query = FakeQuery(data="cancel:7:abcd", chat_id=10, message_id=7)

    await handlers.cancel_callback(FakeUpdate(query), None)

    assert key not in handlers._state
    assert query.edited_text == "Cancelled."
    assert query.reply_markup is None


@pytest.mark.asyncio
async def test_cancel_removes_session_and_replaces_photo_caption():
    key = handlers._state_key(10, 7, "abcd")
    handlers._state[key] = {"created": time.time()}
    query = FakeQuery(data="cancel:7:abcd", chat_id=10, message_id=7, photo=True)

    await handlers.cancel_callback(FakeUpdate(query), None)

    assert key not in handlers._state
    assert query.edited_caption == "Cancelled."
    assert query.reply_markup is None


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
    handlers._state[handlers._state_key(10, 7, "abcd")] = {"created": time.time()}
    callback = handlers._authorized_callback(handlers.cancel_callback, frozenset({42}))

    await callback(FakeUpdate(query), None)

    assert handlers._state == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("callback", "data", "info"),
    [
        ("format_callback", "fmt:7:abcd:audio", {"title": "Video"}),
        ("format_callback", "fmt:7:abcd:video", {"title": "Video", "formats": []}),
        ("quality_callback", "qty:7:abcd:22", {"title": "Video"}),
        (
            "russian_quality_callback",
            "ruqty:7:abcd:720",
            {"title": "Video", "russian_audio": {"formats": [{"height": 720}]}},
        ),
    ],
)
async def test_final_selection_starts_one_download_and_consumes_session(
    monkeypatch, callback, data, info
):
    calls = []
    query = FakeQuery(data=data, chat_id=10, message_id=7)
    handlers._state[handlers._state_key(10, 7, "abcd")] = {
        "url": "https://youtu.be/x", "info": info, "created": time.time(), "user_id": 1,
    }

    async def fake_download(*args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(handlers, "download_and_send", fake_download)

    handler = getattr(handlers, callback)
    await handler(FakeUpdate(query), None)
    await handler(FakeUpdate(query), None)
    await asyncio.sleep(0)

    assert len(calls) == 1
    assert handlers._state == {}


@pytest.mark.asyncio
async def test_russian_quality_callback_passes_language_and_integer_height(monkeypatch):
    calls = []
    entry = {
        "url": "https://youtu.be/x",
        "info": {"title": "Video"},
        "created": time.time(),
        "user_id": 1,
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


@pytest.mark.asyncio
async def test_russian_quality_callback_rejects_malformed_height():
    query = FakeQuery(data="ruqty:7:abcd:not-a-height", chat_id=10, message_id=7)
    handlers._state[handlers._state_key(10, 7, "abcd")] = {
        "url": "https://youtu.be/x",
        "info": {"title": "Video"},
        "created": time.time(),
        "user_id": 1,
    }

    await handlers.russian_quality_callback(FakeUpdate(query), None)

    assert query.edited_text == "Session expired. Please send the link again."


@pytest.mark.asyncio
async def test_russian_quality_callback_rejects_malformed_message_id():
    query = FakeQuery(data="ruqty:not-an-id:abcd:720", chat_id=10, message_id=7)

    await handlers.russian_quality_callback(FakeUpdate(query), None)

    assert query.edited_text == "Session expired. Please send the link again."


@pytest.mark.asyncio
async def test_russian_quality_callback_passes_no_height_for_best(monkeypatch):
    calls = []
    query = FakeQuery(data="ruqty:7:abcd:best", chat_id=10, message_id=7)
    handlers._state[handlers._state_key(10, 7, "abcd")] = {
        "url": "https://youtu.be/x",
        "info": {"title": "Video"},
        "created": time.time(),
        "user_id": 1,
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
        "user_id": 1,
    }

    await handlers.russian_quality_callback(FakeUpdate(query), None)

    assert query.edited_text == "Session expired. Please send the link again."


@pytest.mark.asyncio
async def test_russian_format_callback_rejects_missing_russian_formats():
    query = FakeQuery(data="fmt:7:abcd:video_ru", chat_id=10, message_id=7)
    handlers._state[handlers._state_key(10, 7, "abcd")] = {
        "url": "https://youtu.be/x",
        "info": {"title": "Video", "russian_audio": {"available": True, "formats": []}},
        "created": time.time(),
        "user_id": 1,
    }

    await handlers.format_callback(FakeUpdate(query), None)

    assert query.edited_text == "Session expired. Please send the link again."


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

    async def fake_wait(job_id, message):
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
        ("^settings:", handlers.settings_callback),
    }
