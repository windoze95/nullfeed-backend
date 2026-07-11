from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base
from app.utils.time import utcnow_naive


class ChannelEmbedding(Base):
    """Cached embedding vector for a YouTube channel's text profile.

    Rows are keyed by (youtube_channel_id, model) where model is the
    provider-qualified embedding model, e.g. "gemini:gemini-embedding-2".
    Vectors from different models live in incompatible spaces, so switching
    the embedding provider simply computes fresh rows under the new model
    key — stale rows under the old key are ignored and harmless.
    """

    __tablename__ = "channel_embeddings"

    youtube_channel_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    model: Mapped[str] = mapped_column(String(128), primary_key=True)
    # sha256 of the embedded text; a changed profile re-embeds on next use.
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # The text that was embedded (name/description + recent titles), kept so
    # the reranker can describe candidates without refetching them.
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    handle: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vector: Mapped[list] = mapped_column(JSON, nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
