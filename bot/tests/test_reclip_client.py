import pytest
import httpx
from unittest.mock import AsyncMock, patch

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from reclip_client import (
    get_info,
    start_download,
    poll_status,
    ReclipInfoError,
    ReclipDownloadError,
    ReclipServiceDown,
    ReclipError,
    ReclipJobLost,
    ReclipServiceOutage,
    ReclipJobDeadlineExceeded,
    wait_for_job,
)


@pytest.fixture
def mock_response():
    def _make(status_code=200, json_data=None):
        from unittest.mock import MagicMock
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status_code
        resp.json.return_value = json_data or {}
        if status_code >= 400:
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "error", request=MagicMock(), response=resp
            )
        return resp
    return _make


class TestGetInfo:
    @pytest.mark.asyncio
    async def test_success(self, mock_response):
        info = {
            "title": "Test Video",
            "thumbnail": "https://example.com/thumb.jpg",
            "duration": 120,
            "uploader": "TestUser",
            "extractor": "youtube",
            "formats": [{"id": "22", "label": "720p", "height": 720}],
        }
        resp = mock_response(200, info)
        with patch("reclip_client._client") as mock_client:
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            client.post = AsyncMock(return_value=resp)
            mock_client.return_value = client

            result = await get_info("https://youtube.com/watch?v=test")
            assert result["title"] == "Test Video"
            assert result["extractor"] == "youtube"

    @pytest.mark.asyncio
    async def test_connection_error(self):
        with patch("reclip_client._client") as mock_client:
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client.return_value = client

            with pytest.raises(ReclipServiceDown):
                await get_info("https://youtube.com/watch?v=test")

    @pytest.mark.asyncio
    async def test_timeout(self):
        with patch("reclip_client._client") as mock_client:
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
            mock_client.return_value = client

            with pytest.raises(ReclipInfoError, match="timed out"):
                await get_info("https://youtube.com/watch?v=test")

    @pytest.mark.asyncio
    async def test_http_error(self, mock_response):
        resp = mock_response(400, {"error": "Unsupported URL"})
        with patch("reclip_client._client") as mock_client:
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            client.post = AsyncMock(return_value=resp)
            mock_client.return_value = client

            with pytest.raises(ReclipInfoError):
                await get_info("https://invalid-site.com/nope")


class TestStartDownload:
    @pytest.mark.asyncio
    async def test_success(self, mock_response):
        resp = mock_response(200, {"job_id": "abc1234567"})
        with patch("reclip_client._client") as mock_client:
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            client.post = AsyncMock(return_value=resp)
            mock_client.return_value = client

            job_id = await start_download("https://youtube.com/watch?v=test", "video", "22", "Test")
            assert job_id == "abc1234567"

    @pytest.mark.asyncio
    async def test_service_down(self):
        with patch("reclip_client._client") as mock_client:
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client.return_value = client

            with pytest.raises(ReclipServiceDown):
                await start_download("https://youtube.com/watch?v=test", "video", None, "Test")


class TestPollStatus:
    @pytest.mark.asyncio
    async def test_done(self, mock_response):
        data = {
            "status": "done",
            "filename": "test.mp4",
            "file_path": "/downloads/abc1234567.mp4",
            "progress": None,
        }
        resp = mock_response(200, data)
        with patch("reclip_client._client") as mock_client:
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            client.get = AsyncMock(return_value=resp)
            mock_client.return_value = client

            result = await poll_status("abc1234567")
            assert result["status"] == "done"
            assert result["file_path"] == "/downloads/abc1234567.mp4"

    @pytest.mark.asyncio
    async def test_downloading_with_progress(self, mock_response):
        data = {
            "status": "downloading",
            "progress": {"percent": 45.2, "downloaded_bytes": 23000000, "total_bytes": 51000000},
        }
        resp = mock_response(200, data)
        with patch("reclip_client._client") as mock_client:
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            client.get = AsyncMock(return_value=resp)
            mock_client.return_value = client

            result = await poll_status("abc1234567")
            assert result["status"] == "downloading"
            assert result["progress"]["percent"] == 45.2

    @pytest.mark.asyncio
    async def test_error(self, mock_response):
        data = {"status": "error", "error": "Download timed out (5 min limit)"}
        resp = mock_response(200, data)
        with patch("reclip_client._client") as mock_client:
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            client.get = AsyncMock(return_value=resp)
            mock_client.return_value = client

            result = await poll_status("abc1234567")
            assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_not_found_means_job_was_lost_after_service_restart(self, mock_response):
        resp = mock_response(404)
        with patch("reclip_client._client") as mock_client:
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            client.get = AsyncMock(return_value=resp)
            mock_client.return_value = client

            with pytest.raises(ReclipJobLost, match="service restarted"):
                await poll_status("abc1234567")

    @pytest.mark.asyncio
    async def test_server_error_raises_status_error(self, mock_response):
        resp = mock_response(503)
        with patch("reclip_client._client") as mock_client:
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            client.get = AsyncMock(return_value=resp)
            mock_client.return_value = client

            with pytest.raises(ReclipError, match="Status request failed: 503"):
                await poll_status("abc1234567")


