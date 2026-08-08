"""Tests for dashboard/db.py"""
import asyncio
import os
import tempfile
import time

# Must set DB_PATH before importing db
_tmpdir = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmpdir, "test_reclip.db")

import db  # noqa: E402


_loop = asyncio.new_event_loop()


def run(coro):
    return _loop.run_until_complete(coro)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _job(suffix=""):
    return f"job-{suffix or int(time.time() * 1000)}"


async def _seed_download(job_id, platform="youtube", status="done", user_id=1,
                          username="alice", chat_id=100, url="https://example.com/v"):
    await db.insert_download_start(
        job_id=job_id, user_id=user_id, username=username,
        chat_id=chat_id, url=url, platform=platform,
    )
    if status == "done":
        await db.update_download_done(
            job_id=job_id, title="Test Video", format="mp4",
            quality="720p", file_size_bytes=1_000_000,
            download_duration_sec=3.5,
        )
    elif status == "error":
        await db.update_download_error(job_id=job_id, error_message="network timeout")


# ---------------------------------------------------------------------------
# Task 1 tests: CRUD
# ---------------------------------------------------------------------------

def test_insert_download_start():
    job_id = _job("start1")
    run(db.insert_download_start(
        job_id=job_id, user_id=42, username="bob",
        chat_id=999, url="https://youtube.com/watch?v=abc",
        platform="youtube",
    ))
    row = run(db.get_download_by_job_id(job_id))
    assert row is not None
    assert row["job_id"] == job_id
    assert row["status"] == "started"
    assert row["user_id"] == 42
    assert row["username"] == "bob"
    assert row["platform"] == "youtube"
    assert row["url"] == "https://youtube.com/watch?v=abc"


def test_update_download_done():
    job_id = _job("done1")
    run(db.insert_download_start(
        job_id=job_id, user_id=1, username="alice",
        chat_id=10, url="https://example.com",
        platform="tiktok",
    ))
    run(db.update_download_done(
        job_id=job_id, title="Cool Clip", format="mp4",
        quality="1080p", file_size_bytes=5_000_000,
        download_duration_sec=10.2,
    ))
    row = run(db.get_download_by_job_id(job_id))
    assert row["status"] == "done"
    assert row["title"] == "Cool Clip"
    assert row["format"] == "mp4"
    assert row["quality"] == "1080p"
    assert row["file_size_bytes"] == 5_000_000
    assert abs(row["download_duration_sec"] - 10.2) < 0.01
    assert row["completed_at"] is not None


def test_update_download_error():
    job_id = _job("err1")
    run(db.insert_download_start(
        job_id=job_id, user_id=7, username="charlie",
        chat_id=77, url="https://example.com",
        platform="instagram",
    ))
    run(db.update_download_error(job_id=job_id, error_message="403 Forbidden"))
    row = run(db.get_download_by_job_id(job_id))
    assert row["status"] == "error"
    assert row["error_message"] == "403 Forbidden"
    assert row["completed_at"] is not None


def test_update_download_cancelled_clears_error_message_and_sets_completion_time():
    job_id = _job("cancelled1")
    run(db.insert_download_start(
        job_id=job_id, user_id=7, username="charlie",
        chat_id=77, url="https://example.com", platform="instagram",
    ))
    run(db.update_download_cancelled(job_id=job_id))
    row = run(db.get_download_by_job_id(job_id))
    assert row["status"] == "cancelled"
    assert row["error_message"] is None
    assert row["completed_at"] is not None


def test_get_download_by_job_id_missing():
    row = run(db.get_download_by_job_id("nonexistent-job-id"))
    assert row is None


# ---------------------------------------------------------------------------
# Task 2 tests: stats queries
# ---------------------------------------------------------------------------

def test_get_dashboard_stats_empty():
    """Stats on a fresh DB (after inserts above, but we want structure check)."""
    stats = run(db.get_dashboard_stats())
    assert "downloads_today" in stats
    assert "downloads_yesterday" in stats
    assert "active_users_24h" in stats
    assert "error_rate" in stats
    assert "error_rate_yesterday" in stats
    assert "errors_today" in stats
    assert "total_today" in stats


def test_get_dashboard_stats_with_data():
    job_done = _job("stats-done")
    job_err = _job("stats-err")
    run(_seed_download(job_done, platform="youtube", status="done"))
    run(_seed_download(job_err, platform="youtube", status="error"))
    stats = run(db.get_dashboard_stats())
    # At least the downloads we inserted are counted
    assert stats["downloads_today"] >= 1
    assert stats["errors_today"] >= 1
    assert stats["total_today"] >= 2


