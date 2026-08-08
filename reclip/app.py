import os
import uuid
import glob
import json
import re
import subprocess
import threading
import signal
import time
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from flask import Flask, request, jsonify, send_file, render_template

try:
    from youtube_audio import (
        build_russian_extractor_args,
        empty_russian_audio,
        is_youtube_info,
        is_youtube_url,
        build_russian_format_selector,
        russian_download_available,
        russian_audio_summary,
    )
except ImportError:
    from reclip.youtube_audio import (
        build_russian_extractor_args,
        empty_russian_audio,
        is_youtube_info,
        is_youtube_url,
        build_russian_format_selector,
        russian_download_available,
        russian_audio_summary,
    )

app = Flask(__name__)
logger = logging.getLogger(__name__)
DOWNLOAD_DIR = os.environ.get("DOWNLOADS_PATH", os.path.join(os.path.dirname(__file__), "downloads"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL", "http://bgutil:4416")
# Keep the server-side info request below the bot client's 60-second timeout.
# The localized Russian probe is optional and shares this single deadline.
INFO_REQUEST_TIMEOUT = 55

MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", 3))
# DOWNLOAD_TIMEOUT is retained as a backwards-compatible fallback for older
# deployments. The single job deadline covers download and post-processing.
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", os.environ.get("DOWNLOAD_TIMEOUT", 9000)))
download_semaphore = threading.Semaphore(MAX_CONCURRENT_DOWNLOADS)

jobs = {}

PROGRESS_TEMPLATE = (
    'download:{"downloaded_bytes":%(progress.downloaded_bytes)s,'
    '"total_bytes":%(progress.total_bytes)s,'
    '"speed":%(progress.speed)s,'
    '"eta":%(progress.eta)s}'
)

DOWNLOAD_DIAGNOSTIC_LINE_LIMIT = 20
DOWNLOAD_DIAGNOSTIC_CHAR_LIMIT = 2048
DOWNLOAD_ERROR_CHAR_LIMIT = 1500
RUSSIAN_AUDIO_UNAVAILABLE_ERROR = "Russian audio track is no longer available. Please retry."
FFMPEG_TIMEOUT_EXIT_CODE = 146
DOWNLOAD_RETRY_DELAYS = (2, 5)
QUEUE_WAIT_TIMEOUT = 30
SEMAPHORE_POLL_SECONDS = 0.1
TERMINAL_JOB_STATUSES = frozenset({"done", "error", "cancelled"})
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])\d{6,}:[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"
)


def build_download_command(job_id, url, format_choice, format_id, audio_language=None, height=None):
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")
    command = [
        "yt-dlp", "--no-playlist", "-o", out_template,
        "--progress-template", PROGRESS_TEMPLATE,
        "--force-ipv4",
        "--downloader", "m3u8:ffmpeg",
        "--concurrent-fragments", "2",
        "--socket-timeout", "20",
        "--retries", "5",
        "--fragment-retries", "10",
        "--throttled-rate", "50K",
    ]

    if format_choice == "audio":
        command += ["-x", "--audio-format", "mp3"]
    elif audio_language == "ru":
        for extractor_arg in build_russian_extractor_args(POT_PROVIDER_URL):
            command += ["--extractor-args", extractor_arg]
        command += ["-f", build_russian_format_selector(height), "--merge-output-format", "mp4"]
    elif format_id:
        command += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        command += ["-f", "bv*[vcodec~='^(avc|h264)']+ba/b[vcodec~='^(avc|h264)']/bv*+ba/b", "--merge-output-format", "mp4"]

    command.append(url)
    return command


