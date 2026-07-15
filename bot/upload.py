import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


class TelegramUploadError(RuntimeError):
    pass


async def send_local_path(
    chat,
    path: Path,
    *,
    caption: str,
    video_meta: dict | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    absolute_path = path.resolve(strict=True)
    file_size = absolute_path.stat().st_size
    last_error = None

    for attempt in range(2):
        try:
            if video_meta is not None and absolute_path.suffix.lower() == ".mp4":
                duration = video_meta.get("duration")
                await chat.send_video(
                    video=absolute_path,
                    caption=caption,
                    supports_streaming=True,
                    width=video_meta.get("width"),
                    height=video_meta.get("height"),
                    duration=int(duration) if duration else None,
                )
            else:
                await chat.send_document(document=absolute_path, caption=caption)
            break
        except Exception as error:
            last_error = error
            if attempt == 0:
                await sleep(1)
    else:
        raise TelegramUploadError("Telegram upload failed after 2 attempts") from last_error

    try:
        absolute_path.unlink()
    except OSError:
        logger.warning("Telegram delivery succeeded but cleanup failed: %s", absolute_path)
    return file_size
