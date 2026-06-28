from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Server
    tubevault_port: int = 8484

    # Anthropic
    anthropic_api_key: str = ""

    # Downloads
    # Number of videos pulled from the full yt-dlp listing when a channel is
    # cataloged for the first time (its back catalog). Routine polls use the RSS
    # feed instead, so this only bounds that initial ingest.
    catalog_fetch_count: int = 50
    download_concurrency: int = 2
    media_quality: str = "1080p"
    metadata_refresh_interval_hours: int = 12

    # Polling cadence (adaptive, per channel).
    # The beat wakes every check_interval_minutes and polls only the channels
    # that are DUE (next_poll_at <= now); each wake is cheap (a due-check query
    # plus, for due channels, an RSS conditional GET that usually 304s). After a
    # poll, a channel's interval moves by poll_interval_backoff_factor: it is
    # divided (toward the floor) when new uploads are found and multiplied
    # (toward the cap) when the poll is empty, so busy channels settle near the
    # floor and dormant ones near the cap. Keep check_interval_minutes <= the
    # floor so a channel that comes due is polled promptly.
    check_interval_minutes: int = 5
    poll_interval_floor_minutes: int = 15
    poll_interval_cap_minutes: int = 240
    poll_interval_backoff_factor: float = 2.0

    # Retention enforcement
    # How often the retention sweep runs (Celery beat, in hours). Each run
    # applies every subscription's retention_policy: e.g. KEEP_LAST_N keeps the
    # newest N downloaded videos and soft-removes the user's refs to the rest,
    # letting the orphan cleanup reclaim files no other user still wants.
    # Reclaiming disk is not urgent, so a few hours' latency is plenty.
    retention_interval_hours: int = 6

    # Auth sessions
    # A session is rejected once it is older than the absolute TTL (since
    # creation) or has been idle longer than the idle TTL (since last activity).
    # Defaults are generous so active users are never logged out unexpectedly;
    # tune them down for stricter security.
    session_absolute_ttl_days: int = 30
    session_idle_ttl_days: int = 14

    # Stream/WebSocket access tickets (#30)
    # HMAC secret for signing the short-lived tickets that authorize media
    # streaming and the WebSocket handshake (replacing the session token that
    # used to ride those URLs as ?token=). It MUST be identical across every
    # worker and survive restarts: a ticket minted by one worker is verified by
    # any worker, so a per-process value would cause sporadic auth failures.
    # Leave blank to have the app generate one once and persist it under
    # config_path (stable for a single-host deployment sharing that volume); set
    # it explicitly across multiple hosts, or to rotate all outstanding tickets.
    stream_ticket_secret: str = ""

    # Trust X-Forwarded-For when deriving the client IP for PIN rate limiting.
    # Enable ONLY when running behind a single trusted reverse proxy that sets
    # this header; if clients can reach the app directly they could forge it to
    # evade the throttle. Off by default -> the real socket peer is used.
    trust_proxy_headers: bool = False

    # File permissions
    puid: int = 1000
    pgid: int = 1000

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Database
    database_url: str = "sqlite+aiosqlite:////data/db/nullfeed.db"

    # Paths
    media_path: str = "/data/media"
    db_path: str = "/data/db"
    config_path: str = "/data/config"
    thumbnails_path: str = "/data/thumbnails"

    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def sync_database_url(self) -> str:
        """Return a synchronous database URL for Alembic and Celery."""
        return self.database_url.replace("sqlite+aiosqlite:", "sqlite:")


settings = Settings()