def record_download_output(job, diagnostics, line):
    if line.startswith("download:"):
        try:
            progress_data = json.loads(line.removeprefix("download:"))
            total = progress_data.get("total_bytes")
            downloaded = progress_data.get("downloaded_bytes")
            percent = None
            if (
                isinstance(total, (int, float))
                and isinstance(downloaded, (int, float))
                and total > 0
            ):
                percent = round(downloaded / total * 100, 1)
            job["progress"] = {
                "percent": percent,
                "downloaded_bytes": downloaded,
                "total_bytes": total,
                "speed": progress_data.get("speed"),
                "eta": progress_data.get("eta"),
            }
            return
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    if len(line) > DOWNLOAD_DIAGNOSTIC_CHAR_LIMIT:
        marker = "...[truncated]..."
        prefix_length = (DOWNLOAD_DIAGNOSTIC_CHAR_LIMIT - len(marker)) // 2
        suffix_length = DOWNLOAD_DIAGNOSTIC_CHAR_LIMIT - len(marker) - prefix_length
        line = line[:prefix_length] + marker + line[-suffix_length:]
    diagnostics.append(line)


def summarize_download_error(diagnostics):
    lines = list(diagnostics)[-DOWNLOAD_DIAGNOSTIC_LINE_LIMIT:]
    if not lines:
        return "Download failed"

    summary = "\n".join(lines)
    summary = URL_PATTERN.sub("[URL]", summary)
    summary = TOKEN_PATTERN.sub("[TOKEN]", summary)
    return summary[-DOWNLOAD_ERROR_CHAR_LIMIT:]


def classify_error_code(error, *, default):
    """Reduce unstable yt-dlp/transport text to the bot's stable error vocabulary."""
    if isinstance(error, subprocess.TimeoutExpired):
        return "info_timeout"
    if isinstance(error, (ConnectionError, TimeoutError)):
        return "network"

    message = str(error).lower()
    if any(marker in message for marker in (
        "sign in", "log in", "login", "authentication", "cookies",
        "private video", "members-only", "age-restricted",
    )):
        return "auth_required"
    if any(marker in message for marker in (
        "requested format", "format is not available", "no video formats",
        "no suitable formats",
    )):
        return "format_unavailable"
    if any(marker in message for marker in (
        "connection", "network", "timed out", "timeout", "temporary failure",
        "unable to download webpage", "name resolution", "dns",
    )):
        return "network"
    if any(marker in message for marker in (
        "video unavailable", "content unavailable", "unsupported url", "removed",
    )):
        return "unavailable"
    return default


def is_retryable_download_timeout(returncode, diagnostics):
    return (
        returncode == FFMPEG_TIMEOUT_EXIT_CODE
        and any("Connection timed out" in line for line in diagnostics)
    )


def build_info_command(url, *, russian=False):
    command = ["yt-dlp", "--no-playlist", "-J"]
    if russian:
        for extractor_arg in build_russian_extractor_args(POT_PROVIDER_URL):
            command += ["--extractor-args", extractor_arg]
    command.append(url)
    return command


def fetch_info(url, *, russian=False, timeout=60):
    result = subprocess.run(
        build_info_command(url, russian=russian),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        message = result.stderr.strip().split("\n")[-1] or "Failed to fetch video info"
        raise ValueError(message)
    return json.loads(result.stdout)


def run_ffmpeg(command, timeout):
    return subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )


def _job_duration(job):
    started_at = job.get("_started_monotonic")
    return round(time.monotonic() - started_at, 1) if started_at else None


def _log_stage(job, stage):
    with _job_lock(job):
        if _job_stopped(job):
            return False
        job["status"] = stage
        job["stage"] = stage
    logger.info("job_id=%s stage=%s", job["job_id"], stage)
    return True


def _log_result(job, result):
    logger.info(
        "job_id=%s result=%s duration_seconds=%s",
        job["job_id"], result, _job_duration(job),
    )


def _cleanup_job_files(job_id):
    for path in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*")):
        try:
            os.remove(path)
        except OSError:
            pass


def _timeout_minutes():
    minutes = JOB_TIMEOUT / 60
    return str(int(minutes)) if minutes.is_integer() else f"{minutes:g}"


def _job_lock(job):
    return job.get("_lock") or job["_process_lock"]


def _event_is_set(job, key):
    event = job.get(key)
    return event is not None and event.is_set()


