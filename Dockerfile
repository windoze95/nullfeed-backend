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
# Cache-bust the web build with the flutter main commit sha. Otherwise the
# `git clone + flutter build web` layer is cached forever (its command text
# never changes), freezing the served bundle at whatever flutter main was when
# the cache was first filled. CI passes the current sha so this layer rebuilds
# whenever flutter main advances.
ARG FLUTTER_WEB_SHA=dev
WORKDIR /src
RUN echo "flutter web ${FLUTTER_WEB_REF}@${FLUTTER_WEB_SHA}" && \
    git clone --depth 1 --branch ${FLUTTER_WEB_REF} \
        https://github.com/windoze95/nullfeed-flutter.git . && \
    git config --global --add safe.directory /src && \
    flutter pub get && \
    dart run build_runner build --delete-conflicting-outputs && \
    flutter build web --release --no-tree-shake-icons

# ---------------------------------------------------------------------------
# po_token provider (bgutil). YouTube serves cookie-authenticated sessions
# SABR-only formats with no plain progressive URLs; the yt-dlp bgutil plugin
# (requirements.txt) fetches a PO token from this local node server so those
# formats become downloadable — required for age-restricted playback. Bundled
# here (Debian 12, same base as runtime) so the all-in-one image needs no
# separate container; started by the entrypoint on 127.0.0.1:4416.
FROM brainicism/bgutil-ytdlp-pot-provider AS potprovider

# ---------------------------------------------------------------------------
# Backend runtime (no web bundle). This is the fast per-PR CI target.
FROM python:3.12-slim AS runtime

LABEL maintainer="NullFeed" \
      description="NullFeed - Self-Hosted YouTube Media Center Backend"

# Install runtime dependencies: ffmpeg, redis-server, gosu for UID mapping, and
# libatomic1 (a runtime dep of the bundled node binary on some arches).
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        aria2 \
        ffmpeg \
        redis-server \
        gosu \
        libatomic1 \
        curl && \
    rm -rf /var/lib/apt/lists/*

# Bundle the bgutil po_token provider (node runtime + built server). The yt-dlp
# plugin auto-connects to it at 127.0.0.1:4416 once the entrypoint starts it.
COPY --from=potprovider /usr/local/bin/node /usr/local/bin/node
COPY --from=potprovider /app /opt/bgutil-provider

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
