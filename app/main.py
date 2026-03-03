from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import auth, channels, discover, feed, health, videos, websocket
from app.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Startup
    import os
    for path in [settings.media_path, settings.db_path, settings.config_path, settings.thumbnails_path]:
        os.makedirs(path, exist_ok=True)
    yield
    # Shutdown


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


@app.get("/")
async def root() -> dict:
    return {"service": "NullFeed", "version": "1.0.0", "docs": "/docs"}