def _job_stopped(job):
    return (
        job.get("status") in TERMINAL_JOB_STATUSES
        or _event_is_set(job, "_timed_out")
        or _event_is_set(job, "_cancelled")
    )


def _mark_downloading(job):
    with _job_lock(job):
        if _job_stopped(job):
            return False
        job["status"] = "downloading"
        job["stage"] = "downloading"
    logger.info("job_id=%s stage=downloading", job["job_id"])
    return True


def _finish_error(job, message, *, error_code="download_failed"):
    with _job_lock(job):
        if job.get("status") in TERMINAL_JOB_STATUSES:
            return False
        job["status"] = "error"
        job["stage"] = None
        job["error"] = message
        job["error_code"] = error_code
    _log_result(job, "error")
    return True


def _finish_done(job, *, file=None, file_path=None, filename=None):
    """Atomically finalize a job unless an earlier terminal state won the race."""
    with _job_lock(job):
        if _job_stopped(job) or job.get("status") not in {"downloading", "postprocessing"}:
            return False
        job["status"] = "done"
        job["stage"] = None
        if file is not None:
            job["file"] = file
            job["file_path"] = file_path
            job["filename"] = filename
    return True


def _finish_cancelled(job):
    """Make cancellation terminal before stopping processes or deleting output."""
    with _job_lock(job):
        if job.get("status") == "cancelled":
            return True
        if job.get("status") in {"done", "error"}:
            return False
        job["_cancelled"].set()
        job["status"] = "cancelled"
        job["stage"] = None
        process = job.get("_active_process") or job.get("_process")
    if process is not None:
        _terminate_process_group(process)
    _cleanup_job_files(job["job_id"])
    _log_result(job, "cancelled")
    return True


def _terminate_process_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    # The group leader can exit before an ffmpeg/yt-dlp child. Waiting on the
    # leader alone would then skip SIGKILL and leave that child running.
    time.sleep(5)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def expire_job(job):
    """Stop the active process group and remove partial output at the job deadline."""
    with _job_lock(job):
        if job.get("status") in TERMINAL_JOB_STATUSES:
            return
        job["_timed_out"].set()
        stage = job.get("stage") or "downloading"
        job["status"] = "error"
        job["stage"] = None
        job["error"] = (
            f"Job timed out after {_timeout_minutes()} minutes during {stage}."
        )
        job["error_code"] = "job_timeout"
        process = job.get("_active_process") or job.get("_process")
    logger.info("job_id=%s deadline_exceeded stage=%s", job["job_id"], stage)
    _log_result(job, "error")
    if process is not None:
        _terminate_process_group(process)
    _cleanup_job_files(job["job_id"])


def _start_job_process(job, command, **kwargs):
    with _job_lock(job):
        if _job_stopped(job):
            return None
        process = subprocess.Popen(command, start_new_session=True, **kwargs)
        job["_active_process"] = process
        job["_process"] = process
    return process


def _clear_job_process(job, process):
    with _job_lock(job):
        if job.get("_active_process") is process:
            job["_active_process"] = None
        if job.get("_process") is process:
            job["_process"] = None


def _run_job_process(job, command, *, capture_output=False):
    kwargs = {
        "stdout": subprocess.PIPE if capture_output else subprocess.DEVNULL,
        "stderr": subprocess.PIPE if capture_output else subprocess.DEVNULL,
        "text": True,
    }
    process = _start_job_process(job, command, **kwargs)
    if process is None:
        return SimpleNamespace(returncode=-1, stdout="", stderr="")
    try:
        stdout, stderr = process.communicate()
        return SimpleNamespace(returncode=process.returncode, stdout=stdout, stderr=stderr)
    finally:
        _clear_job_process(job, process)


def _probe_job_info(job, url):
    result = _run_job_process(job, build_info_command(url, russian=True), capture_output=True)
    if result.returncode != 0:
        message = (result.stderr or "").strip().split("\n")[-1]
        raise ValueError(message or "Failed to fetch localized video info")
    try:
        info = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Failed to parse localized video info") from error
    if not isinstance(info, dict):
        raise ValueError("Localized video info was not an object")
    return info


