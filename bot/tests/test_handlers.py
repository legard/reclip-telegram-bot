import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import handlers


class Message:
    photo = False

    def __init__(self):
        self.edits = []

    async def edit_text(self, text):
        self.edits.append(text)


@pytest.mark.asyncio
async def test_wait_helper_edits_only_when_download_stage_or_progress_changes(monkeypatch):
    message = Message()
    statuses = [
        {"status": "downloading", "stage": "downloading", "progress": {"percent": 10}},
        {"status": "downloading", "stage": "downloading", "progress": {"percent": 10}},
        {"status": "downloading", "stage": "postprocessing", "progress": {"percent": 10}},
        {"status": "done", "file_path": "/downloads/video.mp4"},
    ]

    async def fake_wait_for_job(job_id, on_status):
        for status in statuses:
            await on_status(status)
        return statuses[-1]

    async def ignore_progress(**kwargs):
        pass

    monkeypatch.setattr(handlers, "wait_for_job", fake_wait_for_job)
    monkeypatch.setattr(handlers.event_client, "send_progress", ignore_progress)

    result = await handlers._wait_for_download_job("job-1", message)

    assert result["status"] == "done"
    assert message.edits == ["Downloading… 10%", "Post-processing…"]
