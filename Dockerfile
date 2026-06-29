# Flutter SDK version + repo ref for the web-bundle build stage (see `webbuild`).
ARG FLUTTER_VERSION=3.41.2
ARG FLUTTER_WEB_REF=main

FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---------------------------------------------------------------------------
# Build the Flutter web bundle from the (public) flutter repo. Only the final
# `web` image pulls this in, so the backend-only `runtime` target skips it.
# The bundle's correctness is already gated by the flutter repo's own Build Web
# CI on every PR, so this stage just packages main.
# Pinned to the native build platform: the web bundle is arch-independent, so we
# build it once (not once per target arch) and avoid emulating Flutter on arm64.
FROM --platform=$BUILDPLATFORM ghcr.io/cirruslabs/flutter:${FLUTTER_VERSION} AS webbuild
ARG FLUTTER_WEB_REF
WORKDIR /src
RUN git clone --depth 1 --branch ${FLUTTER_WEB_REF} \
        https://github.com/windoze95/nullfeed-flutter.git . && \
    git config --global --add safe.directory /src && \
    flutter pub get && \
    dart run build_runner build --delete-conflicting-outputs && \
    flutter build web --release --no-tree-shake-icons

# ---------------------------------------------------------------------------
# Backend runtime (no web bundle). This is the fast per-PR CI target.
FROM python:3.12-slim AS runtime

LABEL maintainer="NullFeed" \
      description="NullFeed - Self-Hosted YouTube Media Center Backend"

# Install runtime dependencies: ffmpeg, redis-server, and gosu for UID mapping
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        aria2 \
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

# ---------------------------------------------------------------------------
# Published image: the backend with the Flutter web app baked in and served at
# `/` (see app/main.py). This is the default build target.
FROM runtime AS web
COPY --from=webbuild /src/build/web /app/web
