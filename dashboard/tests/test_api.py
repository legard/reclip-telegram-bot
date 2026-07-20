"""Tests for the admin dashboard API routes."""
import json as _json
import os
import tempfile
import pytest

# Set env vars BEFORE any dashboard imports so db.py picks them up
_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("ADMIN_PASSWORD", "testpass123")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ["DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["DOWNLOADS_PATH"] = _tmpdir

from fastapi.testclient import TestClient
from main import create_app

app = create_app()
client = TestClient(app, raise_server_exceptions=True)


def _delete(path: str, body: dict, cookies: dict | None = None):
    """Helper: send DELETE with JSON body using client.request()."""
    kwargs = {
        "content": _json.dumps(body).encode(),
        "headers": {"content-type": "application/json"},
    }
    if cookies:
        kwargs["cookies"] = cookies
    return client.request("DELETE", path, **kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _login() -> dict:
    """POST /login with correct creds and return the response cookies."""
    resp = client.post(
        "/login",
        data={"username": "admin", "password": "testpass123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"Login failed: {resp.status_code} {resp.text}"
    return resp.cookies


# ---------------------------------------------------------------------------
# Event ingestion (no auth)
# ---------------------------------------------------------------------------

def test_event_download_start():
    resp = client.post("/api/events", json={
        "type": "download_start",
        "job_id": "job-start-1",
        "user_id": 42,
        "username": "alice",
        "chat_id": 99,
        "url": "https://example.com/video.mp4",
        "platform": "youtube",
        "format": "video",
        "quality": "best",
        "title": "Test Video",
    })
    assert resp.status_code == 200


def test_event_download_progress():
    # Start one first
    client.post("/api/events", json={
        "type": "download_start",
        "job_id": "job-progress-1",
        "user_id": 1,
        "username": "bob",
        "chat_id": 1,
        "url": "https://example.com/v.mp4",
        "platform": "youtube",
        "format": "video",
        "quality": "best",
        "title": "Progress Video",
    })
    resp = client.post("/api/events", json={
        "type": "download_progress",
        "job_id": "job-progress-1",
        "percent": 50.0,
        "speed": 1048576.0,
        "eta": 5.0,
        "downloaded_bytes": 524288,
        "total_bytes": 1048576,
    })
    assert resp.status_code == 200


def test_event_download_done():
    client.post("/api/events", json={
        "type": "download_start",
        "job_id": "job-done-1",
        "user_id": 2,
        "username": "carol",
        "chat_id": 2,
        "url": "https://example.com/done.mp4",
        "platform": "tiktok",
        "format": "video",
        "quality": "1080p",
        "title": "My Video",
    })
    resp = client.post("/api/events", json={
        "type": "download_done",
        "job_id": "job-done-1",
        "file_size_bytes": 1024000,
        "duration_seconds": 3.5,
        "filename": "video.mp4",
    })
    assert resp.status_code == 200


def test_event_download_error():
    client.post("/api/events", json={
        "type": "download_start",
        "job_id": "job-err-1",
        "user_id": 3,
        "username": "dave",
        "chat_id": 3,
        "url": "https://example.com/err.mp4",
        "platform": "youtube",
        "format": "video",
        "quality": "best",
        "title": "Error Video",
    })
    resp = client.post("/api/events", json={
        "type": "download_error",
        "job_id": "job-err-1",
        "error_message": "HTTP 403 forbidden",
    })
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Auth: dashboard-stats
# ---------------------------------------------------------------------------

def test_dashboard_stats_without_auth_returns_401():
    resp = client.get("/api/dashboard-stats")
    assert resp.status_code == 401


def test_dashboard_stats_with_auth_returns_200():
    cookies = _login()
    resp = client.get("/api/dashboard-stats", cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert "stats" in body
    stats = body["stats"]
    assert "downloads_today" in stats
    assert "active_users_24h" in stats
    assert "error_rate" in stats


# ---------------------------------------------------------------------------
# Chart data
# ---------------------------------------------------------------------------

def test_chart_data_without_auth_returns_401():
    fresh = TestClient(app, raise_server_exceptions=True)
    resp = fresh.get("/api/chart-data?range=1D")
    assert resp.status_code == 401


def test_chart_data_with_auth():
    cookies = _login()
    for range_key in ("1D", "7D", "1M", "1Y"):
        resp = client.get(f"/api/chart-data?range={range_key}", cookies=cookies)
        assert resp.status_code == 200, f"Failed for range={range_key}"
        body = resp.json()
        assert "labels" in body
        assert "values" in body


# ---------------------------------------------------------------------------
# Active downloads
# ---------------------------------------------------------------------------

def test_active_downloads_without_auth_returns_401():
    fresh = TestClient(app, raise_server_exceptions=True)
    resp = fresh.get("/api/active-downloads")
    assert resp.status_code == 401


def test_active_downloads_with_auth():
    # Start a job to make sure at least one is active
    client.post("/api/events", json={
        "type": "download_start",
        "job_id": "job-active-1",
        "user_id": 10,
        "username": "eve",
        "chat_id": 10,
        "url": "https://example.com/active.mp4",
        "platform": "instagram",
        "format": "video",
        "quality": "best",
        "title": "Active Video",
    })
    cookies = _login()
    resp = client.get("/api/active-downloads", cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    job_ids = [d["job_id"] for d in body]
    assert "job-active-1" in job_ids


def test_active_download_exposes_current_stage_and_percent():
    """The active-download API retains the stage from lifecycle events."""
    job_id = "job-active-stage-1"
    client.post("/api/events", json={
        "type": "download_start",
        "job_id": job_id,
        "user_id": 11,
        "username": "frank",
        "chat_id": 11,
        "url": "https://example.com/staged.mp4",
        "platform": "youtube",
        "stage": "downloading",
    })
    client.post("/api/events", json={
        "type": "download_progress",
        "job_id": job_id,
        "stage": "postprocessing",
        "percent": 83.5,
    })

    response = client.get("/api/active-downloads", cookies=_login())
    assert response.status_code == 200
    active_job = next(item for item in response.json() if item["job_id"] == job_id)
    assert active_job["stage"] == "postprocessing"
    assert active_job["percent"] == 83.5


def test_active_downloads_ui_shows_stage_and_percent_progress():
    """The table renders a stage column and reads the API percent field."""
    from pathlib import Path

    dashboard_dir = Path(__file__).resolve().parents[1]
    template = (dashboard_dir / "templates" / "dashboard.html").read_text()
    javascript = (dashboard_dir / "static" / "dashboard.js").read_text()

    assert "<th>Stage</th>" in template
    assert "dl.percent != null" in javascript
    assert "dl.progress" not in javascript


# ---------------------------------------------------------------------------
# Task 5: Delete files
# ---------------------------------------------------------------------------

def test_delete_files():
    """Create a temp file, delete it via API, verify it's gone."""
    import pathlib

    downloads_path = pathlib.Path(_tmpdir)
    test_file = downloads_path / "test_delete_me.mp4"
    test_file.write_bytes(b"fake video content")
    assert test_file.exists()

    cookies = _login()
    resp = _delete("/api/files", {"paths": ["test_delete_me.mp4"]}, cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert "test_delete_me.mp4" in body["deleted"]
    assert not test_file.exists()


def test_delete_files_without_auth_returns_401():
    fresh = TestClient(app, raise_server_exceptions=True)
    resp = fresh.request(
        "DELETE",
        "/api/files",
        content=_json.dumps({"paths": ["something.mp4"]}).encode(),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 401


def test_purge_requires_confirm():
    """Sending empty/missing confirm should return 400."""
    cookies = _login()
    resp = _delete("/api/files/all", {}, cookies=cookies)
    assert resp.status_code == 400


def test_purge_with_confirm():
    """Create a temp file, purge all, verify it's gone."""
    import pathlib

    downloads_path = pathlib.Path(_tmpdir)
    test_file = downloads_path / "purge_me.mp4"
    test_file.write_bytes(b"content to be purged")
    assert test_file.exists()

    cookies = _login()
    resp = _delete("/api/files/all", {"confirm": "PURGE"}, cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert "deleted_count" in body
    assert not test_file.exists()
