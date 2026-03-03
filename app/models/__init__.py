from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


# Import all models so Alembic can discover them.
from app.models.user import User  # noqa: E402, F401
from app.models.channel import Channel  # noqa: E402, F401
from app.models.video import Video  # noqa: E402, F401
from app.models.subscription import UserSubscription  # noqa: E402, F401
from app.models.user_video_ref import UserVideoRef  # noqa: E402, F401
from app.models.recommendation import Recommendation  # noqa: E402, F401
