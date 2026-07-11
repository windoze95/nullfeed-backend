# NullFeed Backend

**A Self-Hosted YouTube Media Center -- Dockerized Backend**

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)](https://fastapi.tiangolo.com)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg)](https://www.docker.com/)
[![License: GPL v3](https://img.shields.io/badge/license-GPLv3-blue.svg)](LICENSE)

NullFeed is a self-hosted YouTube media center that wraps **yt-dlp** with a polished, multi-user experience. The backend runs as a single Docker container and provides automated channel subscriptions, invisible background caching, instant reverse-proxied playback plus range-request media streaming, sponsor-segment detection, AI-powered recommendations via Claude, and real-time WebSocket updates -- consumed by the NullFeed apps (Flutter on iOS, native SwiftUI on tvOS).

---

## Features

- **Instant Playback** -- A not-yet-cached video plays immediately: the backend resolves and reverse-proxies a progressive source (`/instant-stream`), or serves a prewarmed 360p preview, while the full-quality version caches in the background for a seamless client-side upgrade -- no buffering, no interruption.
- **Invisible Caching, Not a Collection** -- Following a channel quietly caches its new uploads in the background (configurable polling interval), and cold-pressed videos cache too. Cached content is reference-counted and bounded automatically -- LRU eviction for incidental cold-press cache, per-subscription retention for followed channels -- so there's no user-managed "download" collection to babysit.
- **Download/Cache Manager** -- Celery-based task queue with configurable concurrency, retry logic, and exponential backoff.
- **Media Streaming** -- Built-in static file server with HTTP range request support for native seeking and scrubbing.
- **Built-in Web App** -- The published Docker image bakes in the Flutter web client and serves it at `/`, so a single URL gives you both the app and the API (`/docs`) -- no separate web host needed.
- **Multi-User Support** -- Independent profiles with per-user subscriptions, watch history, and playback positions.
- **Smart Deduplication** -- One copy of each video on disk, reference-counted across all subscribing users.
- **AI Recommendations** -- Claude-powered channel and video suggestions derived from each user's subscription graph via the Anthropic API.
- **Sponsor-Skip** -- Detects sponsor/ad segments per video (SponsorBlock, with an AI-from-transcript fallback) and exposes them so clients skip them during playback.
- **Real-Time Updates** -- WebSocket push for cache/download completion, new-episode alerts, sponsor-segment readiness, and recommendation refresh.
- **Resume-Aware Feeds** -- Continue Watching, New Episodes, and Recently Added API endpoints for the home screen experience.
- **Unraid-Native** -- Community Applications template for one-click installation on Unraid servers.
- **Auto-Updating yt-dlp** -- Automatically updates yt-dlp on every container start to stay current with YouTube changes.

---

## Architecture

```
                          +---------------------+
  Flutter App             |   Docker Container  |
  (iOS / tvOS)  <---->    |                     |
                  REST    |  FastAPI  (API)      |
                  + WS    |  Celery   (Workers)  |
                          |  Redis    (Broker)   |
                          |  SQLite   (Database) |
                          |  yt-dlp   (Downloads)|
                          |  ffmpeg   (Encoding) |
                          +---------------------+
```

| Component      | Technology         | Purpose                                      |
|----------------|--------------------|----------------------------------------------|
| API Server     | FastAPI + Uvicorn  | Async REST API with auto-generated OpenAPI docs |
| Database       | SQLite + SQLAlchemy 2.x | Zero-config, file-based persistence      |
| Migrations     | Alembic            | Schema versioning and upgrades               |
| Task Queue     | Celery + Redis     | Background download scheduling and retries   |
| Download Engine| yt-dlp             | YouTube content acquisition                  |
| Media Encoding | ffmpeg             | Transcoding and format support               |
| AI Engine      | Anthropic API (Claude) | Personalized recommendations             |
| Real-Time      | WebSocket (FastAPI)| Push notifications to connected clients      |

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) (20.10+)
- [Docker Compose](https://docs.docker.com/compose/install/) (v2+)

---

## Quick Start

1. **Clone the repository:**

   ```bash
   git clone https://github.com/windoze95/nullfeed-backend.git
   cd nullfeed-backend
   ```

2. **Configure environment variables:**

   ```bash
   cp .env.example .env
   # Edit .env with your preferred settings
   ```

3. **Start the container:**

   ```bash
   docker compose up -d
   ```

4. **Verify it's running:**

   ```bash
   curl http://localhost:8484/api/health
   ```

5. **Open the app / API docs:**

   The published image serves the **web app** at [http://localhost:8484/](http://localhost:8484/), and the interactive API docs (Swagger UI) at [http://localhost:8484/docs](http://localhost:8484/docs).

---

## Environment Variables

| Variable                 | Default   | Description                                              |
|--------------------------|-----------|----------------------------------------------------------|
| `TUBEVAULT_PORT`         | `8484`    | API listen port                                          |
| `ANTHROPIC_API_KEY`      | _(none)_  | Anthropic API key for AI recommendations (Discover tab)  |
| `GEMINI_API_KEY`         | _(none)_  | Google Gemini API key — enables the embeddings-based Discover pipeline and/or Gemini ranking |
| `OPENAI_API_KEY`         | _(none)_  | OpenAI API key — enables the embeddings-based Discover pipeline and/or OpenAI ranking |
| `NULLFEED_EMBED_PROVIDER`| _(auto)_  | Discover embedding provider (`gemini` / `openai`); blank auto-picks from available keys. Switching re-embeds from scratch |
| `NULLFEED_RANK_PROVIDER` | _(auto)_  | Discover ranking provider (`anthropic` / `gemini` / `openai` / `chatgpt`); blank auto-picks from available keys (a ChatGPT sign-in counts, picked last) |
| `NULLFEED_EMBED_MODEL`   | _(auto)_  | Override the embedding model (requires `NULLFEED_EMBED_PROVIDER`) |
| `NULLFEED_RANK_MODEL`    | _(auto)_  | Override the ranking model (requires `NULLFEED_RANK_PROVIDER`) |
| `DOWNLOAD_CONCURRENCY`   | `2`       | Max simultaneous yt-dlp downloads                        |
| `MEDIA_QUALITY`          | `1080p`   | Default download quality (`720p` / `1080p` / `4k` / `best`) |
| `YOUTUBE_COOKIES_FILE`   | _(auto)_  | Path to a `cookies.txt` so yt-dlp can play **age-restricted / members-only** videos. Defaults to `<config>/cookies.txt` if present. See [Age-restricted videos](#age-restricted-videos). |
| `CHECK_INTERVAL_MINUTES` | `60`      | How often to poll subscribed channels for new uploads    |
| `PUID`                   | `1000`    | User ID for file permissions (Unraid standard)           |
| `PGID`                   | `1000`    | Group ID for file permissions (Unraid standard)          |
| `REDIS_URL`              | `redis://localhost:6379/0` | Redis broker URL (internal; override for external Redis) |
| `DATABASE_URL`           | `sqlite:////data/db/nullfeed.db` | Database connection string             |

---

## Volume Mounts

| Container Path     | Purpose                          | Example Host Path                       |
|--------------------|----------------------------------|-----------------------------------------|
| `/data/media`      | Downloaded video/audio files     | `/mnt/user/appdata/nullfeed/media`      |
| `/data/db`         | SQLite database + migrations     | `/mnt/user/appdata/nullfeed/db`         |
| `/data/config`     | App configuration, API keys      | `/mnt/user/appdata/nullfeed/config`     |
| `/data/thumbnails` | Cached channel art and thumbnails| `/mnt/user/appdata/nullfeed/thumbs`     |

---

## Use your ChatGPT subscription for Discover ranking

Instead of an OpenAI API key, the Discover reranker can run on a ChatGPT
Plus/Pro subscription via the same "sign in with ChatGPT" flow Codex CLI
uses. Embeddings still need a Gemini or OpenAI key (the ChatGPT surface has
no embeddings) — `GEMINI_API_KEY` from Google AI Studio's free tier pairs
well.

1. In ChatGPT: Settings -> Security -> enable **device authorization**
   (off by default).
2. As the NullFeed admin profile: `POST /api/settings/chatgpt-login` — the
   response contains a `verification_url` and a `user_code`.
3. Open the URL, sign in, enter the code.
4. `POST /api/settings/chatgpt-login/poll` until it returns
   `{"status": "connected"}`. Done — the `chatgpt` rank provider is now
   available (auto-picked when no API keys are set, or pin it with
   `NULLFEED_RANK_PROVIDER=chatgpt`).

**Caveats:** this is an unofficial surface OpenAI operates for Codex
clients — it may change or be gated at any time, and Discover shares your
plan's Codex usage limits (heavy refreshing eats the same quota as Codex
code reviews). Any failure just disables the provider; nothing else breaks.

## Age-restricted videos

YouTube blocks extraction of **age-restricted and members-only** videos for
anonymous requests ("Sign in to confirm your age"). Since the backend uses
`yt-dlp` for streaming, previews, and downloads, those videos won't play unless
yt-dlp is authenticated with your YouTube cookies.

To enable them:

1. Export a `cookies.txt` from a browser **logged in to YouTube** (e.g. the
   "Get cookies.txt LOCALLY" extension, in Netscape format). Use a throwaway
   account if you'd rather not use your main one.
2. Drop it at **`/data/config/cookies.txt`** (auto-detected), or set
   `YOUTUBE_COOKIES_FILE` to a custom path.
3. Restart the container.

Notes: cookies expire and YouTube rotates them, so you may need to refresh the
file occasionally; yt-dlp updates the file in place, so it must be writable.

---

## API Overview

Full interactive documentation is available at `/docs` (Swagger UI) and `/redoc` (ReDoc) when the server is running.

### Authentication
| Method | Endpoint              | Description                         |
|--------|-----------------------|-------------------------------------|
| POST   | `/api/auth/profiles`  | List all user profiles              |
| POST   | `/api/auth/select`    | Select a profile (with optional PIN)|
| POST   | `/api/auth/create`    | Create a new user profile (admin)   |

### Channels & Subscriptions
| Method | Endpoint                           | Description                              |
|--------|------------------------------------|------------------------------------------|
| GET    | `/api/channels`                    | List all known channels                  |
| POST   | `/api/channels/subscribe`          | Subscribe current user to a channel      |
| POST   | `/api/channels/subscribe-bulk`     | Subscribe to multiple channels at once   |
| DELETE | `/api/channels/{id}/unsubscribe`   | Unsubscribe current user                 |
| GET    | `/api/channels/{id}`               | Channel detail with video list           |
| GET    | `/api/channels/{id}/videos`        | Paginated video list for a channel       |
| POST   | `/api/channels/poll`               | Trigger an immediate poll of all your channels |
| POST   | `/api/channels/{id}/poll`          | Trigger an immediate poll of one channel |

### Videos & Playback
| Method | Endpoint                          | Description                                |
|--------|-----------------------------------|--------------------------------------------|
| GET    | `/api/videos`                     | Search the user's episodes by title/channel |
| GET    | `/api/videos/{id}`                | Video metadata                             |
| POST   | `/api/videos/{id}/playback-ticket`| Mint a short-lived stream ticket           |
| GET    | `/api/videos/{id}/instant-stream` | Reverse-proxied progressive source for immediate playback |
| GET    | `/api/videos/{id}/preview-stream` | Serve a prewarmed 360p preview (if ready)  |
| GET    | `/api/videos/{id}/stream`         | Stream the cached HQ file (supports range requests) |
| POST   | `/api/videos/{id}/cache`          | Record a cache claim + start a background HQ fetch |
| POST   | `/api/videos/prewarm`             | Batch pre-generate 360p previews for upcoming tiles |
| GET    | `/api/videos/{id}/ad-segments`    | Detected sponsor/ad segments for client-side skipping |
| PUT    | `/api/videos/{id}/progress`       | Update watch position                      |
| DELETE | `/api/videos/{id}`                | Remove user's reference (ref-count check)  |

### Watch-Later Queue
| Method | Endpoint                  | Description                      |
|--------|---------------------------|----------------------------------|
| GET    | `/api/queue`              | Get the user's watch-later queue |
| POST   | `/api/videos/{id}/queue`  | Add a video to watch-later       |
| DELETE | `/api/videos/{id}/queue`  | Remove a video from watch-later  |

### Home Feed
| Method | Endpoint                        | Description                               |
|--------|---------------------------------|-------------------------------------------|
| GET    | `/api/feed/home`                | All three sections below, in one call     |
| GET    | `/api/feed/continue-watching`   | Videos with partial progress, by channel  |
| GET    | `/api/feed/new-episodes`        | Newest unwatched episode per followed channel (cache-agnostic) |
| GET    | `/api/feed/recently-added`      | Recent uploads across followed channels (cache-agnostic) |

### Recommendations
| Method | Endpoint                       | Description                        |
|--------|--------------------------------|------------------------------------|
| GET    | `/api/discover`                | AI-generated suggestions           |
| POST   | `/api/discover/{id}/dismiss`   | Dismiss a suggestion               |
| POST   | `/api/discover/refresh`        | Force-refresh recommendations      |

### WebSocket
| Endpoint                     | Description                                    |
|------------------------------|------------------------------------------------|
| `ws://{host}:{port}/ws/{user_id}` | Real-time events: cache/download completion, new episodes, sponsor-segment readiness, recommendation refresh, watch-progress sync |

### Health
| Method | Endpoint       | Description            |
|--------|----------------|------------------------|
| GET    | `/api/health`  | Container health check |

---

## Unraid Installation

NullFeed includes a Community Applications template for one-click installation on Unraid.

1. In the Unraid web UI, navigate to **Apps** (Community Applications).
2. Search for **NullFeed** or add the template repository manually.
3. Configure the template:
   - Set the **API Port** (default: `8484`).
   - Map the four volume paths (`Media`, `Database`, `Config`, `Thumbnails`).
   - Optionally provide an `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, or `OPENAI_API_KEY` for AI recommendations (a Gemini or OpenAI key unlocks the richer embeddings-based Discover pipeline).
4. Click **Apply** to start the container.
5. Access the API docs at `http://[SERVER_IP]:8484/docs`.

The container includes a health check endpoint at `/api/health` for Unraid's built-in monitoring, and logs are written to stdout/stderr for the Unraid log viewer.

---

## Development Setup

To run the backend locally without Docker:

1. **Prerequisites:**
   - Python 3.12+
   - Redis server
   - ffmpeg

2. **Install dependencies:**

   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure environment:**

   ```bash
   cp .env.example .env
   # Edit .env -- set DATABASE_URL to a local SQLite path
   ```

4. **Run database migrations:**

   ```bash
   alembic upgrade head
   ```

5. **Start Redis:**

   ```bash
   redis-server --daemonize yes
   ```

6. **Start the Celery worker:**

   ```bash
   celery -A app.tasks.celery_app worker --loglevel=info --concurrency=2
   ```

7. **Start the Celery Beat scheduler:**

   ```bash
   celery -A app.tasks.celery_app beat --loglevel=info
   ```

8. **Start the API server:**

   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8484 --reload
   ```

---

## Related Repositories

| Repository | Description |
|------------|-------------|
| **nullfeed-backend** (this repo) | **Python/FastAPI backend -- Docker-based server with yt-dlp, Celery, Redis, and SQLite** |
| [nullfeed-flutter](https://github.com/windoze95/nullfeed-flutter) | Flutter client for iOS |
| [nullfeed-tvos](https://github.com/windoze95/nullfeed-tvos) | Native Swift/SwiftUI tvOS app |
| [nullfeed-demo](https://github.com/windoze95/nullfeed-demo) | FastAPI demo server with Creative Commons content for App Store review |

---

## Marketing Site

The public site lives in [`site/`](site/) — plain static HTML/CSS, no build step. Merges to `main` that touch `site/` deploy it to Cloudflare Pages via `.github/workflows/site.yml` when the `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` repo secrets are set; manual deploy is `npx wrangler pages deploy site --project-name=nullfeed`.

---

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).
