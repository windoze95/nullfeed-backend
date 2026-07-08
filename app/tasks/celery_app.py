from celery import Celery

from app.config import settings

celery_app = Celery(
    "nullfeed",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.download_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    worker_concurrency=settings.download_concurrency,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Long downloads (>1h) must not be redelivered to another worker while
    # still running; with acks_late the Redis broker redelivers after the
    # visibility timeout. 12 hours gives ample headroom.
    broker_transport_options={"visibility_timeout": 43200},
)

# Periodic tasks
celery_app.conf.beat_schedule = {
    # Wake frequently and poll only channels whose adaptive schedule is due.
    # Each wake is cheap: a due-check query plus, for due channels, an RSS
    # conditional GET that usually 304s.
    "poll-due-channels": {
        "task": "app.tasks.download_tasks.poll_all_channels_task",
        "schedule": settings.check_interval_minutes * 60,
        "kwargs": {"due_only": True},
    },
    "refresh-stale-channel-metadata": {
        "task": "app.tasks.download_tasks.refresh_stale_channel_metadata_task",
        "schedule": settings.metadata_refresh_interval_hours * 3600,
    },
    # Self-heal downloads stranded by a crashed worker. Frequent and cheap (a
    # single indexed query); recovery latency is this interval plus the reaper's
    # staleness threshold.
    "reap-stuck-downloads": {
        "task": "app.tasks.download_tasks.reap_stuck_downloads_task",
        "schedule": 300,  # every 5 minutes
    },
    # Sweep expired auth sessions so the sessions table cannot grow without
    # bound. Cheap (a single indexed DELETE); hourly is plenty.
    "reap-expired-sessions": {
        "task": "app.tasks.download_tasks.reap_expired_sessions_task",
        "schedule": 3600,  # hourly
    },
    # Apply each subscription's retention_policy (e.g. KEEP_LAST_N): soft-remove
    # the user's refs to downloaded videos beyond the policy so the orphan
    # cleanup can reclaim files nobody else still wants. Cheap (indexed queries
    # over the few subscriptions that set a policy); a few hours' latency is fine.
    "enforce-retention": {
        "task": "app.tasks.download_tasks.enforce_retention_task",
        "schedule": settings.retention_interval_hours * 3600,
    },
    # Evict the play cache: keep each user's most-recently-watched cache videos
    # (queued ones pinned) and reclaim the rest. Same cadence/cost profile as the
    # retention sweep — indexed per-user queries, a few hours' latency is fine.
    "enforce-video-cache": {
        "task": "app.tasks.download_tasks.enforce_video_cache_task",
        "schedule": settings.cache_retention_interval_hours * 3600,
    },
    # Back-catalog catch-up for the content-type feature: discover each channel's
    # Shorts/livestreams and classify content_type-NULL rows, a few channels at a
    # time. Winds down to a no-op once caught up (an indexed "any NULL rows?"
    # check), so a modest cadence is fine and it stays quiet thereafter.
    "reconcile-content-types": {
        "task": "app.tasks.download_tasks.reconcile_content_task",
        "schedule": 900,  # every 15 minutes
    },
}

# WebSub (PubSubHubbub) subscription upkeep — only scheduled when a public
# callback URL is configured, so the feature is entirely absent when disabled.
# Renews each tracked channel's hub lease before it lapses; every 6h gives
# several renewal attempts inside the default 5-day lease's renewal window, and
# is cheap (one indexed query that hits the network only for channels actually
# due). Discovery keeps working via RSS polling regardless.
if settings.websub_callback_url:
    celery_app.conf.beat_schedule["sync-websub-subscriptions"] = {
        "task": "app.tasks.download_tasks.sync_websub_subscriptions_task",
        "schedule": 6 * 3600,
    }
