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


async def poll_status(job_id: str, *, timeout: float = 10.0) -> dict:
    try:
        async with _client() as client:
            resp = await client.get(f"/api/status/{job_id}", timeout=timeout)
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
    job_id: str,
    on_status=None,
    *,
    sleep=asyncio.sleep,
    monotonic=time.monotonic,
    wall_clock=time.time,
    initial_deadline: float | None = None,
) -> dict:
    """Wait for a terminal ReClip status while enforcing service-side deadline bounds."""
    deadline = initial_deadline if initial_deadline is not None else wall_clock() + JOB_TIMEOUT
    outage_started_at = None

    while True:
        remaining_deadline = deadline + JOB_DEADLINE_GRACE_SECONDS - wall_clock()
        if remaining_deadline <= 0:
            raise ReclipJobDeadlineExceeded(ReclipJobDeadlineExceeded.message)

        request_timeout = min(10.0, remaining_deadline)
        if outage_started_at is not None:
            outage_remaining = SERVICE_OUTAGE_LIMIT_SECONDS - (monotonic() - outage_started_at)
            if outage_remaining <= 0:
                raise ReclipServiceOutage(ReclipServiceOutage.message)
            request_timeout = min(request_timeout, outage_remaining)

        request_started_at = monotonic()
        try:
            # httpx's scalar timeout is applied to individual I/O operations.
            # Keep a separate total elapsed bound so a stalled transport cannot
            # keep this job alive beyond the service deadline or outage budget.
            async with asyncio.timeout(request_timeout):
                status = await poll_status(job_id, timeout=request_timeout)
        except ReclipJobLost:
            raise
        except (ReclipError, TimeoutError):
            now = monotonic()
            if outage_started_at is None:
                # A slow request is part of the outage, so count it from the
                # instant the request began rather than when it raised.
                outage_started_at = request_started_at
            if now - outage_started_at >= SERVICE_OUTAGE_LIMIT_SECONDS:
                raise ReclipServiceOutage(ReclipServiceOutage.message)
            remaining_deadline = deadline + JOB_DEADLINE_GRACE_SECONDS - wall_clock()
            outage_remaining = SERVICE_OUTAGE_LIMIT_SECONDS - (now - outage_started_at)
            sleep_for = min(POLL_INTERVAL_SECONDS, remaining_deadline, outage_remaining)
            if sleep_for <= 0:
                if outage_remaining <= 0:
                    raise ReclipServiceOutage(ReclipServiceOutage.message)
                raise ReclipJobDeadlineExceeded(ReclipJobDeadlineExceeded.message)
            await sleep(sleep_for)
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

        remaining_deadline = deadline + JOB_DEADLINE_GRACE_SECONDS - wall_clock()
        if remaining_deadline <= 0:
            raise ReclipJobDeadlineExceeded(ReclipJobDeadlineExceeded.message)
        await sleep(min(POLL_INTERVAL_SECONDS, remaining_deadline))
