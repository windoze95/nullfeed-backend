from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Server
    tubevault_port: int = 8484

    # Anthropic
    anthropic_api_key: str = ""

    # Discovery providers (app/services/llm_providers.py). Provider API keys
    # follow the same unprefixed convention as ANTHROPIC_API_KEY because they
    # are the vendors' own well-known names; the pipeline knobs are ours and
    # take the NULLFEED_ prefix. Leave the provider selectors blank to
    # auto-pick from whichever keys are present (embeddings: gemini, then
    # openai; ranking: anthropic, then gemini, then openai). With no
    # embedding provider at all, Discover falls back to the legacy
    # prompt-only Anthropic engine. The model overrides are escape hatches
    # for when a vendor retires a default model between our releases.
    gemini_api_key: str = ""
    openai_api_key: str = ""
    discovery_embed_provider: str = Field(
        default="", validation_alias="NULLFEED_EMBED_PROVIDER"
    )
    discovery_rank_provider: str = Field(
        default="", validation_alias="NULLFEED_RANK_PROVIDER"
    )
    discovery_embed_model: str = Field(
        default="", validation_alias="NULLFEED_EMBED_MODEL"
    )
    discovery_rank_model: str = Field(
        default="", validation_alias="NULLFEED_RANK_MODEL"
    )

    # Downloads
    # Number of videos pulled from the full yt-dlp listing when a channel is
    # cataloged for the first time (its back catalog). Routine polls use the RSS
    # feed instead, so this only bounds that initial ingest.
    catalog_fetch_count: int = 50
    download_concurrency: int = 2
    media_quality: str = "1080p"
    # Path to a Netflix-style cookies.txt exported from a logged-in YouTube
    # session. Required for age-restricted / members-only videos (yt-dlp can't
    # extract them otherwise — "Sign in to confirm your age"). Empty falls back
    # to <config_path>/cookies.txt if that file exists.
    youtube_cookies_file: str = ""
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

    # Discover recommendation freshness
    # Recommendations are a snapshot from the last generation. A daily sweep
    # deletes each user's live (non-dismissed) recommendations older than this
    # many days so they regenerate from current subscriptions on the next
    # Discover open (regeneration is lazy — inactive users cost nothing). A
    # value <= 0 disables the sweep. Subscription changes invalidate a user's
    # recommendations immediately regardless of this.
    recommendation_stale_days: int = Field(
        default=7, validation_alias="NULLFEED_RECOMMENDATION_STALE_DAYS"
    )

    # Retention enforcement
    # How often the retention sweep runs (Celery beat, in hours). Each run
    # applies every subscription's retention_policy: e.g. KEEP_LAST_N keeps the
    # newest N downloaded videos and soft-removes the user's refs to the rest,
    # letting the orphan cleanup reclaim files no other user still wants.
    # Reclaiming disk is not urgent, so a few hours' latency is plenty.
    retention_interval_hours: int = 6

    # Play cache (downloads-as-cache, #86)
    # A cold press on a not-yet-downloaded video records an evictable CACHE ref
    # (distinct from the user's library) and may back it with an HQ download. The
    # cache reaper keeps the most-recently-watched cache_retention_count videos
    # per user — watch-later-queued videos are pinned and never evicted — and
    # reclaims the rest. Runs every cache_retention_interval_hours. A negative
    # count disables eviction; 0 keeps no cache.
    cache_retention_count: int = 200
    cache_retention_interval_hours: int = 6

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

    # Push notifications (public push gateway, push.julian.codes)
    # NullFeed relays APNs pushes through a shared, multi-tenant gateway so a
    # self-hoster needs no Apple push key of their own. On first use the backend
    # auto-enrolls as a tenant and persists the issued key under config_path
    # (0600 file, shared across workers on the same volume); set push_api_key to
    # pin an explicit operator key instead (it is never persisted). Leave
    # push_gateway_url blank to DISABLE push entirely — every push path
    # (register/unregister endpoints, new-episode send) then becomes a no-op.
    # push_enroll_token is only needed when the gateway runs in "gated"
    # enrollment mode (sent as the X-Enroll-Token header on auto-enroll).
    push_gateway_url: str = Field(
        default="https://push.julian.codes",
        validation_alias="NULLFEED_PUSH_GATEWAY_URL",
    )
    push_api_key: str = Field(default="", validation_alias="NULLFEED_PUSH_API_KEY")
    push_enroll_token: str = Field(
        default="", validation_alias="NULLFEED_PUSH_ENROLL_TOKEN"
    )

    # WebSub / PubSubHubbub subscriber (near-real-time new uploads)
    # When enabled, NullFeed subscribes each tracked UC channel to YouTube's
    # WebSub hub so the hub PUSHES new-upload notifications to our public
    # callback, instead of waiting for the next adaptive RSS poll. It is a pure
    # accelerator layered on top of polling: RSS + adaptive polling stays the
    # always-on fallback, and everything here no-ops when disabled.
    #
    # websub_callback_url is the PUBLIC https URL the hub will call back; it must
    # be reachable from the internet and resolve to GET/POST /api/websub/callback
    # (e.g. https://nullfeed.example.com/api/websub/callback). Leave it BLANK to
    # disable WebSub entirely — the callback router 404s and the subscribe beat
    # task no-ops, leaving polling untouched. websub_hub_url is Google's public
    # hub; websub_lease_seconds is the subscription lease we request and renew
    # before (default 5 days).
    websub_callback_url: str = Field(
        default="", validation_alias="NULLFEED_WEBSUB_CALLBACK_URL"
    )
    websub_hub_url: str = Field(
        default="https://pubsubhubbub.appspot.com/subscribe",
        validation_alias="NULLFEED_WEBSUB_HUB_URL",
    )
    websub_lease_seconds: int = Field(
        default=432000,  # 5 days
        validation_alias="NULLFEED_WEBSUB_LEASE_SECONDS",
    )

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
