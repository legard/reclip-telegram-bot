"""Database layer for the reclip_bot admin dashboard.

Uses aiosqlite for async SQLite access with WAL journal mode.
DB path is read from the DB_PATH environment variable (default: /data/reclip_bot.db).
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, AsyncGenerator, Dict, List, Optional

import aiosqlite

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT UNIQUE NOT NULL,
    user_id INTEGER,
    username TEXT,
    chat_id INTEGER,
    url TEXT NOT NULL,
    title TEXT,
    platform TEXT,
    format TEXT,
    quality TEXT,
    file_size_bytes INTEGER,
    download_duration_sec REAL,
    status TEXT NOT NULL DEFAULT 'started',
    error_message TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS disk_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    total_bytes INTEGER NOT NULL,
    file_count INTEGER NOT NULL,
    snapshot_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _db_path() -> str:
    """Return current DB_PATH (re-read from env each call so tests can override)."""
    return os.environ.get("DB_PATH", "/data/reclip_bot.db")


def _utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@asynccontextmanager
async def _conn() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Open a WAL-mode connection, run schema migrations, yield, then close."""
    path = _db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    async with aiosqlite.connect(path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(_SCHEMA)
        await conn.commit()
        yield conn


# ---------------------------------------------------------------------------
# Public: schema init
# ---------------------------------------------------------------------------

async def init_db(path: Optional[str] = None) -> None:
    """Initialise (or migrate) the database schema at the given path.

    If path is provided it temporarily overrides the env var for this call.
    """
    if path is not None:
        old = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = path
    try:
        async with _conn():
            pass
    finally:
        if path is not None:
            if old is None:
                del os.environ["DB_PATH"]
            else:
                os.environ["DB_PATH"] = old


# ---------------------------------------------------------------------------
# Public: CRUD — downloads
# ---------------------------------------------------------------------------

async def insert_download_start(
    *,
    job_id: str,
    user_id: Optional[int],
    username: Optional[str],
    chat_id: Optional[int],
    url: str,
    platform: Optional[str] = None,
) -> None:
    """Record the start of a download job."""
    started_at = _utc_now_str()
    async with _conn() as conn:
        await conn.execute(
            """
            INSERT INTO downloads (job_id, user_id, username, chat_id, url, platform, started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, user_id, username, chat_id, url, platform, started_at),
        )
        await conn.commit()


async def update_download_done(
    *,
    job_id: str,
    title: Optional[str] = None,
    format: Optional[str] = None,
    quality: Optional[str] = None,
    file_size_bytes: Optional[int] = None,
    download_duration_sec: Optional[float] = None,
) -> None:
    """Mark a download as successfully completed."""
    completed_at = _utc_now_str()
    async with _conn() as conn:
        await conn.execute(
            """
            UPDATE downloads
            SET status='done', title=?, format=?, quality=?,
                file_size_bytes=?, download_duration_sec=?, completed_at=?
            WHERE job_id=?
            """,
            (title, format, quality, file_size_bytes, download_duration_sec,
             completed_at, job_id),
        )
        await conn.commit()


async def update_download_error(*, job_id: str, error_message: str) -> None:
    """Mark a download as failed."""
    completed_at = _utc_now_str()
    async with _conn() as conn:
        await conn.execute(
            """
            UPDATE downloads
            SET status='error', error_message=?, completed_at=?
            WHERE job_id=?
            """,
            (error_message, completed_at, job_id),
        )
        await conn.commit()


async def update_download_cancelled(*, job_id: str) -> None:
    """Mark a download as cancelled by the user, not as an error."""
    completed_at = _utc_now_str()
    async with _conn() as conn:
        await conn.execute(
            """
            UPDATE downloads
            SET status='cancelled', error_message=NULL, completed_at=?
            WHERE job_id=?
            """,
            (completed_at, job_id),
        )
        await conn.commit()


async def get_download_by_job_id(job_id: str) -> Optional[aiosqlite.Row]:
    """Return a single download row by job_id, or None."""
    async with _conn() as conn:
        async with conn.execute(
            "SELECT * FROM downloads WHERE job_id=?", (job_id,)
        ) as cur:
            return await cur.fetchone()


# ---------------------------------------------------------------------------
# Public: stats queries
# ---------------------------------------------------------------------------

async def get_dashboard_stats() -> Dict[str, Any]:
    """Return high-level dashboard statistics."""
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    async with _conn() as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM downloads WHERE status='done' AND started_at LIKE ?",
            (f"{today_str}%",),
        ) as cur:
            downloads_today = (await cur.fetchone())[0]

        async with conn.execute(
            "SELECT COUNT(*) FROM downloads WHERE status='done' AND started_at LIKE ?",
            (f"{yesterday_str}%",),
        ) as cur:
            downloads_yesterday = (await cur.fetchone())[0]

        async with conn.execute(
            "SELECT COUNT(*) FROM downloads WHERE started_at LIKE ?",
            (f"{today_str}%",),
        ) as cur:
            total_today = (await cur.fetchone())[0]

        async with conn.execute(
            "SELECT COUNT(*) FROM downloads WHERE status='error' AND started_at LIKE ?",
            (f"{today_str}%",),
        ) as cur:
            errors_today = (await cur.fetchone())[0]

        async with conn.execute(
            "SELECT COUNT(*) FROM downloads WHERE status='error' AND started_at LIKE ?",
            (f"{yesterday_str}%",),
        ) as cur:
            errors_yesterday = (await cur.fetchone())[0]

        async with conn.execute(
            "SELECT COUNT(*) FROM downloads WHERE started_at LIKE ?",
            (f"{yesterday_str}%",),
        ) as cur:
            total_yesterday = (await cur.fetchone())[0]

        since_24h = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        async with conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM downloads WHERE started_at >= ?",
            (since_24h,),
        ) as cur:
            active_users_24h = (await cur.fetchone())[0]

    error_rate = (errors_today / total_today * 100) if total_today else 0.0
    error_rate_yesterday = (errors_yesterday / total_yesterday * 100) if total_yesterday else 0.0

    return {
        "downloads_today": downloads_today,
        "downloads_yesterday": downloads_yesterday,
        "active_users_24h": active_users_24h,
        "error_rate": round(error_rate, 2),
        "error_rate_yesterday": round(error_rate_yesterday, 2),
        "errors_today": errors_today,
        "total_today": total_today,
    }


async def get_chart_data(range_key: str) -> Dict[str, Any]:
    """Return time-series chart data for the given range.

    range_key:
        "1D"  -> hourly buckets for the past 24 hours
        "7D"  -> daily buckets for the past 7 days
        "1M"  -> daily buckets for the past 30 days
        "1Y"  -> monthly buckets for the past 12 months
    """
    now = datetime.now(timezone.utc)

    if range_key == "1D":
        # Build list of datetime objects, one per hour, newest last
        bucket_dts = [(now - timedelta(hours=i)) for i in range(23, -1, -1)]
        labels = [dt.strftime("%H:00") for dt in bucket_dts]
        bucket_keys = [dt.strftime("%Y-%m-%d %H") for dt in bucket_dts]
        start_str = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        trunc_fmt = "%Y-%m-%d %H"
    elif range_key == "7D":
        bucket_dts = [(now - timedelta(days=i)) for i in range(6, -1, -1)]
        labels = [dt.strftime("%a %d") for dt in bucket_dts]
        bucket_keys = [dt.strftime("%Y-%m-%d") for dt in bucket_dts]
        start_str = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        trunc_fmt = "%Y-%m-%d"
    elif range_key == "1M":
        bucket_dts = [(now - timedelta(days=i)) for i in range(29, -1, -1)]
        labels = [dt.strftime("%b %d") for dt in bucket_dts]
        bucket_keys = [dt.strftime("%Y-%m-%d") for dt in bucket_dts]
        start_str = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        trunc_fmt = "%Y-%m-%d"
    else:  # 1Y
        # Monthly: go back 12 months (approximate with 30-day steps)
        bucket_dts = [
            (now.replace(day=1) - timedelta(days=30 * i))
            for i in range(11, -1, -1)
        ]
        labels = [dt.strftime("%b %Y") for dt in bucket_dts]
        bucket_keys = [dt.strftime("%Y-%m") for dt in bucket_dts]
        start_str = (now - timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
        trunc_fmt = "%Y-%m"

    async with _conn() as conn:
        async with conn.execute(
            f"""
            SELECT strftime('{trunc_fmt}', started_at) AS bucket, COUNT(*) AS cnt
            FROM downloads
            WHERE started_at >= ? AND status='done'
            GROUP BY bucket
            """,
            (start_str,),
        ) as cur:
            rows = await cur.fetchall()

        bucket_map: Dict[str, int] = {r["bucket"]: r["cnt"] for r in rows}
        values = [bucket_map.get(k, 0) for k in bucket_keys]

        async with conn.execute(
            """
            SELECT platform, COUNT(*) AS cnt
            FROM downloads
            WHERE started_at >= ? AND platform IS NOT NULL
            GROUP BY platform
            ORDER BY cnt DESC
            LIMIT 5
            """,
            (start_str,),
        ) as cur:
            platform_rows = await cur.fetchall()
        platforms = [{"platform": r["platform"], "count": r["cnt"]} for r in platform_rows]

        async with conn.execute(
            """
            SELECT username, COUNT(*) AS cnt
            FROM downloads
            WHERE started_at >= ? AND username IS NOT NULL
            GROUP BY username
            ORDER BY cnt DESC
            LIMIT 5
            """,
            (start_str,),
        ) as cur:
            user_rows = await cur.fetchall()
        top_users = [{"username": r["username"], "count": r["cnt"]} for r in user_rows]

    return {
        "labels": labels,
        "values": values,
        "platforms": platforms,
        "top_users": top_users,
    }


async def get_downloads_page(
    page: int = 1,
    per_page: int = 20,
    platform: Optional[str] = None,
    status: Optional[str] = None,
    user: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Dict[str, Any]:
    """Return paginated download history with optional filters."""
    conditions: List[str] = []
    params: List[Any] = []

    if platform:
        conditions.append("platform = ?")
        params.append(platform)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if user:
        conditions.append("username = ?")
        params.append(user)
    if date_from:
        conditions.append("started_at >= ?")
        params.append(f"{date_from} 00:00:00")
    if date_to:
        conditions.append("started_at <= ?")
        params.append(f"{date_to} 23:59:59")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * per_page

    async with _conn() as conn:
        async with conn.execute(
            f"SELECT COUNT(*) FROM downloads {where}", params
        ) as cur:
            total = (await cur.fetchone())[0]

        async with conn.execute(
            f"SELECT * FROM downloads {where} ORDER BY started_at DESC LIMIT ? OFFSET ?",
            params + [per_page, offset],
        ) as cur:
            rows = await cur.fetchall()

    pages = (total + per_page - 1) // per_page if total else 1
    return {
        "rows": [dict(r) for r in rows],
        "total": total,
        "pages": pages,
        "page": page,
    }


async def get_error_downloads(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return all failed downloads, optionally filtered by date range."""
    conditions = ["status = 'error'"]
    params: List[Any] = []

    if date_from:
        conditions.append("started_at >= ?")
        params.append(f"{date_from} 00:00:00")
    if date_to:
        conditions.append("started_at <= ?")
        params.append(f"{date_to} 23:59:59")

    where = "WHERE " + " AND ".join(conditions)

    async with _conn() as conn:
        async with conn.execute(
            f"SELECT * FROM downloads {where} ORDER BY started_at DESC",
            params,
        ) as cur:
            rows = await cur.fetchall()

    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Public: disk snapshots
# ---------------------------------------------------------------------------

async def insert_disk_snapshot(total_bytes: int, file_count: int) -> None:
    """Record a disk usage snapshot."""
    snapshot_at = _utc_now_str()
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO disk_snapshots (total_bytes, file_count, snapshot_at) VALUES (?, ?, ?)",
            (total_bytes, file_count, snapshot_at),
        )
        await conn.commit()


async def get_latest_disk_snapshot() -> Optional[Dict[str, Any]]:
    """Return the most recent disk snapshot, or None."""
    async with _conn() as conn:
        async with conn.execute(
            "SELECT * FROM disk_snapshots ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None
