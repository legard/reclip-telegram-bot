"""API routes for the reclip_bot admin dashboard."""
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import db
from auth import get_current_user

router = APIRouter()

# In-memory active downloads dict: job_id -> dict
_active_downloads: Dict[str, Dict[str, Any]] = {}


def _downloads_path() -> Path:
    return Path(os.environ.get("DOWNLOADS_PATH", "/downloads"))


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def require_auth(request: Request) -> str:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


# ---------------------------------------------------------------------------
# Event ingestion (no auth — internal Docker network only)
# ---------------------------------------------------------------------------

@router.post("/api/events")
async def ingest_event(request: Request) -> Dict[str, str]:
    """Accept download lifecycle events. No auth — internal network only.

    Bot sends flat JSON: {"type": "download_start", "job_id": "...", ...}
    """
    data = await request.json()
    event_type = data.get("type")

    if event_type == "download_start":
        await db.insert_download_start(
            job_id=data["job_id"],
            user_id=data.get("user_id"),
            username=data.get("username"),
            chat_id=data.get("chat_id"),
            url=data.get("url", ""),
            platform=data.get("platform"),
        )
        _active_downloads[data["job_id"]] = {
            "job_id": data["job_id"],
            "user_id": data.get("user_id"),
            "username": data.get("username"),
            "url": data.get("url"),
            "platform": data.get("platform"),
            "title": data.get("title"),
            "stage": data.get("stage"),
            "percent": 0,
            "speed": 0,
            "eta": 0,
        }

    elif event_type == "download_progress":
        job_id = data.get("job_id")
        if job_id and job_id in _active_downloads:
            _active_downloads[job_id].update({
                "stage": data.get("stage", _active_downloads[job_id].get("stage")),
                "percent": data.get("percent", 0),
                "speed": data.get("speed", 0),
                "eta": data.get("eta", 0),
                "downloaded_bytes": data.get("downloaded_bytes", 0),
                "total_bytes": data.get("total_bytes", 0),
            })

    elif event_type == "download_done":
        await db.update_download_done(
            job_id=data["job_id"],
            file_size_bytes=data.get("file_size_bytes"),
            download_duration_sec=data.get("duration_seconds"),
        )
        _active_downloads.pop(data["job_id"], None)

    elif event_type == "download_error":
        await db.update_download_error(
            job_id=data["job_id"],
            error_message=data.get("error_message", "Unknown error"),
        )
        _active_downloads.pop(data["job_id"], None)

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Dashboard stats (auth required)
# ---------------------------------------------------------------------------

@router.get("/api/dashboard-stats")
async def dashboard_stats(user: str = Depends(require_auth)) -> Dict[str, Any]:
    stats = await db.get_dashboard_stats()
    disk = await db.get_latest_disk_snapshot()
    return {"stats": stats, "disk": disk}


@router.get("/api/chart-data")
async def chart_data(
    range: str = "1D",
    user: str = Depends(require_auth),
) -> Dict[str, Any]:
    valid_ranges = {"1D", "7D", "1M", "1Y"}
    if range not in valid_ranges:
        raise HTTPException(status_code=400, detail=f"Invalid range. Must be one of {valid_ranges}")
    return await db.get_chart_data(range)


@router.get("/api/active-downloads")
async def active_downloads(user: str = Depends(require_auth)) -> List[Dict[str, Any]]:
    return list(_active_downloads.values())


# ---------------------------------------------------------------------------
# Admin file operations (auth required) — Task 5
# ---------------------------------------------------------------------------

class DeleteFilesBody(BaseModel):
    paths: List[str]


class PurgeBody(BaseModel):
    confirm: Optional[str] = None


@router.delete("/api/files")
async def delete_files(
    body: DeleteFilesBody,
    user: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Delete selected files. Uses filename only to prevent path traversal."""
    downloads_path = _downloads_path()
    deleted = []
    errors = []
    for p in body.paths:
        # Prevent path traversal: use only the filename component
        safe_path = downloads_path / Path(p).name
        try:
            if safe_path.exists():
                safe_path.unlink()
                deleted.append(str(safe_path.name))
            else:
                errors.append({"file": p, "error": "not found"})
        except Exception as exc:
            errors.append({"file": p, "error": str(exc)})
    return {"deleted": deleted, "errors": errors}


@router.delete("/api/files/all")
async def purge_all_files(
    body: PurgeBody,
    user: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Purge all files in DOWNLOADS_PATH. Requires confirm='PURGE' in body."""
    if body.confirm != "PURGE":
        raise HTTPException(status_code=400, detail='Body must contain {"confirm": "PURGE"}')
    downloads_path = _downloads_path()
    deleted_count = 0
    if downloads_path.exists():
        for item in downloads_path.iterdir():
            try:
                if item.is_file():
                    item.unlink()
                    deleted_count += 1
                elif item.is_dir():
                    shutil.rmtree(item)
                    deleted_count += 1
            except Exception:
                pass
    return {"deleted_count": deleted_count}
