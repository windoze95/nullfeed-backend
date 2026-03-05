import json
import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.channel import Channel
from app.models.recommendation import Recommendation
from app.models.subscription import UserSubscription
from app.models.user import User
from app.models.user_video_ref import UserVideoRef
from app.models.video import Video

logger = logging.getLogger(__name__)

RECOMMENDATION_PROMPT = """You are a YouTube channel recommendation engine. Based on the user's current subscriptions and watch patterns, suggest 5 YouTube channels they might enjoy.

For each suggestion, provide:
1. The channel name
2. A brief reason why the user would enjoy it

Current subscriptions:
{subscriptions}

Watch statistics:
{watch_stats}

Previously dismissed suggestions (do not recommend these again):
{dismissed}

Respond in JSON format:
[
  {{"channel_name": "ChannelName", "reason": "Because you watch..."}}
]

Only return the JSON array, no other text."""


async def generate_recommendations(
    user: User,
    db: AsyncSession,
) -> list[Recommendation]:
    """Generate AI-powered channel recommendations for a user."""
    if not settings.anthropic_api_key:
        logger.info("No Anthropic API key configured; skipping recommendations.")
        return []

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed")
        return []

    # Build user profile
    subscriptions = await _get_subscription_summary(user.id, db)
    watch_stats = await _get_watch_stats(user.id, db)
    dismissed = await _get_dismissed_names(user.id, db)

    if not subscriptions:
        logger.info("User %s has no subscriptions; skipping recommendations.", user.id)
        return []

    prompt = RECOMMENDATION_PROMPT.format(
        subscriptions="\n".join(f"- {s}" for s in subscriptions),
        watch_stats=watch_stats,
        dismissed=", ".join(dismissed) if dismissed else "None",
    )

    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        block = message.content[0]
        response_text = block.text if hasattr(block, "text") else ""
        suggestions = json.loads(response_text)
    except Exception:
        logger.exception("Failed to get recommendations from Anthropic")
        return []

    # Clear old non-dismissed recommendations for this user
    old_result = await db.execute(
        select(Recommendation).where(
            Recommendation.user_id == user.id,
            Recommendation.dismissed == False,  # noqa: E712
        )
    )
    for old_rec in old_result.scalars().all():
        await db.delete(old_rec)

    # Store new recommendations
    new_recs: list[Recommendation] = []
    for s in suggestions:
        rec = Recommendation(
            id=str(uuid.uuid4()),
            user_id=user.id,
            channel_name=s.get("channel_name", "Unknown"),
            reason=s.get("reason", ""),
        )
        db.add(rec)
        new_recs.append(rec)

    await db.commit()
    for rec in new_recs:
        await db.refresh(rec)

    return new_recs


async def _get_subscription_summary(user_id: str, db: AsyncSession) -> list[str]:
    result = await db.execute(
        select(Channel.name)
        .join(UserSubscription, UserSubscription.channel_id == Channel.id)
        .where(UserSubscription.user_id == user_id)
    )
    return [row[0] for row in result.all()]


async def _get_watch_stats(user_id: str, db: AsyncSession) -> str:
    # Get per-channel watch stats
    result = await db.execute(
        select(
            Channel.name,
            func.count(UserVideoRef.video_id).label("total"),
            func.count().filter(UserVideoRef.is_watched == True).label("watched"),  # noqa: E712
        )
        .select_from(UserVideoRef)
        .join(Video, UserVideoRef.video_id == Video.id)
        .join(Channel, Video.channel_id == Channel.id)
        .where(
            UserVideoRef.user_id == user_id,
            UserVideoRef.removed_at.is_(None),
        )
        .group_by(Channel.name)
    )

    lines = []
    try:
        for row in result.all():
            name = row[0]
            total = row[1] or 0
            watched = row[2] or 0
            rate = (watched / total * 100) if total > 0 else 0
            lines.append(
                f"- {name}: {watched}/{total} watched ({rate:.0f}% completion)"
            )
    except Exception:
        lines.append("- Watch statistics unavailable")

    return "\n".join(lines) if lines else "No watch data yet"


async def _get_dismissed_names(user_id: str, db: AsyncSession) -> list[str]:
    result = await db.execute(
        select(Recommendation.channel_name).where(
            Recommendation.user_id == user_id,
            Recommendation.dismissed == True,  # noqa: E712
        )
    )
    return [row[0] for row in result.all()]
