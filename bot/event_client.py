import logging
import os
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://dashboard:8080")
_TIMEOUT = 2.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def send_download_start(
    job_id: str,
    user_id: int,
    username: str,
    chat_id: int,
    url: str,
    platform: str,
    format: str,
    quality: str,
    title: str,
) -> None:
    try:
        payload = {
            "type": "download_start",
            "ts": _now_iso(),
            "job_id": job_id,
            "user_id": user_id,
            "username": username,
            "chat_id": chat_id,
            "url": url,
            "platform": platform,
            "format": format,
            "quality": quality,
            "title": title,
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            await client.post(f"{DASHBOARD_URL}/api/events", json=payload)
    except Exception as e:
        logger.debug("event_client.send_download_start failed: %s", e)


async def send_progress(
    job_id: str,
    percent: float | None,
    speed: float | None,
    eta: float | None,
    downloaded_bytes: int | None,
    total_bytes: int | None,
    stage: str | None = None,
) -> None:
    try:
        payload = {
            "type": "download_progress",
            "ts": _now_iso(),
            "job_id": job_id,
            "percent": percent,
            "speed": speed,
            "eta": eta,
            "downloaded_bytes": downloaded_bytes,
            "total_bytes": total_bytes,
            "stage": stage,
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            await client.post(f"{DASHBOARD_URL}/api/events", json=payload)
    except Exception as e:
        logger.debug("event_client.send_progress failed: %s", e)


async def send_download_done(
    job_id: str,
    file_size_bytes: int,
    duration_seconds: float,
    filename: str,
) -> None:
    try:
        payload = {
            "type": "download_done",
            "ts": _now_iso(),
            "job_id": job_id,
            "file_size_bytes": file_size_bytes,
            "duration_seconds": duration_seconds,
            "filename": filename,
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            await client.post(f"{DASHBOARD_URL}/api/events", json=payload)
    except Exception as e:
        logger.debug("event_client.send_download_done failed: %s", e)


async def send_download_error(
    job_id: str,
    error_message: str,
) -> None:
    try:
        payload = {
            "type": "download_error",
            "ts": _now_iso(),
            "job_id": job_id,
            "error_message": error_message,
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            await client.post(f"{DASHBOARD_URL}/api/events", json=payload)
    except Exception as e:
        logger.debug("event_client.send_download_error failed: %s", e)
