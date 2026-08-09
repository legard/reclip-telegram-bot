from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json
import sys

import pytest
from yt_dlp import parse_options

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
    assert calls[0][:2] == ("https://youtu.be/x", False)
    assert 0 < calls[0][2] <= app.INFO_REQUEST_TIMEOUT
    assert calls[1][:2] == ("https://youtu.be/x", True)
    assert 0 < calls[1][2] <= app.INFO_REQUEST_TIMEOUT


def test_info_returns_normal_result_when_localized_probe_times_out_in_remaining_budget(monkeypatch):
    """The optional probe must not consume a second full client-timeout window."""
    overall_budget = 55
    normal = {
        "title": "Video",
        "extractor": "youtube",
        "formats": [],
    }
    clock = [0.0]
    timeouts = []

    def fake_run(command, *, capture_output, text, timeout):
        timeouts.append(timeout)
        if "--extractor-args" in command:
            clock[0] += timeout
            raise app.subprocess.TimeoutExpired(command, timeout)

        clock[0] += overall_budget - 0.25
        return SimpleNamespace(returncode=0, stdout=json.dumps(normal), stderr="")

    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(app.subprocess, "run", fake_run)

    response = app.app.test_client().post("/api/info", json={"url": "https://youtu.be/x"})

    assert response.status_code == 200
    assert response.get_json()["title"] == "Video"
    assert response.get_json()["russian_audio"] == {"available": False, "formats": []}
    assert timeouts == pytest.approx([overall_budget, 0.25])
    assert clock[0] <= overall_budget


def test_info_successful_localized_metadata_without_russian_formats_is_unavailable(monkeypatch):
    normal = {"title": "Video", "extractor": "youtube", "formats": []}
    localized = {
        "formats": [
            {"format_id": "en", "acodec": "aac", "vcodec": "none", "language": "en"}
        ]
    }

    def fake_fetch(url, *, russian=False, timeout=60):
        return localized if russian else normal

    monkeypatch.setattr(app, "fetch_info", fake_fetch)

    response = app.app.test_client().post("/api/info", json={"url": "https://youtu.be/x"})

    assert response.status_code == 200
    assert response.get_json()["russian_audio"] == {"available": False, "formats": []}


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


def test_download_command_scopes_ffmpeg_to_m3u8_protocol():
    command = app.build_download_command(
        "job-1",
        "https://example.com/video",
        "video",
        None,
    )

    options = parse_options(command[1:]).ydl_opts

    assert options["external_downloader"] == {"m3u8": "ffmpeg"}


@pytest.mark.parametrize(
    ("returncode", "diagnostics", "expected"),
    [
        (146, ["[tls] IO error: Connection timed out"], True),
        (146, ["ERROR: requested format is not available"], False),
        (1, ["[tls] IO error: Connection timed out"], False),
    ],
)
def test_retryable_download_timeout_requires_ffmpeg_timeout_signature(
    returncode, diagnostics, expected,
):
    assert app.is_retryable_download_timeout(returncode, diagnostics) is expected


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


def test_legacy_mp3_command_is_unchanged():
    url = "https://example.com/video"

    command = app.build_download_command("job-1", url, "audio", None)

    assert command[command.index("-x"):] == ["-x", "--audio-format", "mp3", url]
    assert "-f" not in command
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


