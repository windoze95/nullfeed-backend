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

# ── Auto-update yt-dlp + the bgutil po_token plugin ────────────────────────
# YouTube's anti-bot/format enforcement changes constantly; pulling both latest
# on every start keeps the extractor and its PO-token plugin matched and current
# (the bundled provider is rebuilt from :latest each image build).
echo "Updating yt-dlp + po_token plugin..."
pip install --quiet --upgrade yt-dlp bgutil-ytdlp-pot-provider 2>/dev/null \
    || echo "yt-dlp/plugin update check failed (non-fatal)"

# ── Run Alembic migrations ─────────────────────────────────────────────────
echo "Running database migrations..."
cd /app
python -m alembic upgrade head

# ── Start Redis in the background ──────────────────────────────────────────
echo "Starting Redis..."
redis-server --daemonize yes --loglevel warning

# ── Start the po_token provider (bgutil) on 127.0.0.1:4416 ─────────────────
# YouTube serves cookie-authenticated sessions SABR-only formats; the yt-dlp
# bgutil plugin fetches a PO token from this local server so those formats are
# downloadable (required for age-restricted playback). Its absence silently
# hides those formats ("Requested format is not available"), so log its output
# to stdout and confirm it's actually serving.
if [ -f /opt/bgutil-provider/build/main.js ]; then
    echo "Starting po_token provider..."
    ( cd /opt/bgutil-provider && node build/main.js ) &
    for i in $(seq 1 15); do
        if curl -sf -m 2 http://127.0.0.1:4416/ping >/dev/null 2>&1; then
            echo "po_token provider ready on :4416"
            break
        fi
        [ "$i" = 15 ] && echo "WARNING: po_token provider not responding on :4416 — age-restricted downloads will fail"
        sleep 1
    done
fi

# ── Start Celery worker in the background ──────────────────────────────────
# Run with `&` (not --detach) so the worker's stdout is inherited and its task
# output — download/preview progress and failures — shows up in `docker logs`.
# A detached worker daemonizes and its logs never reach the container stream.
echo "Starting Celery worker..."
celery -A app.tasks.celery_app worker \
    --loglevel=info \
    --concurrency="${DOWNLOAD_CONCURRENCY:-2}" &

# ── Start Celery Beat scheduler in the background ─────────────────────────
echo "Starting Celery Beat scheduler..."
celery -A app.tasks.celery_app beat \
    --loglevel=info &

# ── Start FastAPI ──────────────────────────────────────────────────────────
echo "Starting FastAPI on port ${PORT}..."
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --log-level info
