# ReClip Telegram Bot

A self-hosted Telegram bot that downloads media from YouTube, TikTok, Instagram, Twitter, Reddit, and 1000+ other sites. Powered by [reclip](https://github.com/averygan/reclip) and yt-dlp.

Send a link, pick your format and quality, get the file delivered right in the chat.

![Bot conversation](images/bot.png)

## Features

- Multi-platform support (YouTube, TikTok, Instagram, Twitter, Reddit, and 1000+ more via yt-dlp)
- Format selection (MP4 video or MP3 audio)
- Quality picker with all available resolutions
- `MP4 • RU` Russian-audio picker for YouTube videos with a separate Russian track
- Real-time download progress (percentage)
- Thumbnail preview with metadata (title, platform, duration)
- Files up to 2GB via self-hosted Telegram Bot API
- Automatic file cleanup (configurable age and disk limits)
- Concurrent download limiting (prevents resource exhaustion)
- Admin dashboard with download stats, history, error tracking, and disk management
- Web UI included (reclip's built-in web interface)

![Admin dashboard](images/admin.png)

## Architecture

```
Telegram User ──> Self-hosted Bot API (2GB limit)
                        │
                        ▼
                   bot (python-telegram-bot + httpx)
                   │              │
          HTTP     │              │  HTTP events
                   ▼              ▼
              reclip (Flask)   dashboard (FastAPI)
              port 8899        port 8080
                   ▲
                   │ internal-only
              bgutil (BGUtil 1.3.1)
              port 4416
                               │
                               ▼
                            SQLite
```

Five Docker containers via docker-compose:
1. **reclip** - Media download engine with REST API and web UI
2. **bot** - Telegram bot that wraps reclip's API
3. **telegram-bot-api** - Self-hosted Telegram Bot API server for 2GB upload limit
4. **dashboard** - Admin panel with download stats, history, errors, and file management
5. **bgutil** - Internal-only BGUtil 1.3.1 Proof-of-Origin token provider used by reclip for YouTube

## Quick Start

### Prerequisites

- Docker and Docker Compose
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- Telegram API credentials (from [my.telegram.org](https://my.telegram.org))

### Setup

1. Clone this repository:
```bash
git clone https://github.com/gth-ai/reclip-telegram-bot.git
cd reclip_bot
```

2. Copy the example environment file:
```bash
cp .env.example .env
```

3. Edit `.env` with your credentials:
```bash
BOT_TOKEN=your-bot-token-from-botfather
ALLOWED_USER_IDS=123,456
TELEGRAM_API_ID=your-api-id
TELEGRAM_API_HASH=your-api-hash
```

`ALLOWED_USER_IDS` is mandatory. The bot exits during startup when the
allowlist is missing, empty, or contains anything other than positive numeric
Telegram user IDs. This keeps the bot private; use a comma-separated list such
as `ALLOWED_USER_IDS=123,456` for multiple users.

To get Telegram API credentials:
- Go to https://my.telegram.org
- Log in with your phone number
- Go to "API Development Tools"
- Create a new application (any name/description works)
- Copy the `api_id` and `api_hash`

4. Start the services:
```bash
docker-compose up -d
```

5. Send a video link to your bot on Telegram.

### First Run Note

The self-hosted Bot API server downloads some data from Telegram on first startup. This can take a minute. The bot will start responding once the Bot API server is ready.

## Configuration

All configuration is via environment variables in `.env`:

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | (required) | Telegram bot token from @BotFather |
| `ALLOWED_USER_IDS` | (required) | Mandatory comma-separated positive Telegram user IDs allowed to use the private bot; an absent or invalid value stops startup |
| `BOT_DB_PATH` | `/data/bot.db` | SQLite database for durable per-user format, quality, and audio preferences; Docker stores it on the `bot-data` volume |
| `TELEGRAM_API_ID` | (required) | Telegram API ID from my.telegram.org |
| `TELEGRAM_API_HASH` | (required) | Telegram API hash from my.telegram.org |
| `MAX_CONCURRENT_DOWNLOADS` | 3 | Max parallel downloads |
| `JOB_TIMEOUT` | 9000 | Shared ReClip deadline in seconds (150 minutes), covering download and post-processing |
| `CLEANUP_MAX_AGE_HOURS` | 1 | Delete files older than this |
| `CLEANUP_MAX_DISK_MB` | 5000 | Max disk usage before cleanup; `0` disables size cleanup while age cleanup remains active |
| `CLEANUP_INTERVAL_SECONDS` | 300 | Cleanup check interval |
| `DASHBOARD_USER` | admin | Dashboard login username |
| `DASHBOARD_PASSWORD` | (required) | Dashboard login password |
| `DASHBOARD_PORT` | 8080 | Dashboard port on host |
| `DASHBOARD_SECRET_KEY` | change-me | Cookie signing key |

`JOB_TIMEOUT` is the shared ReClip deadline for both services. Existing
deployments that have only `DOWNLOAD_TIMEOUT` continue to use that value;
otherwise the default is 9000 seconds.

`BOT_DB_PATH` should point to durable storage. In Docker Compose it is
`/data/bot.db`, backed by the `bot-data` volume, so `/settings` preferences
survive bot container restarts. Set a writable local path when running the bot
outside Docker.

## Admin Dashboard

The admin dashboard is available at http://localhost:8080 after starting the services. Log in with the credentials from your `.env` file.

Pages:
- **Dashboard** — downloads today, active users, disk usage, error rate, charts
- **History** — full download log with filters and pagination
- **Errors** — failed downloads with error messages
- **Admin** — file management, system info, purge controls

## Web UI

The reclip web UI is available if you uncomment the `reclip-web` service in `docker-compose.yml`:

```yaml
reclip-web:
  extends:
    service: reclip
  ports:
    - "8899:8899"
```

Then access it at http://localhost:8899.

## How It Works

1. You send a URL to the bot
2. Bot sends "Fetching info..." immediately
3. Bot calls reclip's API to get video metadata
4. With no saved preferences, bot displays thumbnail, title, platform, and format buttons (MP4/MP3)
5. The final manual selection persists its format, quality, and audio choice for that user
6. Later ordinary URLs start automatically from those saved semantic preferences
7. The bot's messages, controls, and errors are in Russian. When YouTube exposes a separate Russian track, it also displays `MP4 • RU`; it opens Russian resolutions plus `Best quality`
8. You tap MP4 to see ordinary quality options (1080p, 720p, etc.) or MP3 for audio
9. `MP4 • RU` downloads only the selected Russian track and never substitutes the original audio
10. Bot starts the download and shows real-time progress
11. Bot uploads the file to the Telegram chat
12. Cleanup task removes old files automatically

### Commands, settings, and retries

- `/mp3 <ссылка>` starts an MP3 download immediately, without the format picker.
- `/mp4 <ссылка>` starts an MP4 download immediately in the best available
  quality, without the format picker. `/best <ссылка>` is the same one-shot
  best-quality MP4 action. These one-shot commands do not change saved
  preferences.
- `/settings` stores each user's default MP4/MP3 format, quality, and original
  or Russian-when-available audio choice in `BOT_DB_PATH`; a final manual
  selection for an ordinary URL saves the same preferences. The **Сбросить
  настройки** button deletes those saved preferences and returns the user to
  the defaults: MP4, best quality, and original audio.
- Retry buttons preserve only the intended URL, format, quality, audio choice,
  and owner for 24 hours. A retry is one-time and disappears after expiry; bot
  restarts also discard outstanding retry buttons, so the user must send the
  link again.
- **Отменить** is available while choosing or while ReClip is downloading and
  processing. Cancellation is allowed only before the Telegram upload starts;
  once upload has begun, the card no longer offers cancellation.

## Development

### Running locally (without Docker Compose)

ReClip needs Deno >= 2.3.0 in `PATH`. `pip install -r reclip/requirements.txt`
installs yt-dlp's bundled EJS and the BGUtil provider plugin. Start a BGUtil
1.3.1 endpoint separately and ensure it is reachable at `POT_PROVIDER_URL`
(Docker Compose uses its internal-only `http://bgutil:4416` endpoint):

```bash
# Start the BGUtil provider in another terminal
docker run --rm -p 4416:4416 brainicism/bgutil-ytdlp-pot-provider:1.3.1

# Start reclip
cd reclip && pip install -r requirements.txt
POT_PROVIDER_URL=http://localhost:4416 python app.py &

# Start the bot
cd bot && pip install -r requirements.txt
BOT_TOKEN=your-token ALLOWED_USER_IDS=123,456 BOT_DB_PATH=./bot.db RECLIP_URL=http://localhost:8899 DOWNLOADS_PATH=../reclip/downloads python bot.py
```

### Tests

From the repository root, run the ReClip API and Compose contract tests with:

```bash
python -m pytest reclip/tests/ -v
```

### Project structure

```
reclip_bot/
├── docker-compose.yml      # 5 services: reclip, bot, telegram-bot-api, dashboard, internal bgutil
├── .env.example             # Environment variables template
├── bot/
│   ├── bot.py               # Bot entry point
│   ├── handlers.py          # Telegram message/callback handlers
│   ├── reclip_client.py     # Async HTTP client for reclip API
│   ├── event_client.py      # Fire-and-forget events to dashboard
│   ├── cleanup.py           # Background file cleanup task
│   ├── requirements.txt
│   └── Dockerfile
├── dashboard/
│   ├── main.py              # FastAPI app with background tasks
│   ├── db.py                # SQLite queries (async via aiosqlite)
│   ├── auth.py              # Session cookie auth
│   ├── routes/              # API + page routes
│   ├── templates/           # Jinja2 templates (dark theme)
│   ├── static/              # CSS + Chart.js frontend
│   ├── requirements.txt
│   └── Dockerfile
└── reclip/                  # Fork of averygan/reclip with enhancements
    ├── app.py               # Flask API + web UI (with progress hooks)
    ├── Dockerfile
    ├── templates/
    └── static/
```

## Credits

- [reclip](https://github.com/averygan/reclip) by averygan - The media download engine
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) - The download backend
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) - Telegram bot framework

## License

MIT
