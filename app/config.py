from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Server
    tubevault_port: int = 8484

    # Anthropic
    anthropic_api_key: str = ""

    # Downloads
    catalog_fetch_count: int = 50
    download_concurrency: int = 2
    media_quality: str = "1080p"
    check_interval_minutes: int = 60
    metadata_refresh_interval_hours: int = 12

    # Auth sessions
    # A session is rejected once it is older than the absolute TTL (since
    # creation) or has been idle longer than the idle TTL (since last activity).
    # Defaults are generous so active users are never logged out unexpectedly;
    # tune them down for stricter security.
    session_absolute_ttl_days: int = 30
    session_idle_ttl_days: int = 14

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
