# Russian YouTube Audio Track Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить в Telegram-бот условную кнопку `MP4 • RU`, которая скачивает YouTube-видео с отдельной русской дорожкой в выбранном разрешении без fallback на исходный звук.

**Architecture:** ReClip выполняет обычный probe для существующего контракта и отдельный локализованный YouTube probe для обнаружения русской дорожки; чистые правила языка, разрешений и yt-dlp selector изолируются в `reclip/youtube_audio.py`. Telegram-бот передаёт в расширенный `/api/download` только `audio_language="ru"` и высоту, а ReClip повторно проверяет свежие метаданные внутри общего job deadline перед запуском языково-ограниченного selector. Deno и EJS входят в multi-arch образ ReClip, а выпуск заканчивается immutable digest rollout через отдельный Ansible-worktree и Telegram smoke-тест.

**Tech Stack:** Python 3.12, Flask, yt-dlp 2026.x with `yt-dlp[default]`, Deno 2.9.4, pytest, python-telegram-bot, httpx, Docker Buildx, GitHub Actions, Ansible.

## Global Constraints

- Утверждённый пользовательский контракт находится в `docs/superpowers/specs/2026-08-01-russian-autodub-audio-design.md`; изменение этого контракта требует нового согласования.
- Кнопка называется ровно `MP4 • RU` и показывается только для YouTube-видео с отдельной русской дорожкой; исходный русский звук отдельной кнопки не создаёт.
- Принимаются `ru` и региональные варианты `ru-*`; первая версия API принимает только точное `audio_language: "ru"`.
- Callback и API оперируют высотой, а не нестабильным YouTube format ID; `Best quality` передаёт `height=None`.
- Обычные MP4 и MP3 сценарии, веб-интерфейс ReClip, semaphore, единый `JOB_TIMEOUT`, постобработка, очистка и локальный Telegram Bot API сохраняют текущее поведение.
- Русский selector не содержит общей ветки `best`/`bestaudio` без фильтра `language=ru`; исчезнувшая дорожка завершает job точной ошибкой `Russian audio track is no longer available. Please retry.`.
- Не добавлять cookies, Visitor Data, PO Token, удалённую загрузку EJS или секреты без отдельного согласования пользователя.
- EJS поставляется внутри образа через `yt-dlp[default]`; Deno находится в `PATH`, поддерживает `linux/amd64` и `linux/arm64` и имеет версию не ниже официального минимума 2.3.0.
- Следовать TDD: сначала наблюдаемое падение нового теста, затем минимальная реализация, затем узкий и полный test run.
- Release version для этой функции — `v0.1.6`: remote tag `v0.1.5` уже указывает на `d827367`.
- Перед Ansible-правками повторно проверить состояние репозитория. На момент написания `/Users/tabolin/projects/orangepi-ansible` находится на `c399ed7`, опережает `origin/main` на 55 коммитов и содержит несвязанные незакоммиченные docs; удалённый worktree `.../kelp` не использовать и грязный `main` не изменять.

## File Structure

- Create `reclip/youtube_audio.py` — чистые правила распознавания YouTube/русского языка, определения исходной дорожки, построения списка высот, проверки свежих форматов и безопасного yt-dlp selector.
- Create `reclip/tests/test_youtube_audio.py` — табличные unit-тесты доменных правил и доказательство отсутствия fallback.
- Modify `reclip/app.py` — orchestration обычного/локализованного probe, API schema/validation, job arguments и свежая проверка перед download.
- Modify `reclip/tests/test_app.py` — Flask API и job orchestration tests при сохранении существующих deadline/process tests.
- Modify `reclip/Dockerfile` — multi-stage Deno binary и установка `requirements.txt`.
- Modify `reclip/requirements.txt` — `yt-dlp[default]` вместо минимального `yt-dlp`.
- Modify `bot/reclip_client.py` — необязательные `audio_language` и `height` в download payload.
- Modify `bot/tests/test_reclip_client.py` — точные payload tests для legacy и RU запросов.
- Modify `bot/handlers.py` — условная кнопка, отдельные RU callbacks по высоте и проброс языка/высоты до клиента.
- Modify `bot/tests/test_handlers.py` — keyboard, callback и download propagation tests.
- Modify `.github/workflows/release.yml` — build/run runtime-contract image до release job.
- Modify `.github/tests/test_ci_config.py` — проверка обязательных runtime-contract CI steps.
- Modify `README.md` — пользовательский поток `MP4 • RU` и локальная зависимость Deno.
- During rollout, modify `/Users/tabolin/projects/orangepi-ansible/group_vars/all/docker_services.yml` — три новых immutable manifest digest.
- During rollout, modify `/Users/tabolin/projects/orangepi-ansible/tests/check_reclip_inventory.sh` — exact digest expectations.
- During rollout, modify `/Users/tabolin/projects/orangepi-ansible/docs/runbooks/reclip-orangepi.md` — `v0.1.6` и RU smoke acceptance.

---

### Task 1: Docker runtime contract and extraction feasibility gate

**Files:**
- Modify: `reclip/Dockerfile:1-16`
- Modify: `reclip/requirements.txt:1-2`

