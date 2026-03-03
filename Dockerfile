FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---------------------------------------------------------------------------
FROM python:3.12-slim

LABEL maintainer="NullFeed" \
      description="NullFeed - Self-Hosted YouTube Media Center Backend"

# Install runtime dependencies: ffmpeg, redis-server, and gosu for UID mapping
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        redis-server \
        gosu \
        curl && \
    rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder stage
COPY --from=builder /install /usr/local

WORKDIR /app
COPY . .

# Create data directories
RUN mkdir -p /data/media /data/db /data/config /data/thumbnails

# Make entrypoint executable
RUN chmod +x /app/scripts/entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8484

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${TUBEVAULT_PORT:-8484}/api/health || exit 1

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