def _russian_audio_is_confirmed_unavailable(info, height):
    if not isinstance(info, dict):
        return False
    formats = info.get("formats")
    if not isinstance(formats, list) or not all(isinstance(fmt, dict) for fmt in formats):
        return False
    try:
        return not russian_download_available(info, height)
    except (AttributeError, TypeError):
        return False


def _acquire_download_slot(job):
    queue_deadline = min(
        time.monotonic() + QUEUE_WAIT_TIMEOUT,
        job["_deadline_monotonic"],
    )
    while not _job_stopped(job):
        now = time.monotonic()
        if now >= job["_deadline_monotonic"]:
            expire_job(job)
            return False
        if now >= queue_deadline:
            _finish_error(
                job,
                "Too many concurrent downloads, please try again later",
                error_code="busy",
            )
            return False
        wait_for = min(SEMAPHORE_POLL_SECONDS, queue_deadline - now)
        if download_semaphore.acquire(timeout=wait_for):
            return True
    return False


def run_download(job_id, url, format_choice, format_id, audio_language=None, height=None):
    job = jobs[job_id]

    if not _acquire_download_slot(job):
        if _job_stopped(job):
            return
        _finish_error(
            job,
            "Too many concurrent downloads, please try again later",
            error_code="busy",
        )
        return

    try:
        if _mark_downloading(job):
            _do_download(job_id, url, format_choice, format_id, audio_language, height)
    finally:
        download_semaphore.release()