def test_get_chart_data_ranges():
    for rng in ("1D", "7D", "1M", "1Y"):
        data = run(db.get_chart_data(rng))
        assert "labels" in data
        assert "values" in data
        assert "platforms" in data
        assert "top_users" in data
        assert isinstance(data["labels"], list)
        assert isinstance(data["values"], list)
        assert isinstance(data["platforms"], list)
        assert isinstance(data["top_users"], list)
        assert len(data["labels"]) == len(data["values"])


def test_get_chart_data_platform_top5():
    data = run(db.get_chart_data("7D"))
    assert len(data["platforms"]) <= 5
    for p in data["platforms"]:
        assert "platform" in p
        assert "count" in p


def test_get_chart_data_top_users():
    data = run(db.get_chart_data("7D"))
    assert len(data["top_users"]) <= 5
    for u in data["top_users"]:
        assert "username" in u
        assert "count" in u


def test_get_downloads_page_basic():
    result = run(db.get_downloads_page(page=1, per_page=10))
    assert "rows" in result
    assert "total" in result
    assert "pages" in result
    assert "page" in result
    assert result["page"] == 1
    assert isinstance(result["rows"], list)


def test_get_downloads_page_pagination():
    # Seed 5 more downloads
    for i in range(5):
        run(_seed_download(_job(f"page-{i}"), platform="youtube", status="done",
                           user_id=10 + i, username=f"user{i}"))
    result_p1 = run(db.get_downloads_page(page=1, per_page=3))
    assert len(result_p1["rows"]) == 3
    assert result_p1["total"] >= 5

    result_p2 = run(db.get_downloads_page(page=2, per_page=3))
    assert len(result_p2["rows"]) >= 1
    # No overlap between pages
    ids_p1 = {r["id"] for r in result_p1["rows"]}
    ids_p2 = {r["id"] for r in result_p2["rows"]}
    assert ids_p1.isdisjoint(ids_p2)


def test_get_downloads_page_filters():
    run(_seed_download(_job("filter-tw"), platform="twitter", status="done",
                       user_id=999, username="twitteruser"))
    result = run(db.get_downloads_page(page=1, per_page=50, platform="twitter"))
    assert all(r["platform"] == "twitter" for r in result["rows"])

    result_err = run(db.get_downloads_page(page=1, per_page=50, status="error"))
    assert all(r["status"] == "error" for r in result_err["rows"])

    result_user = run(db.get_downloads_page(page=1, per_page=50, user="twitteruser"))
    assert any(r["username"] == "twitteruser" for r in result_user["rows"])


def test_get_error_downloads():
    errors = run(db.get_error_downloads())
    assert isinstance(errors, list)
    for e in errors:
        assert e["status"] == "error"


def test_get_error_downloads_date_filter():
    from datetime import datetime, timezone, timedelta
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    errors = run(db.get_error_downloads(date_from=yesterday, date_to=tomorrow))
    assert isinstance(errors, list)
    # All errors we seeded today should appear
    for e in errors:
        assert e["status"] == "error"


def test_insert_and_get_disk_snapshot():
    run(db.insert_disk_snapshot(total_bytes=1_000_000_000, file_count=42))
    snap = run(db.get_latest_disk_snapshot())
    assert snap is not None
    assert snap["total_bytes"] == 1_000_000_000
    assert snap["file_count"] == 42
    assert snap["snapshot_at"] is not None


def test_get_latest_disk_snapshot_empty():
    # Use a separate temp DB to test empty state
    with tempfile.TemporaryDirectory() as d:
        old_path = os.environ["DB_PATH"]
        empty_path = os.path.join(d, "empty.db")
        os.environ["DB_PATH"] = empty_path
        # init_db will create schema on empty_path
        run(db.init_db())
        snap = run(db.get_latest_disk_snapshot())
        assert snap is None
        os.environ["DB_PATH"] = old_path


def test_disk_snapshot_latest_is_newest():
    run(db.insert_disk_snapshot(total_bytes=100, file_count=1))
    run(db.insert_disk_snapshot(total_bytes=200, file_count=2))
    run(db.insert_disk_snapshot(total_bytes=300, file_count=3))
    snap = run(db.get_latest_disk_snapshot())
    assert snap["total_bytes"] == 300
    assert snap["file_count"] == 3
