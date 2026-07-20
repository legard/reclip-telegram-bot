import os
import logging
import asyncio
import inspect
import time
from datetime import datetime
import httpx

logger = logging.getLogger(__name__)

RECLIP_URL = os.environ.get("RECLIP_URL", "http://reclip:8899")
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", os.environ.get("DOWNLOAD_TIMEOUT", 9000)))
POLL_INTERVAL_SECONDS = 2
SERVICE_OUTAGE_LIMIT_SECONDS = 60
JOB_DEADLINE_GRACE_SECONDS = 60


class ReclipError(Exception):
    pass


class ReclipInfoError(ReclipError):
    pass


class ReclipDownloadError(ReclipError):
    pass


class ReclipServiceDown(ReclipError):
    pass


class ReclipJobLost(ReclipError):
    """The in-memory ReClip job disappeared, usually after a service restart."""

    message = "Download interrupted because the service restarted. Please retry."


class ReclipServiceOutage(ReclipError):
    message = "Download service unavailable for more than 60 seconds."


class ReclipJobDeadlineExceeded(ReclipError):
    message = "Download service did not finalize the job within its deadline."


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=RECLIP_URL)


async def get_info(url: str) -> dict:
    try:
        async with _client() as client:
            resp = await client.post("/api/info", json={"url": url}, timeout=60.0)
            resp.raise_for_status()
            data = resp.json()
            return data
    except httpx.ConnectError:
        raise ReclipServiceDown("Cannot reach reclip service")
    except httpx.TimeoutException:
        raise ReclipInfoError("Info request timed out")
    except httpx.HTTPStatusError as e:
        raise ReclipInfoError(f"Info request failed: {e.response.status_code}")
    except Exception as e:
        raise ReclipInfoError(f"Info request failed: {e}")


async def start_download(url: str, format: str, format_id: str | None, title: str) -> str:
    payload = {"url": url, "format": format, "title": title}
    if format_id:
        payload["format_id"] = format_id
    try:
        async with _client() as client:
            resp = await client.post("/api/download", json=payload, timeout=60.0)
            resp.raise_for_status()
            data = resp.json()
            return data["job_id"]
    except httpx.ConnectError:
        raise ReclipServiceDown("Cannot reach reclip service")
    except httpx.TimeoutException:
        raise ReclipDownloadError("Download request timed out")
    except httpx.HTTPStatusError as e:
        raise ReclipDownloadError(f"Download request failed: {e.response.status_code}")
    except (KeyError, ValueError) as e:
        raise ReclipDownloadError(f"Malformed response: {e}")
    except ReclipError:
        raise
    except Exception as e:
        raise ReclipDownloadError(f"Download request failed: {e}")


async def poll_status(job_id: str) -> dict:
    try:
        async with _client() as client:
            resp = await client.get(f"/api/status/{job_id}", timeout=10.0)
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        raise ReclipServiceDown("Cannot reach reclip service")
    except httpx.TimeoutException:
        raise ReclipError("Status request timed out")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise ReclipJobLost(ReclipJobLost.message)
        raise ReclipError(f"Status request failed: {e.response.status_code}")
    except Exception as e:
        raise ReclipError(f"Status request failed: {e}")


def _deadline_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


async def wait_for_job(
    job_id: str, on_status=None, *, sleep=asyncio.sleep, monotonic=time.monotonic
) -> dict:
    """Wait for a terminal ReClip status while enforcing service-side deadline bounds."""
    deadline = time.time() + JOB_TIMEOUT
    outage_started_at = None

    while time.time() <= deadline + JOB_DEADLINE_GRACE_SECONDS:
        await sleep(POLL_INTERVAL_SECONDS)
        try:
            status = await poll_status(job_id)
        except ReclipJobLost:
            raise
        except ReclipError:
            now = monotonic()
            if outage_started_at is None:
                outage_started_at = now
            if now - outage_started_at > SERVICE_OUTAGE_LIMIT_SECONDS:
                raise ReclipServiceOutage(ReclipServiceOutage.message)
            continue

        outage_started_at = None
        server_deadline = _deadline_timestamp(status.get("deadline_at"))
        if server_deadline is not None:
            deadline = server_deadline

        if on_status is not None:
            callback_result = on_status(status)
            if inspect.isawaitable(callback_result):
                await callback_result

        if status.get("status") in ("done", "error"):
            return status

    raise ReclipJobDeadlineExceeded(ReclipJobDeadlineExceeded.message)