def _run_download_attempt(job, command):
    stderr_lines = deque(maxlen=DOWNLOAD_DIAGNOSTIC_LINE_LIMIT)
    process = _start_job_process(
        job, command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    if process is None:
        return None, stderr_lines
    try:
        for line in process.stderr:
            record_download_output(job, stderr_lines, line.rstrip("\n"))
        return process.wait(), stderr_lines
    finally:
        _clear_job_process(job, process)


def _do_download(job_id, url, format_choice, format_id, audio_language=None, height=None):
    job = jobs[job_id]

    remaining_timeout = max(0, job["_deadline_monotonic"] - time.monotonic())
    deadline_timer = threading.Timer(remaining_timeout, expire_job, args=(job,))
    deadline_timer.daemon = True
    deadline_timer.start()

    try:
        if _job_stopped(job):
            deadline_timer.cancel()
            return
        if audio_language == "ru":
            try:
                info = _probe_job_info(job, url)
            except Exception as error:
                if not _job_stopped(job):
                    message = str(error) or "Failed to fetch localized video info"
                    _finish_error(
                        job,
                        message,
                        error_code=classify_error_code(
                            error, default="download_failed",
                        ),
                    )
                deadline_timer.cancel()
                return
            if _job_stopped(job):
                deadline_timer.cancel()
                return
            if not russian_download_available(info or {}, height):
                _finish_error(
                    job,
                    RUSSIAN_AUDIO_UNAVAILABLE_ERROR,
                    error_code="russian_audio_unavailable",
                )
                deadline_timer.cancel()
                return

        cmd = build_download_command(job_id, url, format_choice, format_id, audio_language, height)
        for attempt in range(len(DOWNLOAD_RETRY_DELAYS) + 1):
            returncode, stderr_lines = _run_download_attempt(job, cmd)
            if returncode in (None, 0) or _job_stopped(job):
                break
            if (
                attempt == len(DOWNLOAD_RETRY_DELAYS)
                or not is_retryable_download_timeout(returncode, stderr_lines)
            ):
                break

            delay = DOWNLOAD_RETRY_DELAYS[attempt]
            logger.info(
                "job_id=%s retry=%s reason=ffmpeg_connection_timeout",
                job_id,
                attempt + 1,
            )
            _cleanup_job_files(job_id)
            if job["_cancelled"].wait(delay) or job["_timed_out"].is_set():
                break
    except Exception:
        if not _job_stopped(job):
            _finish_error(job, "Download failed")
        deadline_timer.cancel()
        return

    if _job_stopped(job):
        deadline_timer.cancel()
        return

    if returncode is None:
        deadline_timer.cancel()
        return

    try:
        if returncode != 0:
            error = summarize_download_error(stderr_lines)
            error_code = classify_error_code(error, default="download_failed")
            if audio_language == "ru" and not _job_stopped(job):
                try:
                    revalidated_info = _probe_job_info(job, url)
                except Exception:
                    revalidated_info = None
                if (
                    not _job_stopped(job)
                    and _russian_audio_is_confirmed_unavailable(revalidated_info, height)
                ):
                    error = RUSSIAN_AUDIO_UNAVAILABLE_ERROR
                    error_code = "russian_audio_unavailable"
            if not _job_stopped(job):
                _finish_error(job, error, error_code=error_code)
            deadline_timer.cancel()
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            _finish_error(
                job,
                "Download completed but no file was found",
                error_code="file_missing",
            )
            deadline_timer.cancel()
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        if chosen.endswith(".mp4"):
            if not _log_stage(job, "postprocessing"):
                deadline_timer.cancel()
                return
            try:
                codec_probe = _run_job_process(
                    job,
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=codec_name",
                     "-of", "default=noprint_wrappers=1:nokey=1", chosen],
                    capture_output=True,
                )
                if _job_stopped(job):
                    deadline_timer.cancel()
                    return
                vcodec = (codec_probe.stdout or "").strip().lower()
            except Exception:
                vcodec = ""

            if vcodec in ("av1", "vp9", "vp8"):
                transcoded = chosen + ".h264.mp4"
                try:
                    r = _run_job_process(job,
                        ["ffmpeg", "-y", "-i", chosen,
                         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                         "-c:a", "aac", "-b:a", "128k",
                         "-movflags", "+faststart",
                         transcoded],
                    )
                    if _job_stopped(job):
                        deadline_timer.cancel()
                        return
                    if r.returncode == 0 and os.path.exists(transcoded) and os.path.getsize(transcoded) > 0:
                        os.replace(transcoded, chosen)
                    elif os.path.exists(transcoded):
                        os.remove(transcoded)
                except (subprocess.TimeoutExpired, OSError):
                    if os.path.exists(transcoded):
                        try: os.remove(transcoded)
                        except OSError: pass
            else:
                faststart_tmp = chosen + ".fs.mp4"
                try:
                    _run_job_process(job,
                        ["ffmpeg", "-y", "-i", chosen, "-c", "copy",
                         "-movflags", "+faststart", faststart_tmp],
                    )
                    if _job_stopped(job):
                        deadline_timer.cancel()
                        return
                    if os.path.exists(faststart_tmp) and os.path.getsize(faststart_tmp) > 0:
                        os.replace(faststart_tmp, chosen)
                    elif os.path.exists(faststart_tmp):
                        os.remove(faststart_tmp)
                except (subprocess.TimeoutExpired, OSError):
                    if os.path.exists(faststart_tmp):
                        try: os.remove(faststart_tmp)
                        except OSError: pass

        if chosen.endswith(".mp4"):
            try:
                probe = _run_job_process(
                    job,
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height:format=duration",
                     "-of", "json", chosen],
                    capture_output=True,
                )
                if _job_stopped(job):
                    deadline_timer.cancel()
                    return
                info = json.loads(probe.stdout)
                stream = (info.get("streams") or [{}])[0]
                fmt = info.get("format") or {}
                job["width"] = stream.get("width")
                job["height"] = stream.get("height")
                dur = fmt.get("duration")
                job["duration"] = float(dur) if dur else None
            except Exception:
                pass

        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        # Sanitize title for filename
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
            filename = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            filename = os.path.basename(chosen)
        completed = _finish_done(
            job,
            file=chosen,
            file_path=os.path.abspath(chosen),
            filename=filename,
        )
        if completed:
            _log_result(job, "done")
        deadline_timer.cancel()
    except Exception:
        if not _job_stopped(job):
            _finish_error(job, "Download failed")
        deadline_timer.cancel()


