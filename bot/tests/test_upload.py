import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from upload import TelegramUploadError, send_local_path


async def no_wait(_seconds):
    return None


@pytest.mark.asyncio
async def test_video_uses_absolute_path_and_deletes_after_success(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"video")
    chat = AsyncMock()

    size = await send_local_path(
        chat,
        path,
        caption="clip",
        video_meta={"width": 1920, "height": 1080, "duration": 12},
        sleep=no_wait,
    )

    assert size == 5
    assert not path.exists()
    sent_path = chat.send_video.await_args.kwargs["video"]
    assert isinstance(sent_path, Path)
    assert sent_path.is_absolute()
    chat.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_document_uses_absolute_path_and_deletes_after_success(tmp_path):
    path = tmp_path / "clip.mp3"
    path.write_bytes(b"audio")
    chat = AsyncMock()

    await send_local_path(chat, path, caption="clip", sleep=no_wait)

    sent_path = chat.send_document.await_args.kwargs["document"]
    assert isinstance(sent_path, Path)
    assert sent_path.is_absolute()
    assert not path.exists()


@pytest.mark.asyncio
async def test_final_upload_error_keeps_file_for_age_cleanup(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"keep-me")
    chat = AsyncMock()
    chat.send_video.side_effect = RuntimeError("telegram unavailable")

    with pytest.raises(TelegramUploadError, match="after 2 attempts"):
        await send_local_path(
            chat,
            path,
            caption="clip",
            video_meta={"duration": 1},
            sleep=no_wait,
        )

    assert chat.send_video.await_count == 2
    assert path.exists()
    assert path.read_bytes() == b"keep-me"


@pytest.mark.asyncio
async def test_cleanup_error_does_not_resend_delivered_file(tmp_path, monkeypatch):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"delivered")
    chat = AsyncMock()

    def fail_unlink(_self):
        raise OSError("temporary local file delete failure")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    size = await send_local_path(
        chat,
        path,
        caption="clip",
        video_meta={"duration": 1},
        sleep=no_wait,
    )

    assert size == len(b"delivered")
    assert chat.send_video.await_count == 1
    assert path.exists()
