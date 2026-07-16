import os
import uuid
import glob
import json
import re
import subprocess
import threading
from collections import deque
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
DOWNLOAD_DIR = os.environ.get("DOWNLOADS_PATH", os.path.join(os.path.dirname(__file__), "downloads"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", 3))
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", 900))
download_semaphore = threading.Semaphore(MAX_CONCURRENT_DOWNLOADS)

jobs = {}

PROGRESS_TEMPLATE = (
    'download:{"downloaded_bytes":%(progress.downloaded_bytes)s,'
    '"total_bytes":%(progress.total_bytes)s,'
    '"speed":%(progress.speed)s,'
    '"eta":%(progress.eta)s}'
)

DOWNLOAD_DIAGNOSTIC_LINE_LIMIT = 20
DOWNLOAD_ERROR_CHAR_LIMIT = 1500
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
TOKEN_PATTERN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")


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

    diagnostics.append(line)


def summarize_download_error(diagnostics):
    lines = list(diagnostics)[-DOWNLOAD_DIAGNOSTIC_LINE_LIMIT:]
    if not lines:
        return "Download failed"

    summary = "\n".join(lines)
    summary = URL_PATTERN.sub("[URL]", summary)
    summary = TOKEN_PATTERN.sub("[TOKEN]", summary)
    return summary[-DOWNLOAD_ERROR_CHAR_LIMIT:]


def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]

    if not download_semaphore.acquire(timeout=30):
        job["status"] = "error"
        job["error"] = "Too many concurrent downloads, please try again later"
        return

    try:
        _do_download(job_id, url, format_choice, format_id)
    finally:
        download_semaphore.release()


def _do_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    cmd = build_download_command(job_id, url, format_choice, format_id)

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )

    timed_out = threading.Event()

    def _kill_on_timeout():
        timed_out.set()
        try:
            process.kill()
        except Exception:
            pass

    deadline_timer = threading.Timer(DOWNLOAD_TIMEOUT, _kill_on_timeout)
    deadline_timer.daemon = True
    deadline_timer.start()

    stderr_lines = deque(maxlen=DOWNLOAD_DIAGNOSTIC_LINE_LIMIT)

    def _read_stderr():
        for line in process.stderr:
            line = line.rstrip("\n")
            record_download_output(job, stderr_lines, line)

    stderr_reader = threading.Thread(target=_read_stderr, daemon=True)
    stderr_reader.start()

    try:
        returncode = process.wait()
    finally:
        deadline_timer.cancel()
        stderr_reader.join(timeout=5)

    if timed_out.is_set():
        job["status"] = "error"
        job["error"] = f"Download timed out ({DOWNLOAD_TIMEOUT // 60} min limit)"
        return

    try:
        if returncode != 0:
            job["status"] = "error"
            job["error"] = summarize_download_error(stderr_lines)
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
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
            try:
                codec_probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=codec_name",
                     "-of", "default=noprint_wrappers=1:nokey=1", chosen],
                    capture_output=True, text=True, timeout=10,
                )
                vcodec = (codec_probe.stdout or "").strip().lower()
            except Exception:
                vcodec = ""

            if vcodec in ("av1", "vp9", "vp8"):
                transcoded = chosen + ".h264.mp4"
                try:
                    r = subprocess.run(
                        ["ffmpeg", "-y", "-i", chosen,
                         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                         "-c:a", "aac", "-b:a", "128k",
                         "-movflags", "+faststart",
                         transcoded],
                        capture_output=True, timeout=600,
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
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", chosen, "-c", "copy",
                         "-movflags", "+faststart", faststart_tmp],
                        capture_output=True, timeout=120,
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
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height:format=duration",
                     "-of", "json", chosen],
                    capture_output=True, text=True, timeout=10,
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

        job["status"] = "done"
        job["file"] = chosen
        job["file_path"] = os.path.abspath(chosen)
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        # Sanitize title for filename
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


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
    jobs[job_id] = {"status": "downloading", "url": url, "title": title}

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
