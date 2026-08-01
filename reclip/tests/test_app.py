from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from reclip import app


def test_info_adds_localized_russian_audio_summary(monkeypatch):
    normal = {
        "title": "Video",
        "extractor": "youtube",
        "formats": [
            {
                "format_id": "en",
                "acodec": "aac",
                "vcodec": "none",
                "language": "en",
                "language_preference": 10,
                "format_note": "original",
            },
            {
                "format_id": "v720",
                "acodec": "none",
                "vcodec": "h264",
                "height": 720,
                "tbr": 1000,
            },
        ],
    }
    localized = {
        "formats": [
            {
                "format_id": "ru",
                "acodec": "aac",
                "vcodec": "none",
                "language": "ru",
            }
        ]
    }
    calls = []

    def fake_fetch(url, *, russian=False, timeout=60):
        calls.append((url, russian, timeout))
        return localized if russian else normal

    monkeypatch.setattr(app, "fetch_info", fake_fetch)

    response = app.app.test_client().post("/api/info", json={"url": "https://youtu.be/x"})

    assert response.status_code == 200
    assert response.get_json()["russian_audio"] == {
        "available": True,
        "formats": [{"height": 720, "label": "720p"}],
    }
    assert calls == [
        ("https://youtu.be/x", False, 60),
        ("https://youtu.be/x", True, 60),
    ]


def test_info_localized_probe_failure_preserves_normal_response(monkeypatch, caplog):
    normal = {"title": "Video", "extractor": "youtube", "formats": []}

    def fake_fetch(url, *, russian=False, timeout=60):
        if russian:
            raise RuntimeError("localized probe failed")
        return normal

    monkeypatch.setattr(app, "fetch_info", fake_fetch)

    response = app.app.test_client().post("/api/info", json={"url": "https://youtu.be/x"})

    assert response.status_code == 200
    assert response.get_json()["russian_audio"] == {"available": False, "formats": []}
    assert "localized Russian probe failed" in caplog.text


def test_info_non_youtube_does_not_run_localized_probe(monkeypatch):
    calls = []

    def fake_fetch(url, *, russian=False, timeout=60):
        calls.append(russian)
        return {"extractor": "vimeo", "formats": []}

    monkeypatch.setattr(app, "fetch_info", fake_fetch)

    response = app.app.test_client().post("/api/info", json={"url": "https://vimeo.com/1"})

    assert response.status_code == 200
    assert response.get_json()["russian_audio"] == {"available": False, "formats": []}
    assert calls == [False]


def test_download_command_limits_fragment_concurrency():
    command = app.build_download_command(
        "job-1",
        "https://example.com/video",
        "video",
        None,
    )

    fragments_index = command.index("--concurrent-fragments")
    assert command[fragments_index + 1] == "2"


def test_russian_download_command_uses_localized_selector_without_fallback():
    command = app.build_download_command(
        "job-1", "https://youtu.be/x", "video", None,
        audio_language="ru", height=720,
    )

    selector = command[command.index("-f") + 1]
    extractor_args = [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--extractor-args"
    ]

    assert extractor_args == [
        "youtube:lang=ru;player_client=mweb",
        "youtubepot-bgutilhttp:base_url=http://bgutil:4416",
    ]
    assert all("language~=" in branch for branch in selector.split("/"))
    assert all("height=720" in branch for branch in selector.split("/"))
    assert "bestaudio" not in selector


def test_legacy_best_video_command_is_unchanged():
    command = app.build_download_command("job-1", "https://example.com/video", "video", None)

    assert command[command.index("-f") + 1] == "bv*[vcodec~='^(avc|h264)']+ba/b[vcodec~='^(avc|h264)']/bv*+ba/b"
    assert "--extractor-args" not in command


