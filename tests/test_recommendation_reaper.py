"""Unit tests for the stale-recommendation reaper (sync, in-memory)."""

from datetime import timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.models.recommendation import Recommendation
from app.services.recommendation_reaper import sweep_stale_recommendations
from app.utils.time import utcnow_naive


def _sync_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_sweep_deletes_old_live_keeps_recent_and_dismissed():
    db = _sync_db()
    now = utcnow_naive()
    old = now - timedelta(days=10)
    db.add_all(
        [
            Recommendation(user_id="u1", channel_name="stale", created_at=old),
            Recommendation(user_id="u1", channel_name="fresh", created_at=now),
            Recommendation(
                user_id="u1",
                channel_name="stale-dismissed",
                dismissed=True,
                created_at=old,
            ),
        ]
    )
    db.commit()

    deleted = sweep_stale_recommendations(db, stale_days=7)
    assert deleted == 1
    remaining = sorted(
        r.channel_name for r in db.execute(select(Recommendation)).scalars()
    )
    # The stale live rec is gone; the fresh one and the dismissed one stay.
    assert remaining == ["fresh", "stale-dismissed"]


def test_sweep_disabled_when_non_positive():
    db = _sync_db()
    db.add(
        Recommendation(
            user_id="u1",
            channel_name="old",
            created_at=utcnow_naive() - timedelta(days=999),
        )
    )
    db.commit()
    assert sweep_stale_recommendations(db, stale_days=0) == 0
    assert sweep_stale_recommendations(db, stale_days=-1) == 0
    assert db.execute(select(Recommendation)).scalars().first() is not None