def test_download_retries_ffmpeg_timeout_and_removes_partial_output(monkeypatch, tmp_path):
    class Timer:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def cancel(self):
            pass

    class Process:
        def __init__(self, returncode, diagnostics, on_wait=None):
            self.returncode = returncode
            self.stderr = iter(diagnostics)
            self._on_wait = on_wait

        def wait(self):
            if self._on_wait:
                self._on_wait()
            return self.returncode

    partial = tmp_path / "job-1.part"
    completed = tmp_path / "job-1.mp4"
    processes = iter([
        Process(146, ["[tls] IO error: Connection timed out\n"], lambda: partial.write_text("partial")),
        Process(0, [], lambda: completed.write_text("video")),
    ])
    starts = []

    def start_process(*args, **kwargs):
        starts.append(args[1])
        return next(processes)

    def run_process(job, command, *, capture_output=False):
        if command[0] == "ffprobe" and "codec_name" in command:
            return SimpleNamespace(returncode=0, stdout="h264\n", stderr="")
        if command[0] == "ffprobe":
            return SimpleNamespace(
                returncode=0,
                stdout='{"streams": [{"width": 1280, "height": 720}], "format": {"duration": "1"}}',
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(app, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(app, "DOWNLOAD_RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(app.threading, "Timer", Timer)
    monkeypatch.setattr(app, "_start_job_process", start_process)
    monkeypatch.setattr(app, "_run_job_process", run_process)
    job = app._new_job("job-1", "https://youtu.be/x", "Video")
    app.jobs["job-1"] = job

    try:
        app._do_download("job-1", "https://youtu.be/x", "video", None)
    finally:
        app.jobs.pop("job-1", None)

    assert len(starts) == 2
    assert not partial.exists()
    assert job["status"] == "done"


@pytest.mark.parametrize(
    ("revalidation", "expected_error"),
    [
        ({"formats": []}, "Russian audio track is no longer available. Please retry."),
        (
            {
                "formats": [
                    {"acodec": "aac", "vcodec": "none", "language": "ru"},
                    {"acodec": "none", "vcodec": "h264", "height": 720},
                ]
            },
            "ERROR: requested format is not available",
        ),
        (None, "ERROR: requested format is not available"),
        ([], "ERROR: requested format is not available"),
        ({}, "ERROR: requested format is not available"),
        ({"formats": {}}, "ERROR: requested format is not available"),
        ({"formats": ()}, "ERROR: requested format is not available"),
        ({"formats": [None]}, "ERROR: requested format is not available"),
        ({"formats": "not-an-array"}, "ERROR: requested format is not available"),
        (RuntimeError("localized probe failed"), "ERROR: requested format is not available"),
    ],
    ids=[
        "track-disappeared",
        "track-still-available",
        "revalidation-failed",
        "revalidation-invalid-metadata",
        "revalidation-missing-formats",
        "revalidation-mapping-formats",
        "revalidation-tuple-formats",
        "revalidation-non-dict-format",
        "revalidation-malformed-formats",
        "revalidation-errors",
    ],
)
def test_russian_selector_failure_is_classified_only_after_fresh_revalidation(
    monkeypatch, revalidation, expected_error,
):
    """Only a fresh, successful probe may turn a selector error into the RU contract error."""
    available = {
        "formats": [
            {"acodec": "aac", "vcodec": "none", "language": "ru"},
            {"acodec": "none", "vcodec": "h264", "height": 720},
        ]
    }
    probe_results = iter([available, revalidation])
    probe_calls = []

    class Timer:
        def __init__(self, *args, **kwargs):
            self.cancelled = False

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    class FailedProcess:
        def __init__(self):
            self.stderr = iter(["ERROR: requested format is not available\n"])

        def wait(self):
            return 1

    def fake_probe(current_job, url):
        probe_calls.append((current_job, url))
        result = next(probe_results)
        if isinstance(result, Exception):
            raise result
        return result

    job = app._new_job("job-1", "https://youtu.be/x", "Video")
    app.jobs["job-1"] = job
    monkeypatch.setattr(app.threading, "Timer", Timer)
    monkeypatch.setattr(app, "_probe_job_info", fake_probe)
    monkeypatch.setattr(app, "_start_job_process", lambda *args, **kwargs: FailedProcess())

    try:
        app._do_download("job-1", "https://youtu.be/x", "video", None, "ru", 720)
    finally:
        app.jobs.pop("job-1", None)

    assert len(probe_calls) == 2
    assert job["status"] == "error"
    assert job["error"] == expected_error


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
    app.jobs[job_id] = app._new_job(job_id, "https://example.com/video", "Video")
    app.jobs[job_id].update(
        status="downloading",
        stage="downloading",
        started_at=started_at,
        deadline_at=deadline_at,
    )

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


def test_lightweight_process_double_still_receives_term_and_kill(monkeypatch):
    class Process:
        pid = 4242

    signals = []
    sleeps = []
    monkeypatch.setattr(app.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(app.time, "sleep", lambda seconds: sleeps.append(seconds))

    app._terminate_process_group(Process())

    assert sleeps == []
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
    app.jobs["job-1"] = app._new_job(
        "job-1", "https://example.com/video", "Video",
    )
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


def test_download_endpoint_creates_a_queued_job(monkeypatch):
    class Thread:
        daemon = False

        def __init__(self, *, target, args):
            self.target = target
            self.args = args

        def start(self):
            pass

    monkeypatch.setattr(app.threading, "Thread", Thread)

    response = app.app.test_client().post(
        "/api/download",
        json={"url": "https://example.com/video", "format": "video"},
    )
    job_id = response.get_json()["job_id"]

    try:
        assert app.jobs[job_id]["status"] == "queued"
        assert app.jobs[job_id]["stage"] == "queued"
        assert app.jobs[job_id]["error_code"] is None
        assert app.jobs[job_id]["_cancelled"].is_set() is False
    finally:
        app.jobs.pop(job_id, None)


def test_worker_marks_job_downloading_only_after_acquiring_slot(monkeypatch):
    observed = []

    class Semaphore:
        def acquire(self, timeout):
            observed.append(("acquire", app.jobs["job-1"]["status"]))
            return True

        def release(self):
            observed.append(("release", app.jobs["job-1"]["status"]))

    job = app._new_job("job-1", "https://example.com/video", "Video")
    app.jobs["job-1"] = job
    monkeypatch.setattr(app, "download_semaphore", Semaphore())
    monkeypatch.setattr(
        app,
        "_do_download",
        lambda *args: observed.append(("download", job["status"])),
    )

    try:
        app.run_download("job-1", "https://example.com/video", "video", None)
    finally:
        app.jobs.pop("job-1", None)

    assert observed == [
        ("acquire", "queued"),
        ("download", "downloading"),
        ("release", "downloading"),
    ]


def test_cancel_queued_job_is_idempotent():
    job = app._new_job("job-1", "https://example.com/video", "Video")
    app.jobs["job-1"] = job

    try:
        first = app.app.test_client().post("/api/cancel/job-1")
        second = app.app.test_client().post("/api/cancel/job-1")
    finally:
        app.jobs.pop("job-1", None)

    assert first.status_code == 200
    assert first.get_json() == {"job_id": "job-1", "status": "cancelled"}
    assert second.status_code == 200
    assert second.get_json() == {"job_id": "job-1", "status": "cancelled"}
    assert job["_cancelled"].is_set()


def test_cancel_racing_with_semaphore_acquire_releases_slot_without_downloading(monkeypatch):
    calls = []
    job = app._new_job("job-1", "https://example.com/video", "Video")
    app.jobs["job-1"] = job

    class Semaphore:
        def acquire(self, timeout):
            app._finish_cancelled(job)
            calls.append("acquired")
            return True

        def release(self):
            calls.append("released")

    monkeypatch.setattr(app, "download_semaphore", Semaphore())
    monkeypatch.setattr(
        app,
        "_do_download",
        lambda *args: pytest.fail("cancelled queued job must not download"),
    )

    try:
        app.run_download("job-1", "https://example.com/video", "video", None)
    finally:
        app.jobs.pop("job-1", None)

    assert calls == ["acquired", "released"]
    assert job["status"] == "cancelled"


def test_queue_wait_checks_for_cancellation_at_short_intervals(monkeypatch):
    timeouts = []
    job = app._new_job("job-1", "https://example.com/video", "Video")
    app.jobs["job-1"] = job

    class Semaphore:
        def acquire(self, timeout):
            timeouts.append(timeout)
            app._finish_cancelled(job)
            return False

        def release(self):
            pytest.fail("a slot that was not acquired must not be released")

    monkeypatch.setattr(app, "download_semaphore", Semaphore())

    try:
        app.run_download("job-1", "https://example.com/video", "video", None)
    finally:
        app.jobs.pop("job-1", None)

    assert timeouts and timeouts[0] <= 0.25
    assert job["status"] == "cancelled"


def test_job_deadline_expires_while_waiting_for_semaphore(monkeypatch):
    clock = [100.0]

    class Semaphore:
        def acquire(self, timeout):
            clock[0] += timeout
            return False

        def release(self):
            pytest.fail("a slot that was not acquired must not be released")

    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(app, "JOB_TIMEOUT", 0.2)
    monkeypatch.setattr(app, "download_semaphore", Semaphore())
    job = app._new_job("job-1", "https://example.com/video", "Video")
    app.jobs["job-1"] = job

    try:
        app.run_download("job-1", "https://example.com/video", "video", None)
    finally:
        app.jobs.pop("job-1", None)

    assert job["status"] == "error"
    assert job["error_code"] == "job_timeout"
    assert clock[0] == pytest.approx(100.2)


@pytest.mark.parametrize("status", ["downloading", "postprocessing"])
def test_cancel_active_job_terminates_process_group_and_cleans_files(
    monkeypatch, tmp_path, status,
):
    process = SimpleNamespace(pid=4242)
    signals = []
    job = app._new_job("job-1", "https://example.com/video", "Video")
    job.update(status=status, stage=status, _active_process=process)
    app.jobs["job-1"] = job
    monkeypatch.setattr(app, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(app.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(app.time, "sleep", lambda seconds: None)
    (tmp_path / "job-1.mp4").write_text("partial")
    (tmp_path / "job-1.part").write_text("partial")
    (tmp_path / "other.mp4").write_text("keep")

    try:
        response = app.app.test_client().post("/api/cancel/job-1")
    finally:
        app.jobs.pop("job-1", None)

    assert response.status_code == 200
    assert job["status"] == "cancelled"
    assert job["stage"] is None
    assert signals == [(4242, app.signal.SIGTERM), (4242, app.signal.SIGKILL)]
    assert not (tmp_path / "job-1.mp4").exists()
    assert not (tmp_path / "job-1.part").exists()
    assert (tmp_path / "other.mp4").exists()


def test_cancel_teardown_failure_is_retryable_and_not_falsely_idempotent(
    monkeypatch, tmp_path,
):
    process = SimpleNamespace(pid=4242)
    job = app._new_job("job-1", "https://example.com/video", "Video")
    job.update(status="downloading", stage="downloading", _active_process=process)
    app.jobs["job-1"] = job
    partial = tmp_path / "job-1.part"
    partial.write_text("partial")
    attempts = []

    def flaky_terminate(current_process):
        attempts.append(current_process.pid)
        if len(attempts) == 1:
            raise RuntimeError("process group survived SIGKILL")

    monkeypatch.setattr(app, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(app, "_terminate_process_group", flaky_terminate)

    try:
        first = app.app.test_client().post("/api/cancel/job-1")
        assert first.status_code == 503
        assert first.get_json()["error_code"] == "download_failed"
        assert job["status"] == "downloading"
        assert job["_cancel_requested"].is_set()
        assert job["_cancelled"].is_set() is False
        assert partial.exists()
        assert app._finish_error(job, "late worker failure") is False
        assert job["status"] == "downloading"

        second = app.app.test_client().post("/api/cancel/job-1")
    finally:
        app.jobs.pop("job-1", None)

    assert second.status_code == 200
    assert second.get_json() == {"job_id": "job-1", "status": "cancelled"}
    assert attempts == [4242, 4242]
    assert job["status"] == "cancelled"
    assert not partial.exists()


def test_job_deadline_retries_pending_cancel_teardown(monkeypatch, tmp_path):
    process = SimpleNamespace(pid=4242)
    job = app._new_job("job-1", "https://example.com/video", "Video")
    job.update(status="downloading", stage="downloading", _active_process=process)
    partial = tmp_path / "job-1.part"
    partial.write_text("partial")
    attempts = []

    def flaky_terminate(current_process):
        attempts.append(current_process.pid)
        if len(attempts) == 1:
            raise RuntimeError("process group survived SIGKILL")

    monkeypatch.setattr(app, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(app, "_terminate_process_group", flaky_terminate)

    with pytest.raises(RuntimeError):
        app._finish_cancelled(job)
    app.expire_job(job)

    assert attempts == [4242, 4242]
    assert job["status"] == "cancelled"
    assert not partial.exists()


def test_worker_deadline_retries_transient_cancel_cleanup_failure_without_client_retry(
    monkeypatch, tmp_path,
):
    job = app._new_job("job-1", "https://example.com/video", "Video")
    app.jobs["job-1"] = job
    partial = tmp_path / "job-1.part"
    partial.write_text("partial")
    attempt_started = app.threading.Event()
    remove_attempts = []
    original_remove = app.os.remove
    semaphore = app.threading.Semaphore(1)

    def interrupted_attempt(current_job, command):
        attempt_started.set()
        assert current_job["_cancel_requested"].wait(timeout=1)
        return 1, deque()

    def transient_remove_failure(path):
        remove_attempts.append(path)
        if len(remove_attempts) == 1:
            raise PermissionError("file is still in use")
        original_remove(path)

    monkeypatch.setattr(app, "JOB_TIMEOUT", 1)
    job["_deadline_monotonic"] = app.time.monotonic() + app.JOB_TIMEOUT
    monkeypatch.setattr(app, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(app, "download_semaphore", semaphore)
    monkeypatch.setattr(app, "build_download_command", lambda *args: ["fake-download"])
    monkeypatch.setattr(app, "_run_download_attempt", interrupted_attempt)
    monkeypatch.setattr(app.os, "remove", transient_remove_failure)

    worker = app.threading.Thread(
        target=app.run_download,
        args=("job-1", "https://example.com/video", "video", None),
    )
    worker.start()

    try:
        assert attempt_started.wait(timeout=1)
        response = app.app.test_client().post("/api/cancel/job-1")
        assert response.status_code == 503
        worker.join(timeout=1)
        assert worker.is_alive() is False
        assert semaphore.acquire(blocking=False) is True
        assert semaphore.acquire(blocking=False) is False
        semaphore.release()

        assert job["_cancelled"].wait(timeout=3) is True
    finally:
        job["_cancel_requested"].set()
        worker.join(timeout=1)
        app.jobs.pop("job-1", None)

    assert job["status"] == "cancelled"
    assert remove_attempts == [str(partial), str(partial)]
    assert not partial.exists()


def test_successful_cancel_clears_worker_deadline_after_pending_teardown_race(
    monkeypatch,
):
    job = app._new_job("job-1", "https://example.com/video", "Video")
    app.jobs["job-1"] = job
    attempt_started = app.threading.Event()
    cleanup_started = app.threading.Event()
    allow_cleanup = app.threading.Event()
    responses = []
    timers = []
    real_timer = app.threading.Timer
    semaphore = app.threading.Semaphore(1)

    def capture_real_timer(*args, **kwargs):
        timer = real_timer(*args, **kwargs)
        timers.append(timer)
        return timer

    def interrupted_attempt(current_job, command):
        attempt_started.set()
        assert current_job["_cancel_requested"].wait(timeout=1)
        return 1, deque()

    def blocked_successful_cleanup(job_id):
        cleanup_started.set()
        assert allow_cleanup.wait(timeout=1)

    def cancel_from_client():
        responses.append(app.app.test_client().post("/api/cancel/job-1"))

    monkeypatch.setattr(app, "JOB_TIMEOUT", 10)
    job["_deadline_monotonic"] = app.time.monotonic() + app.JOB_TIMEOUT
    monkeypatch.setattr(app, "download_semaphore", semaphore)
    monkeypatch.setattr(app, "build_download_command", lambda *args: ["fake-download"])
    monkeypatch.setattr(app, "_run_download_attempt", interrupted_attempt)
    monkeypatch.setattr(app, "_cleanup_job_files_strict", blocked_successful_cleanup)
    monkeypatch.setattr(app.threading, "Timer", capture_real_timer)

    worker = app.threading.Thread(
        target=app.run_download,
        args=("job-1", "https://example.com/video", "video", None),
    )
    worker.start()
    cancel_thread = app.threading.Thread(target=cancel_from_client)

    try:
        assert attempt_started.wait(timeout=1)
        cancel_thread.start()
        assert cleanup_started.wait(timeout=1)
        worker.join(timeout=1)
        assert worker.is_alive() is False
        assert semaphore.acquire(blocking=False) is True
        assert semaphore.acquire(blocking=False) is False
        semaphore.release()
        assert timers[0].finished.is_set() is False

        allow_cleanup.set()
        cancel_thread.join(timeout=1)
        assert cancel_thread.is_alive() is False
        assert responses[0].status_code == 200
        assert timers[0].finished.wait(timeout=0.5) is True
    finally:
        allow_cleanup.set()
        worker.join(timeout=1)
        cancel_thread.join(timeout=1)
        for timer in timers:
            timer.cancel()
            timer.join(timeout=1)
        app.jobs.pop("job-1", None)

    assert job["status"] == "cancelled"


def test_cancel_cleanup_failure_is_retryable_before_cancelled_is_published(
    monkeypatch, tmp_path,
):
    process = SimpleNamespace(pid=4242)
    job = app._new_job("job-1", "https://example.com/video", "Video")
    job.update(status="downloading", stage="downloading", _active_process=process)
    app.jobs["job-1"] = job
    partial = tmp_path / "job-1.part"
    partial.write_text("partial")
    original_remove = app.os.remove
    remove_attempts = []
    termination_attempts = []

    def flaky_remove(path):
        remove_attempts.append(path)
        if len(remove_attempts) == 1:
            raise PermissionError("file is still in use")
        original_remove(path)

    def record_termination(current_process):
        termination_attempts.append(current_process.pid)

    monkeypatch.setattr(app, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(app.os, "remove", flaky_remove)
    monkeypatch.setattr(app, "_terminate_process_group", record_termination)

    try:
        first = app.app.test_client().post("/api/cancel/job-1")
        assert first.status_code == 503
        assert job["status"] == "downloading"
        assert partial.exists()

        second = app.app.test_client().post("/api/cancel/job-1")
    finally:
        app.jobs.pop("job-1", None)

    assert second.status_code == 200
    assert job["status"] == "cancelled"
    assert len(remove_attempts) == 2
    assert termination_attempts == [4242]
    assert not partial.exists()


def test_retrying_failed_cancel_unblocks_worker_and_releases_slot_once(monkeypatch):
    process = SimpleNamespace(pid=4242)
    job = app._new_job("job-1", "https://example.com/video", "Video")
    app.jobs["job-1"] = job
    process_ready = app.threading.Event()
    process_stopped = app.threading.Event()
    releases = []
    termination_attempts = []

    class Semaphore:
        def acquire(self, timeout):
            return True

        def release(self):
            releases.append("released")

    def blocked_download(*args):
        with app._job_lock(job):
            job["_active_process"] = process
            job["_process"] = process
        process_ready.set()
        assert process_stopped.wait(timeout=2)

    def flaky_terminate(current_process):
        termination_attempts.append(current_process.pid)
        if len(termination_attempts) == 1:
            raise RuntimeError("termination could not be verified")
        process_stopped.set()

    monkeypatch.setattr(app, "download_semaphore", Semaphore())
    monkeypatch.setattr(app, "_do_download", blocked_download)
    monkeypatch.setattr(app, "_terminate_process_group", flaky_terminate)
    worker = app.threading.Thread(
        target=app.run_download,
        args=("job-1", "https://example.com/video", "video", None),
    )
    worker.start()

    try:
        assert process_ready.wait(timeout=1)
        first = app.app.test_client().post("/api/cancel/job-1")
        assert first.status_code == 503
        assert releases == []

        second = app.app.test_client().post("/api/cancel/job-1")
        worker.join(timeout=1)
    finally:
        process_stopped.set()
        worker.join(timeout=1)
        app.jobs.pop("job-1", None)

    assert second.status_code == 200
    assert worker.is_alive() is False
    assert termination_attempts == [4242, 4242]
    assert releases == ["released"]
    assert job["status"] == "cancelled"


def test_terminate_process_group_does_not_treat_permission_error_as_success(monkeypatch):
    process = SimpleNamespace(pid=4242)

    def permission_error(pid, sig):
        raise PermissionError("not permitted")

    monkeypatch.setattr(app.os, "killpg", permission_error)

    with pytest.raises(PermissionError):
        app._terminate_process_group(process)


def test_cancelled_job_cannot_start_a_later_process(monkeypatch):
    job = app._new_job("job-1", "https://example.com/video", "Video")
    app._finish_cancelled(job)
    monkeypatch.setattr(
        app.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("cancelled job started a process"),
    )

    assert app._start_job_process(job, ["ffmpeg", "-version"]) is None


def test_late_process_return_cannot_overwrite_cancelled_job():
    job = app._new_job("job-1", "https://example.com/video", "Video")
    job.update(status="downloading", stage="downloading")

    assert app._finish_cancelled(job) is True
    assert app._finish_done(job, file="late.mp4", filename="late.mp4") is False
    assert app._finish_error(job, "late failure") is False

    assert job["status"] == "cancelled"
    assert "file" not in job
    assert "error" not in job


def test_cancel_unknown_job_is_404_and_completed_jobs_are_409(tmp_path):
    client = app.app.test_client()
    done = app._new_job("done-job", "https://example.com/video", "Video")
    done.update(status="done", stage=None, file=str(tmp_path / "done.mp4"))
    failed = app._new_job("failed-job", "https://example.com/video", "Video")
    failed.update(status="error", stage=None, error="failed", error_code="download_failed")
    app.jobs.update({"done-job": done, "failed-job": failed})

    try:
        missing_response = client.post("/api/cancel/missing-job")
        done_response = client.post("/api/cancel/done-job")
        failed_response = client.post("/api/cancel/failed-job")
    finally:
        app.jobs.pop("done-job", None)
        app.jobs.pop("failed-job", None)

    assert missing_response.status_code == 404
    assert done_response.status_code == 409
    assert done_response.get_json()["status"] == "done"
    assert failed_response.status_code == 409
    assert failed_response.get_json()["status"] == "error"


def test_status_exposes_cancelled_and_stable_error_code():
    job = app._new_job("job-1", "https://example.com/video", "Video")
    app.jobs["job-1"] = job
    app._finish_cancelled(job)

    try:
        cancelled_response = app.app.test_client().get("/api/status/job-1")
        job["status"] = "error"
        job["error"] = "No output file"
        job["error_code"] = "file_missing"
        error_response = app.app.test_client().get("/api/status/job-1")
    finally:
        app.jobs.pop("job-1", None)

    assert cancelled_response.get_json()["status"] == "cancelled"
    assert cancelled_response.get_json()["error_code"] is None
    assert error_response.get_json()["error_code"] == "file_missing"


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (app.subprocess.TimeoutExpired("yt-dlp", 55), "info_timeout"),
        (ValueError("Sign in to confirm your age"), "auth_required"),
        (ConnectionError("Connection reset by peer"), "network"),
        (ValueError("Unsupported URL"), "unavailable"),
    ],
)
def test_info_endpoint_returns_stable_error_codes(monkeypatch, error, expected_code):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(app, "fetch_info", fail)

    response = app.app.test_client().post(
        "/api/info", json={"url": "https://example.com/video"},
    )

    assert response.status_code == 400
    assert response.get_json()["error_code"] == expected_code
    assert response.get_json()["error"]


@pytest.mark.parametrize(
    ("method", "path", "payload", "expected_code"),
    [
        ("post", "/api/info", {}, "unavailable"),
        (
            "post",
            "/api/download",
            {"url": "https://example.com/video", "audio_language": "de"},
            "format_unavailable",
        ),
        ("get", "/api/status/missing-job", None, "unavailable"),
    ],
)
def test_api_error_payloads_include_stable_codes(method, path, payload, expected_code):
    client_method = getattr(app.app.test_client(), method)

    response = client_method(path, json=payload) if payload is not None else client_method(path)

    assert response.status_code >= 400
    assert response.get_json()["error_code"] == expected_code


@pytest.mark.parametrize(
    ("diagnostics", "expected_code"),
    [
        (["ERROR: Sign in to confirm you're not a bot"], "auth_required"),
        (["ERROR: Unable to download webpage: Connection timed out"], "network"),
        (["ERROR: Requested format is not available"], "format_unavailable"),
        (["ERROR: ffmpeg exited with code 8"], "download_failed"),
    ],
)
def test_failed_download_status_has_classified_error_code(
    monkeypatch, diagnostics, expected_code,
):
    class Timer:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def cancel(self):
            pass

    job = app._new_job("job-1", "https://example.com/video", "Video")
    job.update(status="downloading", stage="downloading")
    app.jobs["job-1"] = job
    monkeypatch.setattr(app.threading, "Timer", Timer)
    monkeypatch.setattr(
        app,
        "_run_download_attempt",
        lambda current_job, command: (1, deque(diagnostics)),
    )

    try:
        app._do_download("job-1", "https://example.com/video", "video", None)
        response = app.app.test_client().get("/api/status/job-1")
    finally:
        app.jobs.pop("job-1", None)

    assert response.get_json()["error_code"] == expected_code


@pytest.mark.parametrize(
    ("diagnostic", "expected_code"),
    [
        ("ERROR: Sign in to confirm you're not a bot", "auth_required"),
        ("ERROR: Unable to download webpage: Connection timed out", "network"),
        ("ERROR: Video unavailable", "unavailable"),
        ("ERROR: extractor crashed", "download_failed"),
    ],
)
def test_russian_preflight_preserves_and_classifies_probe_failure(
    monkeypatch, diagnostic, expected_code,
):
    class Timer:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def cancel(self):
            pass

    job = app._new_job("job-1", "https://youtu.be/x", "Video")
    job.update(status="downloading", stage="downloading")
    app.jobs["job-1"] = job
    monkeypatch.setattr(app.threading, "Timer", Timer)
    monkeypatch.setattr(
        app,
        "_run_job_process",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr=diagnostic,
        ),
    )
    monkeypatch.setattr(
        app,
        "build_download_command",
        lambda *args, **kwargs: pytest.fail("preflight failure must stop download"),
    )

    try:
        app._do_download("job-1", "https://youtu.be/x", "video", None, "ru", 720)
    finally:
        app.jobs.pop("job-1", None)

    assert job["status"] == "error"
    assert job["error"] == diagnostic
    assert job["error_code"] == expected_code


def test_busy_timeout_missing_file_and_russian_audio_have_stable_codes(monkeypatch, tmp_path):
    clock = [100.0]

    class UnavailableSemaphore:
        def acquire(self, timeout):
            clock[0] += timeout
            return False

    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])
    busy = app._new_job("busy", "https://example.com/video", "Video")
    app.jobs["busy"] = busy
    monkeypatch.setattr(app, "download_semaphore", UnavailableSemaphore())
    app.run_download("busy", "https://example.com/video", "video", None)

    timed_out = app._new_job("timed-out", "https://example.com/video", "Video")
    timed_out.update(status="downloading", stage="downloading")
    app.expire_job(timed_out)

    missing = app._new_job("missing", "https://example.com/video", "Video")
    missing.update(status="downloading", stage="downloading")
    russian = app._new_job("russian", "https://youtu.be/x", "Video")
    russian.update(status="downloading", stage="downloading")

    class Timer:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def cancel(self):
            pass

    monkeypatch.setattr(app, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(app.threading, "Timer", Timer)
    monkeypatch.setattr(app, "_run_download_attempt", lambda job, command: (0, deque()))
    monkeypatch.setattr(app, "_probe_job_info", lambda job, url: {"formats": []})
    app.jobs["missing"] = missing
    app.jobs["russian"] = russian
    app._do_download("missing", "https://example.com/video", "video", None)
    app._do_download("russian", "https://youtu.be/x", "video", None, "ru", 720)

    try:
        assert busy["error_code"] == "busy"
        assert timed_out["error_code"] == "job_timeout"
        assert missing["error_code"] == "file_missing"
        assert russian["error_code"] == "russian_audio_unavailable"
    finally:
        app.jobs.pop("busy", None)
        app.jobs.pop("missing", None)
        app.jobs.pop("russian", None)


def test_cancellation_during_ffmpeg_cannot_publish_postprocessed_file(monkeypatch, tmp_path):
    class Timer:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def cancel(self):
            pass

    chosen = tmp_path / "job-1.mp4"
    chosen.write_text("video")
    job = app._new_job("job-1", "https://example.com/video", "Video")
    job.update(status="downloading", stage="downloading")
    app.jobs["job-1"] = job

    def run_process(current_job, command, *, capture_output=False):
        if command[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout="vp9\n", stderr="")
        assert command[0] == "ffmpeg"
        (tmp_path / "job-1.mp4.h264.mp4").write_text("transcoded")
        app._finish_cancelled(current_job)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(app, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(app.threading, "Timer", Timer)
    monkeypatch.setattr(app, "_run_download_attempt", lambda current_job, command: (0, deque()))
    monkeypatch.setattr(app, "_run_job_process", run_process)

    try:
        app._do_download("job-1", "https://example.com/video", "video", None)
    finally:
        app.jobs.pop("job-1", None)

    assert job["status"] == "cancelled"
    assert "file" not in job
    assert list(tmp_path.glob("job-1.*")) == []


@pytest.mark.parametrize("terminal_status", ["done", "error"])
def test_status_reader_never_observes_terminal_state_without_terminal_payload(
    terminal_status,
):
    terminal_published = app.threading.Event()
    allow_writer_to_finish = app.threading.Event()
    reader_finished = app.threading.Event()
    response_data = {}

    class PausingJob(dict):
        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            if key == "status" and value == terminal_status:
                terminal_published.set()
                assert allow_writer_to_finish.wait(timeout=1)

    job = PausingJob(app._new_job("job-1", "https://example.com/video", "Video"))
    job.update(status="downloading", stage="downloading")
    app.jobs["job-1"] = job

    def finish_job():
        if terminal_status == "done":
            app._finish_done(
                job,
                file="/downloads/job-1.mp4",
                file_path="/downloads/job-1.mp4",
                filename="Video.mp4",
            )
        else:
            app._finish_error(job, "failed", error_code="network")

    def read_status():
        response_data.update(
            app.app.test_client().get("/api/status/job-1").get_json(),
        )
        reader_finished.set()

    writer = app.threading.Thread(target=finish_job)
    reader = app.threading.Thread(target=read_status)
    writer.start()
    assert terminal_published.wait(timeout=1)
    reader.start()

    # An unlocked reader can finish while the writer is paused between status
    # publication and payload publication. A locked snapshot must wait.
    reader_finished.wait(timeout=0.1)
    allow_writer_to_finish.set()
    writer.join(timeout=1)
    reader.join(timeout=1)
    app.jobs.pop("job-1", None)

    assert response_data["status"] == terminal_status
    if terminal_status == "done":
        assert response_data["file_path"] == "/downloads/job-1.mp4"
        assert response_data["filename"] == "Video.mp4"
    else:
        assert response_data["error"] == "failed"
        assert response_data["error_code"] == "network"


def test_cancel_waits_for_real_process_group_exit_before_cleanup(monkeypatch, tmp_path):
    part_path = tmp_path / "job-1.part"
    ready_path = tmp_path / "child-ready"
    child_code = (
        "import os,signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        f"path={str(part_path)!r};"
        f"open({str(ready_path)!r},'w').close();"
        "\nwhile True:\n"
        " open(path+'.tmp','w').write('late')\n"
        " os.replace(path+'.tmp',path)\n"
        " time.sleep(0.005)\n"
    )
    leader_code = (
        "import signal,subprocess,sys,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        f"subprocess.Popen([{sys.executable!r},'-c',{child_code!r}]);"
        "time.sleep(60)"
    )
    process = app.subprocess.Popen(
        [sys.executable, "-c", leader_code],
        start_new_session=True,
    )

    def group_exists():
        try:
            app.os.killpg(process.pid, 0)
            return True
        except ProcessLookupError:
            return False

    cleanup_group_states = []
    original_cleanup = app._cleanup_job_files

    def observed_cleanup(job_id):
        cleanup_group_states.append(group_exists())
        original_cleanup(job_id)

    monkeypatch.setattr(app, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(app, "_cleanup_job_files", observed_cleanup)
    monkeypatch.setattr(app, "PROCESS_GROUP_TERM_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(app, "PROCESS_GROUP_KILL_TIMEOUT_SECONDS", 2)
    job = app._new_job("job-1", "https://example.com/video", "Video")
    job.update(status="downloading", stage="downloading", _active_process=process)

    try:
        assert ready_path.exists() or _wait_for_path(ready_path)
        assert app._finish_cancelled(job) is True
        app.threading.Event().wait(0.05)

        assert cleanup_group_states == [False]
        assert group_exists() is False
        assert list(tmp_path.glob("job-1.*")) == []
    finally:
        try:
            app.os.killpg(process.pid, app.signal.SIGKILL)
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except app.subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def _wait_for_path(path, timeout=2):
    deadline = app.time.monotonic() + timeout
    while app.time.monotonic() < deadline:
        if path.exists():
            return True
        app.threading.Event().wait(0.01)
    return path.exists()
