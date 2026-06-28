import asyncio
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

import os

from app.api import (
    auth,
    channels,
    discover,
    feed,
    health,
    queue,
    videos,
    websocket,
    youtube,
)
from app.config import settings
from app.services.progress_broadcaster import start_progress_listener

# Self-hosters debug from container logs; surface app-level INFO messages
# (graceful degradation in the YouTube importer, poller skips, etc.).
logging.basicConfig(level=logging.INFO)

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
app.include_router(queue.router)
app.include_router(feed.router)
app.include_router(discover.router)
app.include_router(websocket.router)
app.include_router(youtube.router)


# ---------------------------------------------------------------------------
# Normalized error envelope (#3)
#
# Every error response is flattened to {"detail": <human string>, "code":
# <machine code>}. Crucially `detail` stays a STRING: FastAPI's default 422
# body nests a list under `detail`, which breaks clients that read it as a
# string. Existing string `detail` messages are preserved verbatim, so clients
# already reading `detail` keep working and only gain a stable machine `code`.

_ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    502: "bad_gateway",
    503: "service_unavailable",
    504: "gateway_timeout",
}


def _code_for_status(status_code: int) -> str:
    return _ERROR_CODES.get(status_code, f"http_{status_code}")


def _humanize_validation_errors(exc: RequestValidationError) -> str:
    """Flatten pydantic/FastAPI validation errors into one readable string."""
    parts: list[str] = []
    for err in exc.errors():
        # Drop the leading location group ("body"/"query"/"path") for brevity.
        loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")]
        msg = err.get("msg", "Invalid value")
        # Pydantic prefixes custom ValueError messages with "Value error, ".
        msg = msg.removeprefix("Value error, ")
        parts.append(f"{'.'.join(loc)}: {msg}" if loc else msg)
    return "; ".join(parts) or "Invalid request"


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "detail": _humanize_validation_errors(exc),
            "code": "validation_error",
        },
    )


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": detail, "code": _code_for_status(exc.status_code)},
        headers=getattr(exc, "headers", None),
    )


@app.get("/")
async def root() -> dict:
    return {"service": "NullFeed", "version": "1.0.0", "docs": "/docs"}
