from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.database import get_db
from app.models.recommendation import Recommendation
from app.models.user import User
from app.schemas.feed import RecommendationOut
from app.services.recommendation import generate_recommendations

router = APIRouter(prefix="/api/discover", tags=["discover"])


@router.get("", response_model=list[RecommendationOut])
async def get_recommendations(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RecommendationOut]:
    result = await db.execute(
        select(Recommendation)
        .where(
            Recommendation.user_id == user.id,
            Recommendation.dismissed == False,  # noqa: E712
        )
        .order_by(Recommendation.created_at.desc())
    )
    recs = result.scalars().all()

    # If no recommendations exist, try generating them.
    if not recs:
        recs = await generate_recommendations(user, db)

    return [RecommendationOut.model_validate(r) for r in recs]


@router.post("/{recommendation_id}/dismiss", response_model=RecommendationOut)
async def dismiss_recommendation(
    recommendation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RecommendationOut:
    result = await db.execute(
        select(Recommendation).where(
            Recommendation.id == recommendation_id,
            Recommendation.user_id == user.id,
        )
    )
    rec = result.scalar_one_or_none()
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    rec.dismissed = True
    await db.commit()
    await db.refresh(rec)
    return RecommendationOut.model_validate(rec)


@router.post("/refresh", response_model=list[RecommendationOut])
async def refresh_recommendations(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RecommendationOut]:
    recs = await generate_recommendations(user, db)
    return [RecommendationOut.model_validate(r) for r in recs]
