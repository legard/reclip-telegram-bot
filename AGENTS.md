# AGENTS.md — ReClip Telegram Bot

Compact instruction file for OpenCode sessions working in this repository.

## Project Overview

Self-hosted Telegram bot that downloads media from YouTube, TikTok, Instagram, and 1000+ other sites. Built from five Dockerized services:

| Service | Tech | Port | Role |
|---------|------|------|------|
| `reclip/` | Flask + yt-dlp | 8899 | Download engine with REST API |
| `bot/` | python-telegram-bot + httpx | — | Telegram bot client |
| `dashboard/` | FastAPI + aiosqlite + Jinja2 | 8080 | Admin panel with stats |
| `telegram-bot-api` | aiogram/telegram-bot-api | 8081 | Self-hosted Bot API for 2GB uploads |
| `bgutil` | BGUtil 1.3.1 provider | internal 4416 | YouTube Proof-of-Origin token provider for ReClip |

## Running Locally (Without Docker Compose)

Start services in this order — each needs its own terminal:

```bash
# 1. BGUtil provider (required for Russian YouTube audio)
docker run --rm -p 4416:4416 brainicism/bgutil-ytdlp-pot-provider:1.3.1

# 2. Reclip (download engine)
cd reclip
# Deno >= 2.3.0 must be available in PATH.
# This installs yt-dlp's bundled EJS and the pinned BGUtil provider plugin.
pip install -r requirements.txt
POT_PROVIDER_URL=http://localhost:4416 python app.py  # listens on 8899

# 3. Dashboard (admin panel)
cd dashboard
pip install -r requirements.txt
ADMIN_PASSWORD=changeme DB_PATH=./reclip.db \
  uvicorn main:create_app --factory --host 0.0.0.0 --port 8080

# 4. Bot (Telegram client)
cd bot
pip install -r requirements.txt
BOT_TOKEN=<token> RECLIP_URL=http://localhost:8899 \
  ALLOWED_USER_IDS=<your-telegram-user-id> BOT_DB_PATH=./bot.db \
  DOWNLOADS_PATH=../reclip/downloads python bot.py
```

The ReClip runtime contract is Deno >= 2.3.0 in `PATH`, bundled EJS and the
BGUtil plugin from `reclip/requirements.txt`, and a reachable BGUtil 1.3.1
endpoint at `POT_PROVIDER_URL`. Docker Compose supplies the internal endpoint
as `http://bgutil:4416`; local runs must point `POT_PROVIDER_URL` at their
separately started provider.

The bot waits up to 60s for the self-hosted Bot API server on startup. In Docker it talks to `http://telegram-bot-api:8081`; locally you need that container running too, or the bot will warn and start anyway (uploads >20MB will fail).

## Running Tests

```bash
# Bot tests
cd bot
pip install -r requirements.txt
pip install pytest pytest-asyncio
python -m pytest tests/ -v

# ReClip API and Docker Compose contract tests (run from repository root)
cd ..
python -m pytest reclip/tests/ -v

# Dashboard tests
cd dashboard
pip install -r requirements.txt
pip install pytest pytest-asyncio httpx
python -m pytest tests/ -v
```

**Critical:** Dashboard tests must set `DB_PATH`, `ADMIN_PASSWORD`, and `SECRET_KEY` **before** importing `db.py` or `main.py`. The test files do this via `os.environ` at the top of the file. If you add new dashboard test files, copy that pattern or imports will create the DB in the wrong place.

## Architecture Notes

### Reclip API Surface (`reclip/app.py`)
- `POST /api/info` — returns title, thumbnail, duration, uploader, extractor, and available quality formats
- `POST /api/download` — spawns a threaded yt-dlp download, returns `job_id`
- `GET /api/status/<job_id>` — returns `status` (`queued` | `downloading` | `postprocessing` | `done` | `error` | `cancelled`), progress dict, file metadata, and stable `error_code`
- `POST /api/cancel/<job_id>` — idempotently cancels queued/running jobs and cleans partial output
- `GET /api/file/<job_id>` — serves finished file as attachment

### Bot → Dashboard Events (`bot/event_client.py`)
The bot sends fire-and-forget HTTP POSTs to `DASHBOARD_URL/api/events` with `type: download_start | progress | download_done | download_error`. These are best-effort; failures are logged at DEBUG and ignored.

### Download Flow
1. User sends URL → bot calls `reclip_client.get_info()`
2. Bot shows thumbnail + format buttons (MP4 / MP3)
3. MP4 path shows quality picker; MP3 path starts audio download directly
4. Bot calls `start_download()` → polls `poll_status()` every 2s
5. Progress updates edit the Telegram message in real time
6. On `status: done`, bot uploads the file via the self-hosted Bot API

