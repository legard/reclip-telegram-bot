from collections import deque

from reclip import app


def test_download_command_limits_fragment_concurrency():
    command = app.build_download_command(
        "job-1",
        "https://example.com/video",
        "video",
        None,
    )

    fragments_index = command.index("--concurrent-fragments")
    assert command[fragments_index + 1] == "2"


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
