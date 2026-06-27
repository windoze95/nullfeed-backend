"""Unit tests for the expired-session reaper."""

from datetime import timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import Base
from app.models.session import Session
from app.services.session_reaper import reap_expired_sessions
from app.utils.time import utcnow_naive


def _make_db():
    # In-memory SQLite without FK pragmas, so sessions need no backing user row.
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_reaper_deletes_absolute_and_idle_expired_keeps_fresh():
    db = _make_db()
    now = utcnow_naive()

    fresh = Session(token_hash="a" * 64, user_id="u1", created_at=now, last_seen_at=now)
    absolute_expired = Session(
        token_hash="b" * 64,
        user_id="u1",
        created_at=now - timedelta(days=settings.session_absolute_ttl_days + 1),
        last_seen_at=now,
    )
    idle_expired = Session(
        token_hash="c" * 64,
        user_id="u1",
        created_at=now,
        last_seen_at=now - timedelta(days=settings.session_idle_ttl_days + 1),
    )
    db.add_all([fresh, absolute_expired, idle_expired])
    db.commit()

    deleted = reap_expired_sessions(db, now=now)
    assert deleted == 2

    remaining = db.execute(select(Session.token_hash)).scalars().all()
    assert remaining == ["a" * 64]
    db.close()


def test_reaper_noop_when_all_fresh():
    db = _make_db()
    now = utcnow_naive()
    db.add(Session(token_hash="d" * 64, user_id="u1", created_at=now, last_seen_at=now))
    db.commit()

    assert reap_expired_sessions(db, now=now) == 0
    assert db.execute(select(Session.token_hash)).scalars().all() == ["d" * 64]
    db.close()