def _new_job(job_id, url, title):
    now = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    deadline = now + timedelta(seconds=JOB_TIMEOUT)
    lock = threading.Lock()
    return {
        "job_id": job_id,
        "status": "queued",
        "stage": "queued",
        "error_code": None,
        "url": url,
        "title": title,
        "started_at": now.isoformat(),
        "deadline_at": deadline.isoformat(),
        "_started_monotonic": started_monotonic,
        "_deadline_monotonic": started_monotonic + JOB_TIMEOUT,
        "_timed_out": threading.Event(),
        "_cancelled": threading.Event(),
        "_lock": lock,
        "_process_lock": lock,
        "_active_process": None,
        "_process": None,
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided", "error_code": "unavailable"}), 400

    try:
        info_deadline = time.monotonic() + INFO_REQUEST_TIMEOUT
        info = fetch_info(url, timeout=max(0, info_deadline - time.monotonic()))
        russian_audio = empty_russian_audio()
        if is_youtube_info(info):
            remaining_timeout = info_deadline - time.monotonic()
            try:
                if remaining_timeout > 0:
                    localized_info = fetch_info(url, russian=True, timeout=remaining_timeout)
                    russian_audio = russian_audio_summary(info, localized_info)
            except Exception as error:
                logger.warning("localized Russian probe failed url=%s: %s", url, error)

        # Build quality options — keep best format per resolution
        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({
                "id": f["format_id"],
                "label": f"{height}p",
                "height": height,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "extractor": info.get("extractor", ""),
            "formats": formats,
            "russian_audio": russian_audio,
        })
    except subprocess.TimeoutExpired:
        return jsonify({
            "error": "Timed out fetching video info",
            "error_code": "info_timeout",
        }), 400
    except Exception as e:
        return jsonify({
            "error": str(e),
            "error_code": classify_error_code(e, default="unavailable"),
        }), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")
    audio_language = data.get("audio_language")
    height = data.get("height")

    if not url:
        return jsonify({"error": "No URL provided", "error_code": "unavailable"}), 400
    if audio_language not in (None, "ru"):
        return jsonify({"error": "Unsupported audio_language", "error_code": "format_unavailable"}), 400
    if height is not None and (isinstance(height, bool) or not isinstance(height, int) or height <= 0):
        return jsonify({"error": "height must be a positive integer", "error_code": "format_unavailable"}), 400
    if audio_language == "ru" and not is_youtube_url(url):
        return jsonify({"error": "Russian audio is only supported for YouTube URLs", "error_code": "format_unavailable"}), 400
    if audio_language == "ru" and format_choice != "video":
        return jsonify({"error": "Russian audio is only supported for video downloads", "error_code": "format_unavailable"}), 400
    if audio_language == "ru" and format_id is not None:
        return jsonify({"error": "format_id is not accepted for Russian audio", "error_code": "format_unavailable"}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = _new_job(job_id, url, title)
    jobs[job_id]["_audio_language"] = audio_language
    jobs[job_id]["_requested_height"] = height
    logger.info("job_id=%s stage=queued", job_id)

    thread = threading.Thread(
        target=run_download,
        args=(job_id, url, format_choice, format_id, audio_language, height),
    )
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found", "error_code": "unavailable"}), 404
    return jsonify({
        "status": job["status"],
        "stage": job.get("stage"),
        "started_at": job.get("started_at"),
        "deadline_at": job.get("deadline_at"),
        "error": job.get("error"),
        "error_code": job.get("error_code"),
        "filename": job.get("filename"),
        "progress": job.get("progress"),
        "file_path": job.get("file_path"),
        "width": job.get("width"),
        "height": job.get("height"),
        "duration": job.get("duration"),
    })


@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if not _finish_cancelled(job):
        return jsonify({
            "job_id": job_id,
            "status": job["status"],
            "error": "Job already finished",
            "error_code": job.get("error_code") or "download_failed",
        }), 409
    return jsonify({"job_id": job_id, "status": "cancelled"})


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready", "error_code": "file_missing"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