### Post-Processing in Reclip
- yt-dlp downloads to `DOWNLOADS_PATH/<job_id>.<ext>`
- AV1/VP9/VP8 videos are transcoded to H264 via ffmpeg (Telegram compatibility)
- Other MP4s get `-movflags +faststart` via ffmpeg copy
- File duration/width/height extracted with ffprobe

### Concurrency & Limits
- Reclip uses `threading.Semaphore(MAX_CONCURRENT_DOWNLOADS)` (default 3)
- ReClip owns one hard `JOB_TIMEOUT` deadline (default 9000 seconds / 150 minutes) across yt-dlp, ffmpeg, faststart, and ffprobe. `DOWNLOAD_TIMEOUT` is a legacy fallback only when `JOB_TIMEOUT` is absent.
- Bot has a 10-minute in-memory session TTL (`STATE_TTL = 600`)

### Cleanup (`bot/cleanup.py`)
Background asyncio task that runs every `CLEANUP_INTERVAL_SECONDS` (default 300):
1. Deletes files older than `CLEANUP_MAX_AGE_HOURS` (default 1)
2. If disk usage exceeds `CLEANUP_MAX_DISK_MB` (default 5000), deletes oldest files first

## Environment Variables

Required:
- `BOT_TOKEN` — from @BotFather
- `ALLOWED_USER_IDS` — comma-separated positive Telegram user IDs permitted to use the private bot
- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` — from my.telegram.org
- `DASHBOARD_PASSWORD` — dashboard login password

Commonly overridden for local dev:
- `RECLIP_URL=http://localhost:8899`
- `TELEGRAM_BOT_API_URL=http://localhost:8081`
- `DOWNLOADS_PATH=./downloads`
- `BOT_DB_PATH=./bot.db`
- `DB_PATH=./reclip.db`
- `SECRET_KEY=change-me` (dashboard cookie signing)

See `.env.example` for the full list with defaults.

The allowlist is mandatory: the bot exits at startup if `ALLOWED_USER_IDS` is
missing, empty, or contains non-positive/non-numeric IDs. Use a comma-separated
value such as `ALLOWED_USER_IDS=123,456`. In Compose, `BOT_DB_PATH=/data/bot.db`
is backed by the durable `bot-data` volume, which preserves a user's saved
format, quality, and audio preferences across bot restarts.

## Telegram UX Contract

- User-facing status, controls, and errors are in Russian. For eligible
  YouTube videos, `MP4 • RU` selects a separate Russian audio track; it does
  not silently replace a missing track with the original audio.
- `/mp3 <ссылка>` downloads MP3 immediately. `/mp4 <ссылка>` downloads the
  best MP4 immediately, and `/best <ссылка>` is its one-shot alias. These
  commands bypass the format picker.
- `/settings` saves default format, quality, and audio preference in
  `BOT_DB_PATH`. **Сбросить настройки** removes those saved values and restores
  MP4, best quality, and original audio.
- Retry cards hold semantic intent for 24 hours and can be used once. They are
  intentionally in memory, so all outstanding retry buttons expire after a bot
  restart and users must send the link again.
- Cancellation is available during selection, download, and post-processing,
  but only before Telegram upload begins. Uploading files cannot be cancelled.

## Code Conventions

- **No linting / formatting / typechecking tooling is configured.** There is no black, ruff, mypy, or pre-commit setup.
- Imports use `sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))` in test files to reach parent modules.
- Dashboard DB uses SQLite with WAL mode (`PRAGMA journal_mode=WAL`).
- Auth is session-cookie based (itsdangerous URLSafeTimedSerializer), not JWT or OAuth.
- All event_client calls are fire-and-forget with a 2-second timeout; they must never crash the bot.

## Common Gotchas

- **Shared volume:** `downloads` is a Docker shared volume between reclip, bot, and dashboard. Locally, all three services must point to the same filesystem path.
- **Bot API server first startup:** The self-hosted Bot API downloads Telegram server data on first run. This can take 30–60 seconds. The bot polls `wait_for_bot_api()` for up to 60s.
- **Dashboard DB path:** `_db_path()` re-reads `DB_PATH` from env on every call so tests can override it dynamically.
- **Reclip job state is in-memory:** `jobs = {}` in `reclip/app.py` is not persistent. Restarting reclip loses active download state.
- **FFmpeg dependency:** The reclip Dockerfile installs ffmpeg. Locally you need it installed (`apt install ffmpeg` or equivalent).
