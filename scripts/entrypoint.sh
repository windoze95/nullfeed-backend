#!/usr/bin/env bash
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}
PORT=${TUBEVAULT_PORT:-8484}

echo "NullFeed Backend starting..."
echo "  PUID=${PUID}  PGID=${PGID}  PORT=${PORT}"

# ── Create nullfeed user/group if running as root ──────────────────────────
if [ "$(id -u)" = "0" ]; then
    groupadd -o -g "${PGID}" nullfeed 2>/dev/null || true
    useradd -o -u "${PUID}" -g nullfeed -d /app -s /bin/bash nullfeed 2>/dev/null || true
    chown -R "${PUID}:${PGID}" /data
fi

# ── Auto-update yt-dlp ─────────────────────────────────────────────────────
echo "Updating yt-dlp..."
pip install --quiet --upgrade yt-dlp 2>/dev/null || echo "yt-dlp update check failed (non-fatal)"

# ── Run Alembic migrations ─────────────────────────────────────────────────
echo "Running database migrations..."
cd /app
python -m alembic upgrade head

# ── Start Redis in the background ──────────────────────────────────────────
echo "Starting Redis..."
redis-server --daemonize yes --loglevel warning

# ── Start Celery worker in the background ──────────────────────────────────
echo "Starting Celery worker..."
celery -A app.tasks.celery_app worker \
    --loglevel=info \
    --concurrency="${DOWNLOAD_CONCURRENCY:-2}" \
    --detach \
    --logfile=/dev/stdout

# ── Start Celery Beat scheduler in the background ─────────────────────────
echo "Starting Celery Beat scheduler..."
celery -A app.tasks.celery_app beat \
    --loglevel=info \
    --detach \
    --logfile=/dev/stdout

# ── Start FastAPI ──────────────────────────────────────────────────────────
echo "Starting FastAPI on port ${PORT}..."
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --log-level info
