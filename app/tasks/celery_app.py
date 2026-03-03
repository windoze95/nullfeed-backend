from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "nullfeed",
    broker=settings.redis_url,
    backend=settings.redis_url,
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
)

# Periodic tasks
celery_app.conf.beat_schedule = {
    "poll-all-channels": {
        "task": "app.tasks.download_tasks.poll_all_channels_task",
        "schedule": settings.check_interval_minutes * 60,
    },
}

# Auto-discover tasks
celery_app.autodiscover_tasks(["app.tasks"])