**Interfaces:**
- Consumes: official [yt-dlp EJS setup](https://github.com/yt-dlp/yt-dlp/wiki/EJS) and [Deno Docker binary image](https://github.com/denoland/deno_docker#using-your-own-base-image).
- Produces: `deno` in `/usr/local/bin`, bundled `yt-dlp-ejs`, and a verified baseline extractor argument `youtube:lang=ru` for all later ReClip probes.

- [ ] **Step 1: Build the baseline image and observe the missing runtime contracts**

```bash
docker build -t reclip-russian-audio-red ./reclip
docker run --rm reclip-russian-audio-red deno --version
docker run --rm reclip-russian-audio-red \
  python -c 'from importlib.metadata import version; print(version("yt-dlp-ejs"))'
```

Expected RED: image build succeeds; `deno --version` fails because the executable is absent, and the metadata command fails with `PackageNotFoundError` because plain `yt-dlp` does not install bundled EJS. These are the two production changes the runtime gate must catch.

- [ ] **Step 2: Make the minimal reproducible runtime change**

Replace `reclip/Dockerfile` with:

```dockerfile
FROM denoland/deno:bin-2.9.4 AS deno

FROM python:3.12-slim

COPY --from=deno /deno /usr/local/bin/deno

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8899
ENV HOST=0.0.0.0
ENV DOWNLOADS_PATH=/downloads
CMD ["python", "app.py"]
```

Replace `reclip/requirements.txt` with:

```text
flask
yt-dlp[default]
```

- [ ] **Step 3: Build the exact candidate image and verify runtime/EJS without network-time component downloads**

```bash
docker build -t reclip-russian-audio-feasibility ./reclip
docker run --rm reclip-russian-audio-feasibility deno --version
docker run --rm reclip-russian-audio-feasibility \
  python -c 'from importlib.metadata import version; print(version("yt-dlp")); print(version("yt-dlp-ejs"))'
```

Expected: image builds; Deno reports `2.9.4`; both Python distributions print installed versions. Do not add `--remote-components` because EJS must already be in the image.

- [ ] **Step 4: Run the localized extraction three times inside the candidate image**

```bash
for attempt in 1 2 3; do
  docker run --rm reclip-russian-audio-feasibility \
    yt-dlp --no-playlist -J --extractor-args 'youtube:lang=ru' \
    'https://www.youtube.com/watch?v=M6mYodf0dJM' |
  python -c '
import json, sys
info = json.load(sys.stdin)
formats = info.get("formats", [])
ru = [f for f in formats if str(f.get("language", "")).lower() == "ru" or str(f.get("language", "")).lower().startswith("ru-")]
assert any(f.get("acodec") not in (None, "none") for f in ru), "no Russian audio format"
print(sorted({f.get("height") for f in ru if f.get("height")}, reverse=True))
'
done
```

Expected: all three pipelines exit 0 and print at least one Russian-audio height or confirm an audio-only Russian format. Warnings about SABR/PO Token do not change the baseline while the required formats remain available.

- [ ] **Step 5: Enforce the feasibility decision gate**

If any attempt cannot expose Russian audio with Deno + bundled EJS + `youtube:lang=ru`, stop implementation and preserve the command output. Do not add cookies, Visitor Data, PO Token, `mweb`, or remote EJS; report the failed gate for explicit user direction. If all attempts pass, later code uses only `youtube:lang=ru`; Deno remains enabled by yt-dlp's default runtime policy, so no redundant `--js-runtimes` flag is added.

- [ ] **Step 6: Commit the runtime contract**

```bash
git add reclip/Dockerfile reclip/requirements.txt
git commit -m "Добавить runtime для русских дорожек YouTube"
```

---

### Task 2: Pure Russian-track discovery and selector module

**Files:**
- Create: `reclip/youtube_audio.py`
- Create: `reclip/tests/test_youtube_audio.py`

**Interfaces:**
- Consumes: normal and localized yt-dlp info dictionaries.
- Produces: `RUSSIAN_EXTRACTOR_ARGS: str`, `empty_russian_audio() -> dict`, `is_youtube_info(info) -> bool`, `is_youtube_url(url) -> bool`, `russian_audio_summary(default_info, localized_info) -> dict`, `russian_download_available(localized_info, height) -> bool`, and `build_russian_format_selector(height) -> str`.

- [ ] **Step 1: Write failing discovery tests with representative format metadata**

```python
import pytest

from reclip.youtube_audio import (
    build_russian_format_selector,
    is_youtube_url,
    russian_audio_summary,
    russian_download_available,
)


def audio(format_id, language, preference, note="", *, height=None, video=False):
    return {
        "format_id": format_id,
        "language": language,
        "language_preference": preference,
        "format_note": note,
        "acodec": "aac",
        "vcodec": "h264" if video else "none",
        "height": height,
    }


def video(format_id, height, codec="h264"):
    return {"format_id": format_id, "height": height, "vcodec": codec, "acodec": "none"}


def test_audio_only_russian_track_uses_default_video_heights_and_deduplicates():
    default = {
        "extractor_key": "Youtube",
        "formats": [
            audio("en", "en-US", 10, "original"),
            video("v1080-a", 1080), video("v1080-b", 1080, "vp9"), video("v720", 720),
        ],
    }
    localized = {"formats": [audio("ru.10", "ru", -10, "auto-dubbed")]}
    assert russian_audio_summary(default, localized) == {
        "available": True,
        "formats": [{"height": 1080, "label": "1080p"}, {"height": 720, "label": "720p"}],
    }


def test_combined_russian_track_uses_only_its_own_heights():
    default = {"extractor": "youtube", "formats": [audio("en", "en", 10), video("v1080", 1080)]}
    localized = {"formats": [audio("ru720", "ru-RU", -10, height=720, video=True)]}
    assert russian_audio_summary(default, localized)["formats"] == [{"height": 720, "label": "720p"}]


@pytest.mark.parametrize("extractor", ["vimeo", "generic"])
def test_non_youtube_has_no_russian_option(extractor):
    default = {"extractor": extractor, "formats": [audio("en", "en", 10)]}
    assert russian_audio_summary(default, {"formats": [audio("ru", "ru", -10)]}) == {
        "available": False, "formats": []
    }


def test_original_russian_audio_has_no_separate_option():
    default = {"extractor": "youtube", "formats": [audio("ru", "ru-RU", 10, "original"), video("v", 720)]}
    localized = {"formats": [audio("ru-dub", "ru", -10)]}
    assert russian_audio_summary(default, localized)["available"] is False


def test_fresh_availability_checks_requested_height():
    localized = {"formats": [audio("ru", "ru", -10), video("v720", 720)]}
    assert russian_download_available(localized, 720) is True
    assert russian_download_available(localized, 1080) is False
    assert russian_download_available(localized, None) is True


def test_selector_has_language_filter_in_every_branch_and_never_falls_back():
    selector = build_russian_format_selector(720)
    branches = selector.split("/")
    assert len(branches) == 4
    assert all("language~=" in branch for branch in branches)
    assert all("height=720" in branch for branch in branches)
    assert "bestaudio" not in selector
    assert not selector.endswith("/best")


@pytest.mark.parametrize("url", ["https://youtube.com/watch?v=x", "https://www.youtube.com/shorts/x", "https://youtu.be/x"])
def test_youtube_url_hosts_are_accepted(url):
    assert is_youtube_url(url) is True


def test_lookalike_youtube_host_is_rejected():
    assert is_youtube_url("https://youtube.com.evil.example/watch?v=x") is False
```

- [ ] **Step 2: Run the new module tests and observe import failure**

Run: `python -m pytest reclip/tests/test_youtube_audio.py -v`

Expected: collection FAILS with `ModuleNotFoundError: No module named 'reclip.youtube_audio'`.

- [ ] **Step 3: Implement the pure module with exact language and ordering rules**

```python
import re
from urllib.parse import urlparse


RUSSIAN_EXTRACTOR_ARGS = "youtube:lang=ru"
RUSSIAN_LANGUAGE = re.compile(r"^ru(?:-|$)", re.IGNORECASE)
RUSSIAN_FILTER = "[language~='^ru(?:-|$)']"


def empty_russian_audio():
    return {"available": False, "formats": []}


def _has_audio(fmt):
    return fmt.get("acodec") not in (None, "none")


def _has_video(fmt):
    return fmt.get("vcodec") not in (None, "none")


def _is_russian(language):
    return bool(RUSSIAN_LANGUAGE.match(str(language or "")))


def is_youtube_info(info):
    extractor = info.get("extractor_key") or info.get("extractor") or ""
    return str(extractor).lower() == "youtube"


def is_youtube_url(url):
    host = (urlparse(url).hostname or "").lower()
    return host in {"youtube.com", "youtu.be"} or host.endswith(".youtube.com")


def _original_audio_language(info):
    audio_formats = [fmt for fmt in info.get("formats", []) if _has_audio(fmt) and fmt.get("language")]
    if not audio_formats:
        return None
    marked = [
        fmt for fmt in audio_formats
        if "original" in f'{fmt.get("format_note", "")} {fmt.get("format", "")}'.lower()
    ]
    candidates = marked or audio_formats
    return max(
        candidates,
        key=lambda fmt: fmt.get("language_preference")
        if isinstance(fmt.get("language_preference"), (int, float)) else float("-inf"),
    ).get("language")


def _height_options(heights):
    return [{"height": height, "label": f"{height}p"} for height in sorted(heights, reverse=True)]


def russian_audio_summary(default_info, localized_info):
    if not is_youtube_info(default_info) or _is_russian(_original_audio_language(default_info)):
        return empty_russian_audio()
    localized = localized_info.get("formats", [])
    russian_audio_only = any(_has_audio(fmt) and not _has_video(fmt) and _is_russian(fmt.get("language")) for fmt in localized)
    source = default_info.get("formats", []) if russian_audio_only else localized
    heights = {
        int(fmt["height"])
        for fmt in source
        if fmt.get("height") and _has_video(fmt)
        and (
            (russian_audio_only and not _has_audio(fmt))
            or (_has_audio(fmt) and _is_russian(fmt.get("language")))
        )
    }
    formats = _height_options(heights)
    return {"available": bool(formats), "formats": formats}


def russian_download_available(localized_info, height):
    formats = localized_info.get("formats", [])
    height_matches = lambda fmt: height is None or fmt.get("height") == height
    russian_audio_only = any(_has_audio(fmt) and not _has_video(fmt) and _is_russian(fmt.get("language")) for fmt in formats)
    separate = russian_audio_only and any(_has_video(fmt) and not _has_audio(fmt) and height_matches(fmt) for fmt in formats)
    combined = any(
        _has_video(fmt) and _has_audio(fmt) and _is_russian(fmt.get("language")) and height_matches(fmt)
        for fmt in formats
    )
    return separate or combined


def build_russian_format_selector(height):
    height_filter = "" if height is None else f"[height={height}]"
    h264 = "[vcodec~='^(avc|h264)']"
    return "/".join([
        f"bv{h264}{height_filter}+ba{RUSSIAN_FILTER}",
        f"b{h264}{height_filter}{RUSSIAN_FILTER}",
        f"bv{height_filter}+ba{RUSSIAN_FILTER}",
        f"b{height_filter}{RUSSIAN_FILTER}",
    ])
```

- [ ] **Step 4: Run focused tests and inspect the selector string**

Run: `python -m pytest reclip/tests/test_youtube_audio.py -v`

Expected: all tests pass; selectors prefer H.264 in branches 1-2, allow other codecs in branches 3-4, and every branch includes the Russian regex.

- [ ] **Step 5: Commit the domain module**

```bash
git add reclip/youtube_audio.py reclip/tests/test_youtube_audio.py
git commit -m "Описать выбор русской аудиодорожки"
```

---

### Task 3: Extend `/api/info` with best-effort Russian discovery

**Files:**
- Modify: `reclip/app.py:1-16,413-457`
- Modify: `reclip/tests/test_app.py`

**Interfaces:**
- Consumes: `is_youtube_info`, `RUSSIAN_EXTRACTOR_ARGS`, `russian_audio_summary`, and `empty_russian_audio` from Task 2.
- Produces: `build_info_command(url, russian=False) -> list[str]`, `fetch_info(url, russian=False, timeout=60) -> dict`, and an always-present `/api/info.russian_audio` object.

- [ ] **Step 1: Write failing Flask API tests for discovery and graceful degradation**

```python
def test_info_adds_localized_russian_audio_summary(monkeypatch):
    normal = {
        "title": "Video", "extractor": "youtube",
        "formats": [
            {"format_id": "en", "acodec": "aac", "vcodec": "none", "language": "en", "language_preference": 10, "format_note": "original"},
            {"format_id": "v720", "acodec": "none", "vcodec": "h264", "height": 720, "tbr": 1000},
        ],
    }
    localized = {"formats": [{"format_id": "ru", "acodec": "aac", "vcodec": "none", "language": "ru"}]}
    calls = []

    def fake_fetch(url, *, russian=False, timeout=60):
        calls.append((url, russian, timeout))
        return localized if russian else normal

    monkeypatch.setattr(app, "fetch_info", fake_fetch)
    response = app.app.test_client().post("/api/info", json={"url": "https://youtu.be/x"})
    assert response.status_code == 200
    assert response.get_json()["russian_audio"] == {
        "available": True, "formats": [{"height": 720, "label": "720p"}]
    }
    assert calls == [("https://youtu.be/x", False, 60), ("https://youtu.be/x", True, 60)]


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
    monkeypatch.setattr(app, "fetch_info", lambda url, *, russian=False, timeout=60: calls.append(russian) or {"extractor": "vimeo", "formats": []})
    response = app.app.test_client().post("/api/info", json={"url": "https://vimeo.com/1"})
    assert response.status_code == 200
    assert response.get_json()["russian_audio"] == {"available": False, "formats": []}
    assert calls == [False]
```

- [ ] **Step 2: Run the three API tests and verify they fail on missing field/helper**

Run: `python -m pytest reclip/tests/test_app.py -k 'info_' -v`

Expected: FAIL because `fetch_info` and `russian_audio` do not exist.

- [ ] **Step 3: Extract the current subprocess probe and add the localized command**

```python
def build_info_command(url, *, russian=False):
    command = ["yt-dlp", "--no-playlist", "-J"]
    if russian:
        command += ["--extractor-args", RUSSIAN_EXTRACTOR_ARGS]
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
```

Import Task 2 helpers at the top of `app.py` and replace the inline normal probe in `get_info()` with `fetch_info(url)`. Initialize `russian_audio = empty_russian_audio()`, run `fetch_info(url, russian=True)` only when `is_youtube_info(info)`, catch that second probe separately, log with `logger.warning("localized Russian probe failed url=%s: %s", url, error)`, and include `"russian_audio": russian_audio` in every successful JSON response.

- [ ] **Step 4: Run focused and complete ReClip tests**

Run: `python -m pytest reclip/tests/test_app.py -k 'info_' -v`

Expected: new info tests pass.

Run: `python -m pytest reclip/tests -v`

Expected: all existing process, timeout, compose, and discovery tests pass.

- [ ] **Step 5: Commit the API discovery contract**

```bash
git add reclip/app.py reclip/tests/test_app.py
git commit -m "Обнаруживать русскую дорожку в ReClip API"
```

---

### Task 4: Validate and execute Russian downloads without fallback

**Files:**
- Modify: `reclip/app.py:45-67,246-405,460-494`
- Modify: `reclip/tests/test_app.py`

**Interfaces:**
- Consumes: `is_youtube_url`, `RUSSIAN_EXTRACTOR_ARGS`, `russian_download_available`, and `build_russian_format_selector` from Task 2.
- Produces: extended `build_download_command(..., audio_language=None, height=None)`, `run_download(..., audio_language=None, height=None)`, `_do_download(..., audio_language=None, height=None)`, and `/api/download` validation for `audio_language`/`height`.

- [ ] **Step 1: Write failing command tests proving RU language filters and legacy stability**

```python
def test_russian_download_command_uses_localized_selector_without_fallback():
    command = app.build_download_command(
        "job-1", "https://youtu.be/x", "video", None,
        audio_language="ru", height=720,
    )
    selector = command[command.index("-f") + 1]
    assert command[command.index("--extractor-args") + 1] == "youtube:lang=ru"
    assert all("language~=" in branch for branch in selector.split("/"))
    assert all("height=720" in branch for branch in selector.split("/"))
    assert "bestaudio" not in selector


def test_legacy_best_video_command_is_unchanged():
    command = app.build_download_command("job-1", "https://example.com/video", "video", None)
    assert command[command.index("-f") + 1] == "bv*[vcodec~='^(avc|h264)']+ba/b[vcodec~='^(avc|h264)']/bv*+ba/b"
    assert "--extractor-args" not in command
```

- [ ] **Step 2: Write failing API validation tests before background job creation**

```python
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
    assert response.status_code == 200
    assert captured["args"][4:] == ("ru", 720)
    assert captured["started"] is True
```

- [ ] **Step 3: Write a failing fresh-metadata test for the exact disappearance error**

```python
def test_russian_job_stops_before_download_when_track_disappears(monkeypatch):
    job = app._new_job("job-1", "https://youtu.be/x", "Video")
    app.jobs["job-1"] = job
    monkeypatch.setattr(app, "_probe_job_info", lambda current_job, url: {"formats": []})
    monkeypatch.setattr(app, "build_download_command", lambda *args, **kwargs: pytest.fail("download command must not be built"))
    app._do_download("job-1", "https://youtu.be/x", "video", None, "ru", 720)
    assert job["status"] == "error"
    assert job["error"] == "Russian audio track is no longer available. Please retry."
```

Extract current job dictionary construction into `_new_job(job_id, url, title)` so this test and `start_download()` share the same deadline/process state.

- [ ] **Step 4: Extend signatures while keeping all existing callers valid**

```python
def build_download_command(job_id, url, format_choice, format_id, audio_language=None, height=None):
    # Keep the existing common command exactly as-is.
    if format_choice == "audio":
        command += ["-x", "--audio-format", "mp3"]
    elif audio_language == "ru":
        command += [
            "--extractor-args", RUSSIAN_EXTRACTOR_ARGS,
            "-f", build_russian_format_selector(height),
            "--merge-output-format", "mp4",
        ]
    elif format_id:
        command += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        command += ["-f", "bv*[vcodec~='^(avc|h264)']+ba/b[vcodec~='^(avc|h264)']/bv*+ba/b", "--merge-output-format", "mp4"]
    command.append(url)
    return command
```

Thread `audio_language=None, height=None` through `run_download()` and `_do_download()`; leave positional defaults so all legacy unit tests and calls still work.

- [ ] **Step 5: Add job-aware localized re-probe under the existing deadline timer**

```python
def _probe_job_info(job, url):
    result = _run_job_process(job, build_info_command(url, russian=True), capture_output=True)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return None
```

Start the existing `deadline_timer` before this probe. For `audio_language == "ru"`, call `_probe_job_info(job, url)` and `russian_download_available(info or {}, height)`; on false call `_finish_error(job, "Russian audio track is no longer available. Please retry.")`, cancel the timer, and return before constructing or starting the download command. The probe process must use `_run_job_process`, so `expire_job()` can terminate it and the one hard deadline still covers probe, yt-dlp, ffmpeg, faststart, and ffprobe.

- [ ] **Step 6: Add synchronous request validation and exact thread arguments**

```python
audio_language = data.get("audio_language")
height = data.get("height")

if audio_language not in (None, "ru"):
    return jsonify({"error": "Unsupported audio_language"}), 400
if height is not None and (isinstance(height, bool) or not isinstance(height, int) or height <= 0):
    return jsonify({"error": "height must be a positive integer"}), 400
if audio_language == "ru" and not is_youtube_url(url):
    return jsonify({"error": "Russian audio is only supported for YouTube URLs"}), 400
if audio_language == "ru" and format_choice != "video":
    return jsonify({"error": "Russian audio is only supported for video downloads"}), 400
if audio_language == "ru" and format_id is not None:
    return jsonify({"error": "format_id is not accepted for Russian audio"}), 400
```

Construct the thread with `args=(job_id, url, format_choice, format_id, audio_language, height)`. Store `audio_language` and `height` in the private job dictionary for diagnostics, but do not add them to `/api/status`.

- [ ] **Step 7: Run focused API/job tests and all ReClip regressions**

Run: `python -m pytest reclip/tests/test_app.py -k 'russian or legacy_best or download_rejects or download_starts' -v`

Expected: new command, validation, fresh-probe, and compatibility tests pass.

Run: `python -m pytest reclip/tests -v`

Expected: all ReClip tests pass, especially the pre-existing deadline, process-group, cleanup, semaphore, codec, and compose contracts.

- [ ] **Step 8: Commit download execution**

```bash
git add reclip/app.py reclip/tests/test_app.py
git commit -m "Скачивать MP4 с русской дорожкой"
```

---

### Task 5: Extend the bot HTTP client payload

**Files:**
- Modify: `bot/reclip_client.py:69-90`
- Modify: `bot/tests/test_reclip_client.py:102-127`

**Interfaces:**
- Consumes: `/api/download` fields from Task 4.
- Produces: `start_download(url, format, format_id, title, *, audio_language=None, height=None) -> str`.

- [ ] **Step 1: Add failing exact-payload tests for legacy and RU calls**

```python
@pytest.mark.asyncio
async def test_start_download_preserves_legacy_payload(mock_response):
    response = mock_response(200, {"job_id": "job-1"})
    with patch("reclip_client._client") as factory:
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(return_value=response)
        factory.return_value = client
        await start_download("https://youtu.be/x", "video", "22", "Video")
        client.post.assert_awaited_once_with(
            "/api/download",
            json={"url": "https://youtu.be/x", "format": "video", "title": "Video", "format_id": "22"},
            timeout=60.0,
        )


@pytest.mark.asyncio
async def test_start_download_sends_russian_height_without_format_id(mock_response):
    response = mock_response(200, {"job_id": "job-ru"})
    with patch("reclip_client._client") as factory:
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(return_value=response)
        factory.return_value = client
        await start_download(
            "https://youtu.be/x", "video", None, "Video",
            audio_language="ru", height=720,
        )
        client.post.assert_awaited_once_with(
            "/api/download",
            json={
                "url": "https://youtu.be/x", "format": "video", "title": "Video",
                "audio_language": "ru", "height": 720,
            },
            timeout=60.0,
        )
```

- [ ] **Step 2: Run the client tests and observe signature failure**

Run: `python -m pytest bot/tests/test_reclip_client.py -k 'start_download' -v`

Expected: RU test FAILS because `start_download` does not accept keyword-only fields.

- [ ] **Step 3: Implement optional fields without adding nulls to legacy payloads**

```python
async def start_download(
    url: str,
    format: str,
    format_id: str | None,
    title: str,
    *,
    audio_language: str | None = None,
    height: int | None = None,
) -> str:
    payload = {"url": url, "format": format, "title": title}
    if format_id:
        payload["format_id"] = format_id
    if audio_language is not None:
        payload["audio_language"] = audio_language
    if height is not None:
        payload["height"] = height
    # Keep the existing HTTP/error mapping unchanged below this point.
```

- [ ] **Step 4: Run focused and full bot client tests**

Run: `python -m pytest bot/tests/test_reclip_client.py -v`

Expected: all client and deadline-polling tests pass.

- [ ] **Step 5: Commit the client contract**

```bash
git add bot/reclip_client.py bot/tests/test_reclip_client.py
git commit -m "Передавать язык и высоту в ReClip"
```

---

### Task 6: Add the conditional Telegram RU flow

**Files:**
- Modify: `bot/handlers.py:385-403,406-557,557-655,676-689`
- Modify: `bot/tests/test_handlers.py`

**Interfaces:**
- Consumes: `/api/info.russian_audio` from Task 3 and extended `start_download` from Task 5.
- Produces: conditional `MP4 • RU`, callback namespaces `fmt:...:video_ru` and `ruqty:...:<height|best>`, and `download_and_send(..., audio_language=None, height=None)`.

- [ ] **Step 1: Write failing keyboard tests for visibility and height callback data**

```python
def button_texts(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def button_callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_format_buttons_show_ru_only_when_available():
    hidden = handlers._build_format_buttons(7, "abcd", {"available": False, "formats": []})
    shown = handlers._build_format_buttons(
        7, "abcd", {"available": True, "formats": [{"height": 720, "label": "720p"}]}
    )
    assert button_texts(hidden) == ["MP4", "MP3"]
    assert button_texts(shown) == ["MP4", "MP4 • RU", "MP3"]
    assert "fmt:7:abcd:video_ru" in button_callbacks(shown)


def test_russian_quality_buttons_store_height_not_format_id():
    markup = handlers._build_russian_quality_buttons(
        7, "abcd",
        [{"height": 1080, "label": "1080p"}, {"height": 720, "label": "720p"}],
    )
    assert button_callbacks(markup) == [
        "ruqty:7:abcd:1080", "ruqty:7:abcd:720", "ruqty:7:abcd:best",
    ]
    assert button_texts(markup) == ["1080p", "720p", "Best quality"]
```

- [ ] **Step 2: Write a failing callback propagation test**

```python
@pytest.mark.asyncio
async def test_russian_quality_callback_passes_language_and_integer_height(monkeypatch):
    calls = []
    entry = {"url": "https://youtu.be/x", "info": {"title": "Video"}, "created": 0, "user_id": 1}
    query = FakeQuery(data="ruqty:7:abcd:720", chat_id=10, message_id=7)
    handlers._state[handlers._state_key(10, 7, "abcd")] = entry

    async def fake_download(query_arg, entry_arg, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(handlers, "download_and_send", fake_download)
    await handlers.russian_quality_callback(FakeUpdate(query), None)
    await asyncio.sleep(0)
    assert calls == [{
        "format": "video", "format_id": None,
        "audio_language": "ru", "height": 720,
    }]
```

Use these exact test doubles and clear session state around every handler test:

```python
from types import SimpleNamespace


class FakeQuery:
    def __init__(self, *, data, chat_id, message_id):
        self.data = data
        self.message = SimpleNamespace(chat_id=chat_id, message_id=message_id)
        self.reply_markup = None
        self.edited_text = None

    async def answer(self):
        pass

    async def edit_message_reply_markup(self, *, reply_markup):
        self.reply_markup = reply_markup

    async def edit_message_text(self, text):
        self.edited_text = text


class FakeUpdate:
    def __init__(self, query):
        self.callback_query = query


@pytest.fixture(autouse=True)
def clear_handler_state():
    handlers._state.clear()
    yield
    handlers._state.clear()
```

- [ ] **Step 3: Run the new handler tests and verify missing helpers/callback fail**

Run: `python -m pytest bot/tests/test_handlers.py -k 'format_buttons or russian_quality' -v`

Expected: FAIL because format buttons lack the new argument and RU callback helpers do not exist.

- [ ] **Step 4: Implement conditional format and Russian quality keyboards**

```python
def _build_format_buttons(message_id, url_hash, russian_audio=None):
    buttons = [InlineKeyboardButton("MP4", callback_data=f"fmt:{message_id}:{url_hash}:video")]
    if (russian_audio or {}).get("available"):
        buttons.append(InlineKeyboardButton("MP4 • RU", callback_data=f"fmt:{message_id}:{url_hash}:video_ru"))
    buttons.append(InlineKeyboardButton("MP3", callback_data=f"fmt:{message_id}:{url_hash}:audio"))
    return InlineKeyboardMarkup([buttons])


def _build_russian_quality_buttons(message_id, url_hash, formats):
    buttons = [
        InlineKeyboardButton(
            fmt.get("label", f'{fmt["height"]}p'),
            callback_data=f'ruqty:{message_id}:{url_hash}:{fmt["height"]}',
        )
        for fmt in formats
    ]
    rows = [buttons[index:index + 3] for index in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton("Best quality", callback_data=f"ruqty:{message_id}:{url_hash}:best")])
    return InlineKeyboardMarkup(rows)
```

Pass `info.get("russian_audio")` at both `_build_format_buttons` call sites: initial `url_handler()` rendering and the `fmt == "back"` branch.

- [ ] **Step 5: Add the `video_ru` branch and a distinct `ruqty` handler**

In `format_callback()`, handle `fmt == "video_ru"` by reading `entry["info"]["russian_audio"]["formats"]`, rejecting an empty/stale list with `Session expired. Please send the link again.`, and rendering `_build_russian_quality_buttons(...)`.

Implement `russian_quality_callback()` with the same state lookup and expiry behavior as `quality_callback()`. Parse `best` as `height = None`; otherwise accept only decimal digits and convert to `int`. Schedule:

```python
download_and_send(
    query,
    entry,
    format="video",
    format_id=None,
    audio_language="ru",
    height=height,
)
```

Register it with `CallbackQueryHandler(russian_quality_callback, pattern=r"^ruqty:")`. Keep existing `fmt:` and `qty:` patterns unchanged.

- [ ] **Step 6: Thread optional language/height through download and events**

```python
async def download_and_send(
    query,
    entry: dict,
    format: str,
    format_id: str | None,
    *,
    audio_language: str | None = None,
    height: int | None = None,
):
    # Existing message setup remains unchanged.
    job_id = await start_download(
        url, format, format_id, title,
        audio_language=audio_language,
        height=height,
    )
```

For the dashboard start event use `quality=str(height) if height is not None else (format_id or "best")`; keep `format="video"` so existing dashboard schema and upload behavior do not change. Existing direct MP3/MP4 and normal quality callers omit both keyword-only arguments.

- [ ] **Step 7: Run focused handler tests and complete bot suite**

Run: `python -m pytest bot/tests/test_handlers.py -v`

Expected: conditional keyboard, callback payload, session expiry, and existing progress tests pass.

Run: `python -m pytest bot/tests -v`

Expected: all bot tests pass, including legacy MP4/MP3 client payloads, upload, URL extraction, cleanup, logging, and event-client best-effort behavior.

- [ ] **Step 8: Commit the Telegram flow**

```bash
git add bot/handlers.py bot/tests/test_handlers.py
git commit -m "Добавить выбор русской дорожки в Telegram"
```

---

### Task 7: CI runtime enforcement, documentation, and full regression gate

**Files:**
- Modify: `.github/workflows/release.yml:25-40`
- Modify: `.github/tests/test_ci_config.py:147-177`
- Modify: `README.md:10-15,145-153,153-166`

**Interfaces:**
- Consumes: buildable ReClip image and all feature tests from Tasks 1-6.
- Produces: CI proof that Deno and bundled EJS exist before multi-arch release; documented user/local-dev behavior.

- [ ] **Step 1: Write a failing workflow-contract test**

```python
def test_release_builds_and_executes_reclip_runtime_contract_image():
    workflow = load_yaml(WORKFLOW_PATH)
    commands = "\n".join(step.get("run", "") for step in workflow["jobs"]["test"]["steps"])
    assert "docker build -t reclip-runtime-contract ./reclip" in commands
    assert "docker run --rm reclip-runtime-contract deno --version" in commands
    assert 'version("yt-dlp-ejs")' in commands
```

- [ ] **Step 2: Run the CI config test and observe failure**

Run: `python -m pytest .github/tests/test_ci_config.py -k runtime_contract -v`

Expected: FAIL because workflow does not build or inspect the ReClip runtime image.

- [ ] **Step 3: Add native runtime image verification to the test job**

Add after `Run ReClip tests` in `.github/workflows/release.yml`:

```yaml
      - name: Build ReClip runtime contract image
        run: docker build -t reclip-runtime-contract ./reclip

      - name: Verify ReClip runtime contract
        run: |
          docker run --rm reclip-runtime-contract deno --version
          docker run --rm reclip-runtime-contract python -c 'from importlib.metadata import version; print(version("yt-dlp-ejs"))'
```

The existing release Buildx step remains responsible for `linux/amd64,linux/arm64`; do not duplicate remote YouTube extraction in CI because it is an unstable third-party network dependency.

- [ ] **Step 4: Update README with exact user and local-runtime behavior**

Add `MP4 • RU` to the feature list and download flow: it appears only when a separate Russian YouTube track is detected, opens Russian resolutions plus `Best quality`, and never substitutes original audio. In local ReClip prerequisites state: Deno >= 2.3.0 must be in `PATH`, and `pip install -r reclip/requirements.txt` installs bundled EJS through `yt-dlp[default]`. Keep ordinary MP4/MP3 instructions unchanged.

- [ ] **Step 5: Run every repository test suite from the repository root**

```bash
python -m pytest reclip/tests -v
python -m pytest bot/tests -v
python -m pytest dashboard/tests -v
python -m pytest .github/tests -v
```

Expected: every suite passes. For dashboard tests, rely on their existing top-of-file `DB_PATH`, `ADMIN_PASSWORD`, and `SECRET_KEY` initialization; do not import dashboard modules from a new unconfigured test file.

- [ ] **Step 6: Rebuild the final local image and run an API-level Docker smoke**

```bash
docker build -t reclip-russian-audio-final ./reclip
docker run --rm reclip-russian-audio-final deno --version
docker run --rm reclip-russian-audio-final \
  python -c 'from importlib.metadata import version; assert version("yt-dlp-ejs")'
```

Start the image with a temporary downloads directory and expose port 18899:

```bash
reclip_smoke_dir=$(mktemp -d)
docker run -d --name reclip-russian-audio-smoke \
  -p 127.0.0.1:18899:8899 \
  -v "$reclip_smoke_dir:/downloads" \
  reclip-russian-audio-final
curl --fail --retry 20 --retry-delay 1 http://127.0.0.1:18899/ >/dev/null
curl --fail -X POST http://127.0.0.1:18899/api/info \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=M6mYodf0dJM"}' |
python -c '
import json, sys
payload = json.load(sys.stdin)
russian = payload["russian_audio"]
heights = [item["height"] for item in russian["formats"]]
assert russian["available"] is True
assert heights == sorted(set(heights), reverse=True)
print(heights)
'
docker rm -f reclip-russian-audio-smoke
```

Expected: API assertion passes. Remove only the explicitly named smoke container; retain the temporary directory path until its contents have been inspected.

- [ ] **Step 7: Commit CI and documentation**

```bash
git add .github/workflows/release.yml .github/tests/test_ci_config.py README.md
git commit -m "Проверять русский YouTube runtime в CI"
```

---

### Task 8: Release `v0.1.6`, pin immutable digests, deploy, and accept

**Files:**
- Modify in isolated Ansible worktree: `group_vars/all/docker_services.yml:114-118`
- Modify in isolated Ansible worktree: `tests/check_reclip_inventory.sh:26-42`
- Modify in isolated Ansible worktree: `docs/runbooks/reclip-orangepi.md:8-27,67-82`

**Interfaces:**
- Consumes: merged/pushed ReClip commit with all CI checks green and GHCR version `0.1.6` manifest indexes.
- Produces: Orange Pi running exact new digests and an accepted Telegram MP4 with Russian speech.

- [ ] **Step 1: Run the pre-release verification skill and confirm a clean ReClip branch**

Use `superpowers:verification-before-completion`. Run the four test suites and final Docker runtime checks from Task 7 again, then:

```bash
git status --short --branch
git log --oneline --decorate -8
git tag --list 'v0.1.*' --sort=-version:refname
```

Expected: implementation branch is clean; all feature commits are present after `f51d51e`; `v0.1.6` does not exist.

- [ ] **Step 2: Finish and integrate the ReClip branch**

Use `superpowers:finishing-a-development-branch`; choose the user-approved integration route. Do not create a release until the resulting `main` commit is pushed and GitHub's test job is green.

- [ ] **Step 3: Create and monitor the `v0.1.6` release**

```bash
gh release create v0.1.6 --target main --title v0.1.6 --generate-notes
reclip_release_run=$(gh run list --workflow release.yml --event release --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$reclip_release_run" --exit-status
```

Expected: release workflow tests pass and publishes `reclip`, `bot`, and `dashboard` tags `0.1.6` plus an immutable `sha-` tag whose suffix is the released Git commit for both `linux/amd64` and `linux/arm64`.

- [ ] **Step 4: Resolve and verify the three manifest-index digests**

```bash
for service in reclip bot dashboard; do
  image="ghcr.io/legard/reclip-telegram-bot/$service:0.1.6"
  docker buildx imagetools inspect "$image"
done
```

Expected for each image: one top-level `Digest: sha256:...` and manifests for both `linux/amd64` and `linux/arm64`. Record the top-level digest, not a platform child digest. Verify each digest again by inspecting the corresponding `reclip`, `bot`, or `dashboard` reference with its recorded `@sha256:` value.

- [ ] **Step 5: Create a safe Ansible worktree from the current local main**

Use `superpowers:using-git-worktrees`. First run:

```bash
git -C /Users/tabolin/projects/orangepi-ansible status --short --branch
git -C /Users/tabolin/projects/orangepi-ansible log -3 --oneline --decorate
git -C /Users/tabolin/projects/orangepi-ansible branch -avv
```

Do not edit the dirty primary checkout and do not recreate from stale `origin/main`. Create `.worktrees/reclip-russian-autodub-audio` from the current local `main` HEAD so its 55+ local commits are preserved; if that worktree/branch name now exists, inspect and reuse it only when its base is the current intended local main.

- [ ] **Step 6: Make the Ansible digest test fail with the released values**

In the isolated worktree, use `apply_patch` to replace the three old expected `reclip`, `bot`, and `dashboard` digests in `tests/check_reclip_inventory.sh` with the exact top-level digests recorded in Step 4. Leave `telegram-bot-api` unchanged.

Run: `bash tests/check_reclip_inventory.sh`

Expected: FAIL because `group_vars/all/docker_services.yml` still contains the old image references.

- [ ] **Step 7: Pin the same three exact digests and update the runbook**

Use `apply_patch` to map each Step 4 digest to the matching variable:

- `reclip` → `docker_services_reclip_reclip_image`
- `bot` → `docker_services_reclip_bot_image`
- `dashboard` → `docker_services_reclip_dashboard_image`

Keep the three full references under their exact package names (`reclip`, `bot`, `dashboard`) and append the matching 64-character lowercase hexadecimal digest printed in Step 4 after `@sha256:`. In `docs/runbooks/reclip-orangepi.md`, change the release prerequisite to `v0.1.6` and add the test URL plus acceptance: `MP4 • RU` visible, selected quality delivered as playable MP4, Russian speech audible, and temporary file removed after successful upload.

- [ ] **Step 8: Verify and commit only the Ansible rollout files**

```bash
bash tests/check_reclip_inventory.sh
bash tests/check_reclip_role_contract.sh
git diff --check
git status --short
git diff -- group_vars/all/docker_services.yml tests/check_reclip_inventory.sh docs/runbooks/reclip-orangepi.md
git add group_vars/all/docker_services.yml tests/check_reclip_inventory.sh docs/runbooks/reclip-orangepi.md
git commit -m "Выпустить русскую дорожку ReClip"
```

Expected: both shell contracts pass; the diff contains only the three synchronized image references and the `v0.1.6`/RU runbook update. Unrelated dirty files from the primary checkout are absent.

- [ ] **Step 9: Pre-pull exact arm64 images before changing the running stack**

For the four exact references now present in Ansible (three updated ReClip images plus unchanged Telegram Bot API), run the runbook preflight on `orangepi`: inspect for `linux/arm64` and pull every exact digest. If any inspect/pull fails, do not run the playbook and do not stop the existing stack.

- [ ] **Step 10: Deploy the pinned stack**

From the isolated Ansible worktree:

```bash
export ANSIBLE_LOCAL_TEMP=/tmp/orangepi-ansible-local
ansible-playbook playbooks/main.yml --limit orangepi --tags reclip \
  --vault-password-file /Users/tabolin/projects/orangepi-ansible/.vault_pass
```

Expected: `failed=0`; ReClip role validates immutable references, renders Compose, starts four services, and passes its API, Telegram, dashboard, polling, and listener health checks.

- [ ] **Step 11: Verify deployed digests and perform the Telegram acceptance test**

```bash
ssh orangepi 'cd /opt/docker/services/reclip && sudo docker compose -p reclip ps'
ssh orangepi 'cd /opt/docker/services/reclip && sudo docker compose -p reclip images'
```

Expected: all four services running; the three application container image IDs match the pinned manifest/platform resolution. Send `https://www.youtube.com/watch?v=M6mYodf0dJM` to the real bot, select `MP4 • RU`, choose a concrete height, and confirm the delivered MP4 is playable with Russian speech. Repeat with `Best quality`; confirm ordinary MP4 and MP3 still work and the successful uploads leave no corresponding temporary files in `/opt/docker/services/reclip/downloads`.

- [ ] **Step 12: Record final evidence before claiming completion**

Capture: ReClip commit/release URL, successful release workflow run, three top-level digests, Ansible commit, play recap, `docker compose ps/images`, and both Telegram RU smoke results. Only after all evidence is present mark the feature complete; if extraction needs PO Token/cookies at this stage, report the blocked external dependency without enabling either mechanism.