@pytest.mark.parametrize("payload", [
    {"url": "https://youtu.be/x", "audio_language": "de"},
    {"url": "https://youtu.be/x", "audio_language": "ru", "height": 0},
    {"url": "https://youtu.be/x", "audio_language": "ru", "height": -1},
    {"url": "https://youtu.be/x", "audio_language": "ru", "height": "720"},
    {"url": "https://youtu.be/x", "audio_language": "ru", "height": True},
    {"url": "https://vimeo.com/1", "audio_language": "ru"},
    {"url": "https://youtu.be/x", "format": "audio", "audio_language": "ru"},
    {"url": "https://youtu.be/x", "audio_language": "ru", "format_id": "22"},
])
def test_download_rejects_invalid_russian_requests_before_thread(monkeypatch, payload):
    started = []
    monkeypatch.setattr(app.threading, "Thread", lambda *args, **kwargs: started.append((args, kwargs)))

    response = app.app.test_client().post("/api/download", json=payload)

    assert response.status_code == 400
    assert started == []


def test_download_starts_russian_job_with_height_not_format_id(monkeypatch):
    captured = {}

    class Thread:
        daemon = False

        def __init__(self, *, target, args):
            captured.update(target=target, args=args)

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(app.threading, "Thread", Thread)

    response = app.app.test_client().post("/api/download", json={
        "url": "https://youtu.be/x", "format": "video", "title": "Video",
        "audio_language": "ru", "height": 720,
    })

    try:
        assert response.status_code == 200
        assert captured["args"][4:] == ("ru", 720)
        assert captured["started"] is True
    finally:
        app.jobs.pop(response.get_json()["job_id"], None)


def test_russian_job_stops_before_download_when_track_disappears(monkeypatch):
    job = app._new_job("job-1", "https://youtu.be/x", "Video")
    app.jobs["job-1"] = job
    monkeypatch.setattr(app, "_probe_job_info", lambda current_job, url: {"formats": []})
    monkeypatch.setattr(
        app,
        "build_download_command",
        lambda *args, **kwargs: pytest.fail("download command must not be built"),
    )

    try:
        app._do_download("job-1", "https://youtu.be/x", "video", None, "ru", 720)
    finally:
        app.jobs.pop("job-1", None)

    assert job["status"] == "error"
    assert job["error"] == "Russian audio track is no longer available. Please retry."


