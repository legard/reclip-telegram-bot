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

app = Flask(__name__)
logger = logging.getLogger(__name__)
DOWNLOAD_DIR = os.environ.get("DOWNLOADS_PATH", os.path.join(os.path.dirname(__file__), "downloads"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

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
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])\d{6,}:[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"
)


def build_download_command(job_id, url, format_choice, format_id):
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")
    command = [
        "yt-dlp", "--no-playlist", "-o", out_template,
        "--progress-template", PROGRESS_TEMPLATE,
        "--force-ipv4",
        "--downloader", "hls:ffmpeg",
        "--concurrent-fragments", "2",
        "--socket-timeout", "20",
        "--retries", "5",
        "--fragment-retries", "10",
        "--throttled-rate", "50K",
    ]

    if format_choice == "audio":
        command += ["-x", "--audio-format", "mp3"]
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
    with job["_process_lock"]:
        if job.get("status") != "downloading" or job["_timed_out"].is_set():
            return False
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


def _finish_error(job, message):
    with job["_process_lock"]:
        if job.get("status") in ("done", "error"):
            return False
        job["status"] = "error"
        job["stage"] = None
        job["error"] = message
    _log_result(job, "error")
    return True


def _finish_done(job, *, file=None, file_path=None, filename=None):
    """Atomically finalize a job unless an earlier terminal state won the race."""
    with job["_process_lock"]:
        if job.get("status") != "downloading" or job["_timed_out"].is_set():
            return False
        job["status"] = "done"
        job["stage"] = None
        if file is not None:
            job["file"] = file
            job["file_path"] = file_path
            job["filename"] = filename
        return True


def _terminate_process_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def expire_job(job):
    """Stop the active process group and remove partial output at the job deadline."""
    with job["_process_lock"]:
        if job.get("status") in ("done", "error"):
            return
        job["_timed_out"].set()
        stage = job.get("stage") or "downloading"
        job["status"] = "error"
        job["stage"] = None
        job["error"] = (
            f"Job timed out after {_timeout_minutes()} minutes during {stage}."
        )
        process = job.get("_active_process")
    logger.info("job_id=%s deadline_exceeded stage=%s", job["job_id"], stage)
    _log_result(job, "error")
    if process is not None:
        _terminate_process_group(process)
    _cleanup_job_files(job["job_id"])


def _start_job_process(job, command, **kwargs):
    with job["_process_lock"]:
        if job.get("status") in ("done", "error") or job["_timed_out"].is_set():
            return None
        process = subprocess.Popen(command, start_new_session=True, **kwargs)
        job["_active_process"] = process
    return process


def _clear_job_process(job, process):
    with job["_process_lock"]:
        if job.get("_active_process") is process:
            job["_active_process"] = None


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


def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]

    if not download_semaphore.acquire(timeout=30):
        _finish_error(job, "Too many concurrent downloads, please try again later")
        return

    try:
        _do_download(job_id, url, format_choice, format_id)
    finally:
        download_semaphore.release()


def _do_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    cmd = build_download_command(job_id, url, format_choice, format_id)

    remaining_timeout = max(0, job["_deadline_monotonic"] - time.monotonic())
    deadline_timer = threading.Timer(remaining_timeout, expire_job, args=(job,))
    deadline_timer.daemon = True
    deadline_timer.start()

    stderr_lines = deque(maxlen=DOWNLOAD_DIAGNOSTIC_LINE_LIMIT)
    try:
        process = _start_job_process(
            job, cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        if process is None:
            deadline_timer.cancel()
            return
        try:
            for line in process.stderr:
                record_download_output(job, stderr_lines, line.rstrip("\n"))
            returncode = process.wait()
        finally:
            _clear_job_process(job, process)
    except Exception:
        if not job["_timed_out"].is_set():
            _finish_error(job, "Download failed")
        deadline_timer.cancel()
        return

    if job["_timed_out"].is_set():
        deadline_timer.cancel()
        return

    try:
        if returncode != 0:
            _finish_error(job, summarize_download_error(stderr_lines))
            deadline_timer.cancel()
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            _finish_error(job, "Download completed but no file was found")
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
            _log_stage(job, "postprocessing")
            try:
                codec_probe = _run_job_process(
                    job,
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=codec_name",
                     "-of", "default=noprint_wrappers=1:nokey=1", chosen],
                    capture_output=True,
                )
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
    except Exception as e:
        if not job["_timed_out"].is_set():
            _finish_error(job, "Download failed")
        deadline_timer.cancel()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cmd = ["yt-dlp", "--no-playlist", "-j", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout)

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
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:10]
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(seconds=JOB_TIMEOUT)
    jobs[job_id] = {
        "job_id": job_id,
        "status": "downloading",
        "stage": "downloading",
        "url": url,
        "title": title,
        "started_at": now.isoformat(),
        "deadline_at": deadline.isoformat(),
        "_started_monotonic": time.monotonic(),
        "_deadline_monotonic": time.monotonic() + JOB_TIMEOUT,
        "_timed_out": threading.Event(),
        "_process_lock": threading.Lock(),
        "_active_process": None,
    }
    logger.info("job_id=%s stage=downloading", job_id)

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "stage": job.get("stage"),
        "started_at": job.get("started_at"),
        "deadline_at": job.get("deadline_at"),
        "error": job.get("error"),
        "filename": job.get("filename"),
        "progress": job.get("progress"),
        "file_path": job.get("file_path"),
        "width": job.get("width"),
        "height": job.get("height"),
        "duration": job.get("duration"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