class TestWaitForJob:
    @pytest.mark.asyncio
    async def test_caps_poll_request_timeout_at_deadline_grace(self, monkeypatch):
        captured_timeouts = []

        async def unavailable(job_id, *, timeout):
            captured_timeouts.append(timeout)
            raise ReclipServiceDown("offline")

        clock = iter([1000.0, 1060.0])
        monkeypatch.setattr("reclip_client.poll_status", unavailable)

        with pytest.raises(ReclipServiceOutage):
            await wait_for_job(
                "job-1",
                sleep=AsyncMock(),
                monotonic=lambda: next(clock),
                wall_clock=lambda: 100.0,
                initial_deadline=45.0,
            )

        assert captured_timeouts == [5.0]

    @pytest.mark.asyncio
    async def test_counts_a_slow_failed_request_from_its_start(self, monkeypatch):
        poll_started = []
        monotonic_values = iter([0.0, 61.0])

        async def unavailable(job_id, *, timeout):
            poll_started.append(timeout)
            raise ReclipServiceDown("offline")

        monkeypatch.setattr("reclip_client.poll_status", unavailable)

        with pytest.raises(ReclipServiceOutage, match="more than 60 seconds"):
            await wait_for_job(
                "job-1",
                sleep=AsyncMock(),
                monotonic=lambda: next(monotonic_values),
                wall_clock=lambda: 100.0,
                initial_deadline=10_000.0,
            )

        assert poll_started == [10.0]

    @pytest.mark.asyncio
    async def test_does_not_sleep_past_deadline_grace(self, monkeypatch):
        sleeps = []

        async def downloading(job_id, *, timeout):
            return {"status": "downloading"}

        async def record_sleep(seconds):
            sleeps.append(seconds)

        wall_clock_values = iter([100.0, 104.0, 105.0])
        monkeypatch.setattr("reclip_client.poll_status", downloading)

        with pytest.raises(ReclipJobDeadlineExceeded):
            await wait_for_job(
                "job-1",
                sleep=record_sleep,
                wall_clock=lambda: next(wall_clock_values),
                initial_deadline=45.0,
            )

        assert sleeps == [1.0]

    @pytest.mark.asyncio
    async def test_waits_past_450_downloading_responses_until_done(self, monkeypatch):
        responses = [
            {
                "status": "downloading",
                "stage": "downloading",
                "deadline_at": "2099-01-01T00:00:00+00:00",
            }
            for _ in range(451)
        ]
        responses.append({"status": "done", "file_path": "/downloads/video.mp4"})

        async def fake_poll_status(job_id, *, timeout):
            return responses.pop(0)

        async def no_sleep(seconds):
            assert seconds == 2

        monkeypatch.setattr("reclip_client.poll_status", fake_poll_status)

        result = await wait_for_job("job-1", sleep=no_sleep)

        assert result["status"] == "done"
        assert responses == []

    @pytest.mark.asyncio
    async def test_reports_service_outage_after_sixty_seconds(self, monkeypatch):
        clock = iter([0, 60])
        calls = 0

        async def unavailable(job_id, *, timeout):
            nonlocal calls
            calls += 1
            raise ReclipServiceDown("offline")

        async def no_sleep(seconds):
            if calls >= 2:
                pytest.fail("wait_for_job did not stop at the 60-second outage limit")

        monkeypatch.setattr("reclip_client.poll_status", unavailable)

        with pytest.raises(ReclipServiceOutage, match="more than 60 seconds"):
            await wait_for_job("job-1", sleep=no_sleep, monotonic=lambda: next(clock))

    @pytest.mark.asyncio
    async def test_retries_server_error_and_returns_terminal_status(self, mock_response):
        unavailable = mock_response(503)
        done = mock_response(200, {"status": "done", "file_path": "/downloads/video.mp4"})
        with patch("reclip_client._client") as mock_client:
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            client.get = AsyncMock(side_effect=[unavailable, done])
            mock_client.return_value = client

            async def no_sleep(seconds):
                assert seconds == 2

            result = await wait_for_job("job-1", sleep=no_sleep)

        assert result["status"] == "done"
        assert client.get.await_count == 2
