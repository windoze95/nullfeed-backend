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
    "poll-all-channels": {
        "task": "app.tasks.download_tasks.poll_all_channels_task",
        "schedule": settings.check_interval_minutes * 60,
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
}
