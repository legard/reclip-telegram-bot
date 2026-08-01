import asyncio
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import handlers


def button_texts(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def button_callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


class FakeQuery:
    def __init__(self, *, data, chat_id, message_id):
        self.data = data
        self.message = SimpleNamespace(chat_id=chat_id, message_id=message_id)
        self.reply_markup = None
        self.edited_text = None

    async def answer(self):
        pass

    async def edit_message_reply_markup(self, *, reply_markup):
        self.reply_markup = reply_markup

    async def edit_message_text(self, text):
        self.edited_text = text


class FakeUpdate:
    def __init__(self, query):
        self.callback_query = query


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

    assert button_texts(hidden) == ["MP4", "MP3"]
    assert button_texts(shown) == ["MP4", "MP4 • RU", "MP3"]
    assert "fmt:7:abcd:video_ru" in button_callbacks(shown)


def test_russian_quality_buttons_store_height_not_format_id():
    markup = handlers._build_russian_quality_buttons(
        7, "abcd",
        [{"height": 1080, "label": "1080p"}, {"height": 720, "label": "720p"}],
    )

    assert button_callbacks(markup) == [
        "ruqty:7:abcd:1080", "ruqty:7:abcd:720", "ruqty:7:abcd:best",
    ]
    assert button_texts(markup) == ["1080p", "720p", "Best quality"]


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


def test_register_handlers_registers_russian_quality_callback():
    registered = []

    class Application:
        def add_handler(self, handler):
            registered.append(handler)

    handlers.register_handlers(Application())

    assert any(
        isinstance(handler, handlers.CallbackQueryHandler)
        and handler.callback == handlers.russian_quality_callback
        and handler.pattern.pattern == "^ruqty:"
        for handler in registered
    )
