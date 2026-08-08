import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from presentation import RetryStore, StatusCard, error_text, escape_markdown, format_progress, is_retryable


class TextMessage:
    photo = False

    def __init__(self):
        self.text_edits = []
        self.button_edits = []
        self.deleted = False

    async def edit_text(self, text, **kwargs):
        self.text_edits.append((text, kwargs))

    async def edit_reply_markup(self, reply_markup=None):
        self.button_edits.append(reply_markup)

    async def delete(self):
        self.deleted = True


class PhotoMessage:
    photo = [object()]

    def __init__(self):
        self.caption_edits = []

    async def edit_caption(self, caption, **kwargs):
        self.caption_edits.append((caption, kwargs))


def test_error_text_hides_service_detail_and_marks_only_retryable_codes():
    assert "timeout" not in error_text("network").lower()
    assert is_retryable("network") is True
    assert is_retryable("auth_required") is False
    assert error_text("unknown-backend-detail") == error_text("download_failed")


def test_progress_and_markdown_are_russian_and_safe():
    assert format_progress({"status": "downloading", "progress": {"percent": 12.4}}) == "Загрузка: 12%"
    assert format_progress({"status": "downloading", "stage": "postprocessing"}) == "Обработка файла…"
    assert escape_markdown("a_b!") == "a\\_b\\!"


@pytest.mark.asyncio
async def test_card_uses_caption_for_photo_and_suppresses_duplicate_progress():
    message = PhotoMessage()
    card = StatusCard(message)

    await card.replace("Загрузка: 10%")
    await card.replace("Загрузка: 10%")

    assert [caption for caption, _ in message.caption_edits] == ["Загрузка: 10%"]


@pytest.mark.asyncio
async def test_completed_card_is_deleted_or_falls_back_to_russian_done_text():
    message = TextMessage()
    card = StatusCard(message)

    await card.complete()

    assert message.deleted is True
    assert message.text_edits == []


@pytest.mark.asyncio
async def test_completed_card_falls_back_when_telegram_delete_fails():
    class UndeletableMessage(TextMessage):
        async def delete(self):
            raise RuntimeError("Telegram refused deletion")

    message = UndeletableMessage()
    await StatusCard(message).complete()

    assert message.text_edits == [("Готово", {"reply_markup": None})]


@pytest.mark.asyncio
async def test_thumbnail_promotion_deletes_text_card_before_sending_photo():
    events = []

    class PromotableMessage(TextMessage):
        async def delete(self):
            events.append("delete")

        async def reply_photo(self, **kwargs):
            events.append("photo")
            return PhotoMessage()

    await StatusCard(PromotableMessage()).show_info("*Видео*", photo="https://example.test/thumb.jpg")

    assert events == ["delete", "photo"]


def test_retry_store_expires_and_keeps_only_semantic_intent():
    now = [1000.0]
    store = RetryStore(now=lambda: now[0])

    token = store.put({
        "url": "https://youtu.be/x", "format": "video", "quality": "720",
        "audio_mode": "original", "format_id": "22", "height": 720,
    })

    assert store.get(token) == {
        "url": "https://youtu.be/x", "format": "video", "quality": "720",
        "audio_mode": "original",
    }
    now[0] += 24 * 60 * 60 + 1
    assert store.get(token) is None


def test_retry_store_consumes_a_token_only_once():
    store = RetryStore()
    token = store.put({
        "url": "https://youtu.be/x", "format": "video", "quality": "720",
        "audio_mode": "original",
    })

    assert store.take(token)["url"] == "https://youtu.be/x"
    assert store.take(token) is None
