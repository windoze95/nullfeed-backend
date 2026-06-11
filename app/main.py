import asyncio
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import os

from app.api import auth, channels, discover, feed, health, videos, websocket, youtube
from app.config import settings
from app.services.progress_broadcaster import start_progress_listener

logger = logging.getLogger(__name__)


async def _run_progress_listener() -> None:
    """Run the Redis progress listener, reconnecting with backoff on failure."""
    delay = 1.0
    while True:
        try:
            await start_progress_listener()
            # Clean return: the listener handles cancellation internally.
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Progress listener error; reconnecting in %.0fs",
                delay,
                exc_info=True,
            )
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60.0)


# Ensure data directories exist before mounting StaticFiles
for _p in [
    settings.media_path,
    settings.db_path,
    settings.config_path,
    settings.thumbnails_path,
]:
    os.makedirs(_p, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Startup
    import os

    for path in [
        settings.media_path,
        settings.db_path,
        settings.config_path,
        settings.thumbnails_path,
    ]:
        os.makedirs(path, exist_ok=True)

    progress_task = asyncio.create_task(_run_progress_listener())

    yield

    # Shutdown
    progress_task.cancel()
    try:
        await progress_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="NullFeed",
    description="Self-Hosted YouTube Media Center API",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS - allow all origins for self-hosted use
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount thumbnail serving
app.mount(
    "/data/thumbnails",
    StaticFiles(directory=settings.thumbnails_path),
    name="thumbnails",
)

# Register API routers
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(channels.router)
app.include_router(videos.router)
app.include_router(feed.router)
app.include_router(discover.router)
app.include_router(websocket.router)
app.include_router(youtube.router)


@app.get("/")
async def root() -> dict:
    return {"service": "NullFeed", "version": "1.0.0", "docs": "/docs"}
