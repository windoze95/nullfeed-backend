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
