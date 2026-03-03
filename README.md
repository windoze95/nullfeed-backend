# NullFeed Backend

**A Self-Hosted YouTube Media Center -- Dockerized Backend**

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)](https://fastapi.tiangolo.com)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg)](https://www.docker.com/)
[![License: Private](https://img.shields.io/badge/license-private-red.svg)](#license)

NullFeed is a self-hosted YouTube media center that wraps **yt-dlp** with a polished, multi-user experience. The backend runs as a single Docker container and provides automated channel subscriptions, download management, media streaming with HTTP range requests, AI-powered recommendations via Claude, and real-time WebSocket updates -- all consumed by the NullFeed Flutter app on iOS and tvOS.

---

## Features

- **Automated Channel Subscriptions** -- Subscribe to YouTube channels and automatically download new uploads on a configurable polling interval.
- **Download Manager** -- Celery-based task queue with configurable concurrency, retry logic, and exponential backoff.
- **Media Streaming** -- Built-in static file server with HTTP range request support for native seeking and scrubbing.
- **Multi-User Support** -- Independent profiles with per-user subscriptions, watch history, and playback positions.
- **Smart Deduplication** -- One copy of each video on disk, reference-counted across all subscribing users.
- **AI Recommendations** -- Claude-powered channel and video suggestions derived from each user's subscription graph via the Anthropic API.
- **Real-Time Updates** -- WebSocket push for download progress, completion events, and new episode alerts.
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
   git clone https://github.com/nullfeed/nullfeed-backend.git
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

5. **Explore the API docs:**

   Open [http://localhost:8484/docs](http://localhost:8484/docs) in your browser for the interactive Swagger UI.

---

## Environment Variables

| Variable                 | Default   | Description                                              |
|--------------------------|-----------|----------------------------------------------------------|
| `TUBEVAULT_PORT`         | `8484`    | API listen port                                          |
| `ANTHROPIC_API_KEY`      | _(none)_  | Anthropic API key for AI recommendations (Discover tab)  |
| `DOWNLOAD_CONCURRENCY`   | `2`       | Max simultaneous yt-dlp downloads                        |
| `MEDIA_QUALITY`          | `1080p`   | Default download quality (`720p` / `1080p` / `4k` / `best`) |
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
| DELETE | `/api/channels/{id}/unsubscribe`   | Unsubscribe current user                 |
| GET    | `/api/channels/{id}`               | Channel detail with video list           |
| GET    | `/api/channels/{id}/videos`        | Paginated video list for a channel       |

### Videos & Playback
| Method | Endpoint                    | Description                                |
|--------|-----------------------------|--------------------------------------------|
| GET    | `/api/videos/{id}`          | Video metadata                             |
| GET    | `/api/videos/{id}/stream`   | Stream video file (supports range requests)|
| PUT    | `/api/videos/{id}/progress` | Update watch position                      |
| DELETE | `/api/videos/{id}`          | Remove user's reference (ref-count check)  |

### Home Feed
| Method | Endpoint                        | Description                               |
|--------|---------------------------------|-------------------------------------------|
| GET    | `/api/feed/continue-watching`   | Videos with partial progress, by channel  |
| GET    | `/api/feed/new-episodes`        | Channels with unwatched downloads         |
| GET    | `/api/feed/recently-added`      | Chronological list of new downloads       |

### Recommendations
| Method | Endpoint                       | Description                        |
|--------|--------------------------------|------------------------------------|
| GET    | `/api/discover`                | AI-generated suggestions           |
| POST   | `/api/discover/{id}/dismiss`   | Dismiss a suggestion               |
| POST   | `/api/discover/refresh`        | Force-refresh recommendations      |

### WebSocket
| Endpoint                     | Description                                    |
|------------------------------|------------------------------------------------|
| `ws://{host}:{port}/ws/{user_id}` | Real-time events: download progress, completions, new episodes |

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
   - Optionally provide your `ANTHROPIC_API_KEY` for AI recommendations.
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

## Project Structure

```
nullfeed-backend/
├── app/
│   ├── __init__.py
│   ├── main.py                  # FastAPI application entry point
│   ├── config.py                # Settings and environment variable loading
│   ├── database.py              # SQLAlchemy engine and session management
│   ├── api/
│   │   ├── auth.py              # Authentication and profile endpoints
│   │   ├── channels.py          # Channel and subscription endpoints
│   │   ├── discover.py          # AI recommendation endpoints
│   │   ├── feed.py              # Home feed endpoints
│   │   ├── health.py            # Health check endpoint
│   │   ├── videos.py            # Video metadata and streaming endpoints
│   │   └── websocket.py         # WebSocket connection handler
│   ├── models/
│   │   ├── channel.py           # Channel ORM model
│   │   ├── recommendation.py    # AI recommendation ORM model
│   │   ├── subscription.py      # User subscription ORM model
│   │   ├── user.py              # User profile ORM model
│   │   ├── user_video_ref.py    # Deduplication pivot table
│   │   └── video.py             # Video ORM model
│   ├── schemas/
│   │   ├── channel.py           # Channel Pydantic schemas
│   │   ├── feed.py              # Feed response schemas
│   │   ├── user.py              # User Pydantic schemas
│   │   └── video.py             # Video Pydantic schemas
│   ├── services/
│   │   ├── channel_poller.py    # Scheduled channel polling logic
│   │   ├── download_manager.py  # yt-dlp download orchestration
│   │   ├── media_server.py      # Media file serving with range requests
│   │   ├── recommendation.py    # Anthropic API integration
│   │   └── storage.py           # Disk and file management utilities
│   └── tasks/
│       ├── celery_app.py        # Celery application configuration
│       └── download_tasks.py    # Async download task definitions
├── alembic/
│   ├── env.py                   # Alembic environment config
│   ├── script.py.mako           # Migration template
│   └── versions/
│       └── 001_initial.py       # Initial database migration
├── scripts/
│   └── entrypoint.sh            # Docker entrypoint script
├── .env.example                 # Example environment configuration
├── .github/workflows/ci.yml    # GitHub Actions CI pipeline
├── alembic.ini                  # Alembic configuration
├── docker-compose.yml           # Docker Compose configuration
├── Dockerfile                   # Multi-stage Docker build
├── requirements.txt             # Pinned Python dependencies
└── unraid-template.xml          # Unraid Community Applications template
```

---

## License

Private -- All rights reserved.