def test_ffmpeg_runner_discards_process_output(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    result = app.run_ffmpeg(["ffmpeg", "-version"], timeout=30)

    assert result.returncode == 0
    assert captured["kwargs"] == {
        "stdout": app.subprocess.DEVNULL,
        "stderr": app.subprocess.DEVNULL,
        "timeout": 30,
    }


def test_progress_lines_do_not_fill_diagnostic_buffer():
    job = {}
    diagnostics = deque(maxlen=app.DOWNLOAD_DIAGNOSTIC_LINE_LIMIT)

    for downloaded in range(1_000):
        app.record_download_output(
            job,
            diagnostics,
            "download:"
            f'{{"downloaded_bytes":{downloaded},"total_bytes":1000,'
            '"speed":100,"eta":1}',
        )

    assert list(diagnostics) == []
    assert job["progress"]["downloaded_bytes"] == 999
    assert job["progress"]["percent"] == 99.9


def test_each_retained_diagnostic_line_is_bounded():
    diagnostics = deque(maxlen=app.DOWNLOAD_DIAGNOSTIC_LINE_LIMIT)

    app.record_download_output({}, diagnostics, "prefix:" + "x" * 100_000)

    assert len(diagnostics) == 1
    assert len(diagnostics[0]) == app.DOWNLOAD_DIAGNOSTIC_CHAR_LIMIT


def test_error_summary_is_bounded_useful_and_redacted():
    trailing_dash_token = "123456789:abcdefghijklmnopqrs-"
    diagnostics = [f"old diagnostic {number}" for number in range(30)]
    diagnostics.extend(
        [
            "[hls] Opening https://cdn.example.test/video.m3u8?signature=secret",
            "Authorization failed for 123456789:abcdefghijklmnopqrstuvwxyzABCDE",
            f"Authorization failed for {trailing_dash_token}",
            "[mov,mp4] Invalid data found when processing input",
            "ERROR: ffmpeg exited with code 8",
        ]
    )

    summary = app.summarize_download_error(diagnostics)

    assert "Invalid data found when processing input" in summary
    assert "ffmpeg exited with code 8" in summary
    assert "old diagnostic 0" not in summary
    assert "https://" not in summary
    assert "signature=secret" not in summary
    assert "123456789:abcdefghijklmnopqrstuvwxyzABCDE" not in summary
    assert trailing_dash_token not in summary
    assert "[URL]" in summary
    assert "[TOKEN]" in summary
    assert len(summary) <= app.DOWNLOAD_ERROR_CHAR_LIMIT


def test_empty_error_summary_has_safe_fallback():
    assert app.summarize_download_error([]) == "Download failed"


def test_status_exposes_active_stage_and_deadline_fields():
    job_id = "active-job"
    started_at = datetime.now(timezone.utc).isoformat()
    deadline_at = (datetime.now(timezone.utc) + timedelta(minutes=150)).isoformat()
    app.jobs[job_id] = {
        "status": "downloading",
        "stage": "downloading",
        "started_at": started_at,
        "deadline_at": deadline_at,
    }

    try:
        response = app.app.test_client().get(f"/api/status/{job_id}")
    finally:
        app.jobs.pop(job_id, None)

    assert response.status_code == 200
    assert response.json["stage"] == "downloading"
    assert response.json["started_at"] == started_at
    assert response.json["deadline_at"] == deadline_at


@pytest.mark.parametrize("stage", ["downloading", "postprocessing"])
def test_expired_job_terminates_process_group_and_removes_all_job_files(monkeypatch, tmp_path, stage):
    class Process:
        pid = 4242

        def wait(self, timeout):
            raise app.subprocess.TimeoutExpired("yt-dlp", timeout)

    signals = []
    monkeypatch.setattr(app, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(app.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(app.time, "sleep", lambda seconds: None)
    for name in ("job-1.mp4", "job-1.part", "other-job.mp4"):
        (tmp_path / name).write_text("data")
    job = {
        "job_id": "job-1",
        "status": "downloading",
        "stage": stage,
        "_timed_out": app.threading.Event(),
        "_process_lock": app.threading.Lock(),
        "_active_process": Process(),
    }

    app.expire_job(job)

    assert job["status"] == "error"
    assert job["stage"] is None
    assert job["error"] == f"Job timed out after 150 minutes during {stage}."
    assert signals == [
        (4242, app.signal.SIGTERM),
        (4242, app.signal.SIGKILL),
    ]
    assert not (tmp_path / "job-1.mp4").exists()
    assert not (tmp_path / "job-1.part").exists()
    assert (tmp_path / "other-job.mp4").exists()


def test_process_group_is_killed_after_grace_when_leader_exits(monkeypatch):
    """The process leader exiting does not prove its descendants exited."""
    class Process:
        pid = 4242

    signals = []
    sleeps = []
    monkeypatch.setattr(app.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(app.time, "sleep", lambda seconds: sleeps.append(seconds))

    app._terminate_process_group(Process())

    assert sleeps == [5]
    assert signals == [
        (4242, app.signal.SIGTERM),
        (4242, app.signal.SIGKILL),
    ]


def test_job_processes_start_in_a_separate_process_group(monkeypatch):
    captured = {}
    fake_process = SimpleNamespace(pid=12)

    def fake_popen(command, **kwargs):
        captured.update(kwargs)
        return fake_process

    monkeypatch.setattr(app.subprocess, "Popen", fake_popen)
    job = {
        "_process_lock": app.threading.Lock(),
        "_timed_out": app.threading.Event(),
    }

    assert app._start_job_process(job, ["ffmpeg", "-version"]) is fake_process
    assert captured["start_new_session"] is True


def test_expiry_waits_for_process_registration_before_cleanup(monkeypatch, tmp_path):
    """A process started as the deadline fires cannot write after cleanup."""
    class Process:
        pid = 4242

        def wait(self, timeout):
            return 0

    job = {
        "job_id": "job-1",
        "status": "downloading",
        "stage": "downloading",
        "_timed_out": app.threading.Event(),
        "_process_lock": app.threading.Lock(),
        "_active_process": None,
    }
    popen_entered = app.threading.Event()
    allow_popen_to_return = app.threading.Event()
    expiry_finished = app.threading.Event()
    process_started = app.threading.Event()
    signals = []

    def fake_popen(*args, **kwargs):
        popen_entered.set()
        assert allow_popen_to_return.wait(timeout=1)
        process_started.set()
        return Process()

    def fake_killpg(pid, sig):
        signals.append((pid, sig))
        if sig == app.signal.SIGTERM:
            (tmp_path / "job-1.late").write_text("late output")

    monkeypatch.setattr(app, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(app.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(app.os, "killpg", fake_killpg)
    monkeypatch.setattr(app.time, "sleep", lambda seconds: None)
    (tmp_path / "job-1.part").write_text("partial output")

    starter = app.threading.Thread(
        target=app._start_job_process, args=(job, ["yt-dlp", "url"]),
    )
    starter.start()
    assert popen_entered.wait(timeout=1)

    def expire():
        app.expire_job(job)
        expiry_finished.set()

    expiry = app.threading.Thread(target=expire)
    expiry.start()
    assert not expiry_finished.wait(timeout=0.1)

    allow_popen_to_return.set()
    starter.join(timeout=1)
    expiry.join(timeout=1)

    assert process_started.is_set()
    assert expiry_finished.is_set()
    assert signals == [
        (4242, app.signal.SIGTERM),
        (4242, app.signal.SIGKILL),
    ]
    assert not (tmp_path / "job-1.part").exists()
    assert not (tmp_path / "job-1.late").exists()


def test_expired_job_error_is_not_overwritten_by_completion():
    job = {
        "job_id": "job-1",
        "status": "error",
        "stage": None,
        "error": "Job timed out after 150 minutes during downloading.",
        "_timed_out": app.threading.Event(),
        "_process_lock": app.threading.Lock(),
        "_active_process": None,
    }

    assert hasattr(app, "_finish_done")
    assert app._finish_done(job) is False

    assert job["status"] == "error"
    assert job["error"] == "Job timed out after 150 minutes during downloading."
    assert "file" not in job


def test_timeout_message_uses_configured_job_deadline(monkeypatch):
    monkeypatch.setattr(app, "JOB_TIMEOUT", 90)
    job = {
        "job_id": "job-1",
        "status": "downloading",
        "stage": "downloading",
        "_timed_out": app.threading.Event(),
        "_process_lock": app.threading.Lock(),
        "_active_process": None,
    }

    app.expire_job(job)

    assert job["error"] == "Job timed out after 1.5 minutes during downloading."


def test_download_slot_is_released_after_job_finishes(monkeypatch):
    class Semaphore:
        def __init__(self):
            self.released = 0

        def acquire(self, timeout):
            return True

        def release(self):
            self.released += 1

    semaphore = Semaphore()
    app.jobs["job-1"] = {"status": "downloading"}
    monkeypatch.setattr(app, "download_semaphore", semaphore)
    monkeypatch.setattr(app, "_do_download", lambda *args: None)

    try:
        app.run_download("job-1", "https://example.com/video", "video", None)
    finally:
        app.jobs.pop("job-1", None)

    assert semaphore.released == 1


def test_download_start_failure_finalizes_job_and_cancels_deadline_timer(monkeypatch):
    class Timer:
        cancelled = False

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    job = {
        "job_id": "job-1",
        "status": "downloading",
        "stage": "downloading",
        "_started_monotonic": app.time.monotonic(),
        "_deadline_monotonic": app.time.monotonic() + 100,
        "_timed_out": app.threading.Event(),
        "_process_lock": app.threading.Lock(),
        "_active_process": None,
    }
    app.jobs["job-1"] = job
    monkeypatch.setattr(app.threading, "Timer", Timer)
    monkeypatch.setattr(app.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))

    try:
        app._do_download("job-1", "https://example.com/video", "video", None)
    finally:
        app.jobs.pop("job-1", None)

    assert job["status"] == "error"
    assert job["error"] == "Download failed"
