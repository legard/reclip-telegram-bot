# ReClip Telegram Bot

A self-hosted Telegram bot that downloads media from YouTube, TikTok, Instagram, Twitter, Reddit, and 1000+ other sites. Powered by [reclip](https://github.com/averygan/reclip) and yt-dlp.

Send a link, pick your format and quality, get the file delivered right in the chat.

![Bot conversation](images/bot.png)

## Features

- Multi-platform support (YouTube, TikTok, Instagram, Twitter, Reddit, and 1000+ more via yt-dlp)
- Format selection (MP4 video or MP3 audio)
- Quality picker with all available resolutions
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
                               │
                               ▼
                            SQLite
```

Four Docker containers via docker-compose:
1. **reclip** - Media download engine with REST API and web UI
2. **bot** - Telegram bot that wraps reclip's API
3. **telegram-bot-api** - Self-hosted Telegram Bot API server for 2GB upload limit
4. **dashboard** - Admin panel with download stats, history, errors, and file management

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
TELEGRAM_API_ID=your-api-id
TELEGRAM_API_HASH=your-api-hash
```

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
| `TELEGRAM_API_ID` | (required) | Telegram API ID from my.telegram.org |
| `TELEGRAM_API_HASH` | (required) | Telegram API hash from my.telegram.org |
| `MAX_CONCURRENT_DOWNLOADS` | 3 | Max parallel downloads |
| `CLEANUP_MAX_AGE_HOURS` | 1 | Delete files older than this |
| `CLEANUP_MAX_DISK_MB` | 0 | Max disk usage before cleanup; `0` disables size cleanup while age cleanup remains active |
| `CLEANUP_INTERVAL_SECONDS` | 300 | Cleanup check interval |
| `DASHBOARD_USER` | admin | Dashboard login username |
| `DASHBOARD_PASSWORD` | (required) | Dashboard login password |
| `DASHBOARD_PORT` | 8080 | Dashboard port on host |
| `DASHBOARD_SECRET_KEY` | change-me | Cookie signing key |

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
4. Bot displays thumbnail, title, platform, and format buttons (MP4/MP3)
5. You tap MP4 to see quality options (1080p, 720p, etc.) or MP3 for audio
6. Bot starts the download and shows real-time progress
7. Bot uploads the file to the Telegram chat
8. Cleanup task removes old files automatically

## Development

### Running locally (without Docker)

```bash
# Start reclip
cd reclip && pip install flask yt-dlp && python app.py &

# Start the bot
cd bot && pip install -r requirements.txt
BOT_TOKEN=your-token RECLIP_URL=http://localhost:8899 DOWNLOADS_PATH=../reclip/downloads python bot.py
```

### Project structure

```
reclip_bot/
├── docker-compose.yml      # 4 services: reclip, bot, telegram-bot-api, dashboard
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
