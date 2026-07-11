"""Provider-selectable discovery pipeline: retrieval + embeddings + rerank.

Replaces the prompt-only "name some channels" engine for users who configure
an embedding provider. The stages:

1. Profile — embed each subscribed channel's text profile (name/description
   plus recent video titles), cached in channel_embeddings per model key.
2. Queries — one ranking-LLM call turns the profile into a handful of
   diverse YouTube search queries (deterministic keyword fallback).
3. Harvest — parallel yt-dlp flat searches (``ytsearchN:``) collect candidate
   channels; already-subscribed and dismissed channels are dropped.
4. Score — candidates are embedded (cached) and scored by max cosine
   similarity against the subscription vectors, so niche interests surface
   alongside dominant ones.
5. Rerank — a final ranking-LLM call picks the best 10 from the top-scored
   candidates. Picks must reference a harvested candidate id, so the model
   cannot invent channels or hallucinate handles.

Falls back to the legacy Anthropic-only engine when no embedding or ranking
provider is configured, and degrades to [] on any provider failure —
recommendations are never load-bearing.
"""

import asyncio
import hashlib
import json
import logging
import math
import re
import subprocess
import uuid
from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.channel import Channel
from app.models.channel_embedding import ChannelEmbedding
from app.models.recommendation import Recommendation
from app.models.subscription import UserSubscription
from app.models.user import User
from app.models.user_video_ref import UserVideoRef
from app.models.video import Video
from app.services import llm_providers
from app.services.recommendation import (
    generate_recommendations as legacy_generate_recommendations,
)
from app.utils.time import utcnow_naive
from app.utils.ytdlp import cookie_args

logger = logging.getLogger(__name__)

# Most-engaged subscriptions used to build the taste profile.
MAX_PROFILE_CHANNELS = 30
PROFILE_TITLES_PER_CHANNEL = 12
MAX_SEARCH_QUERIES = 5
SEARCH_RESULTS_PER_QUERY = 15
SEARCH_TIMEOUT_SECONDS = 25
# Candidates kept after harvest (by search hit count) for embedding.
MAX_CANDIDATES_TO_EMBED = 120
# Top-scored candidates handed to the reranker.
RERANK_CANDIDATES = 40
FINAL_RECOMMENDATIONS = 10
# Cap stored/embedded profile text; embedding APIs bill per token.
MAX_PROFILE_TEXT_CHARS = 1500

_STOPWORDS = frozenset(
    """a an and are as at be but by for from has have how i in is it its my of on
    or our that the their this to was we what when why with you your vs new best
    top full official video videos episode ep part""".split()
)

QUERY_PROMPT = """\
You help a YouTube discovery engine find new channels for a user.

The user subscribes to these channels (most watched first, with recent
video titles):
{profile}

Write {n} short YouTube search queries (3-6 words each) that together cover
the DIFFERENT interests represented above — not {n} variations of a single
interest. Favor queries likely to surface dedicated channels on a topic
rather than one-off viral videos.

Respond with ONLY a JSON array of strings, no other text."""

RERANK_PROMPT = """\
You are the final ranking stage of a YouTube channel discovery engine.

The user subscribes to these channels (most watched first):
{profile}

Do NOT recommend anything on this list (already subscribed or previously
dismissed by the user):
{excluded}

Candidate channels discovered via search, with similarity to the user's
taste profile (0-1) and sample video titles:
{candidates}

Pick the {n} best channels to recommend, balancing:
- similarity to the user's demonstrated interests
- diversity across their different interests (not every pick for one topic)
- channels with a consistent theme over one-hit topics

Every pick MUST come from the candidate list — never invent a channel. Use
the exact candidate id shown in [brackets].

Respond with ONLY a JSON array, no other text:
[{{"channel_id": "UC...", "channel_name": "...", "reason": "Because you watch..."}}]"""


async def generate_recommendations(
    user: User,
    db: AsyncSession,
) -> list[Recommendation]:
    """Generate channel recommendations with the configured providers.

    Entry point used by the discover API. Requires both an embedding and a
    ranking provider for the retrieval pipeline; otherwise defers to the
    legacy prompt-only engine (which no-ops without an Anthropic key).
    """
    if not (
        llm_providers.resolve_embed_provider() and llm_providers.resolve_rank_provider()
    ):
        return await legacy_generate_recommendations(user, db)

    try:
        picks = await _run_pipeline(user, db)
    except Exception as exc:
        logger.warning("Discovery pipeline failed: %s", exc)
        return []
    if not picks:
        return []
    return await _store_recommendations(user, db, picks)


async def _run_pipeline(user: User, db: AsyncSession) -> list[dict]:
    model_key = llm_providers.embed_model_key()
    if not model_key:  # raced config change; treat as unavailable
        return []

    profile = await _build_profile(user.id, db)
    if not profile:
        logger.info("User %s has no subscriptions; skipping discovery.", user.id)
        return []

    profile_vectors = await _embed_cached(db, profile, model_key)

    dismissed_names, dismissed_ids = await _get_dismissed(user.id, db)
    subscribed_ids = {entry["youtube_channel_id"] for entry in profile}
    subscribed_ids |= await _get_all_subscribed_ids(user.id, db)

    queries = await _generate_queries(profile)
    candidates = await _harvest_candidates(
        queries,
        exclude_ids=subscribed_ids | dismissed_ids,
        exclude_names={n.lower() for n in dismissed_names},
    )
    if not candidates:
        logger.info("Discovery found no new candidate channels for user %s", user.id)
        return []

    candidate_vectors = await _embed_cached(db, candidates, model_key)
    scored = _score_candidates(candidates, candidate_vectors, profile_vectors)
    if not scored:
        return []

    return await _rerank(profile, scored, dismissed_names)


# ---------------------------------------------------------------------------
# Stage 1: taste profile


async def _build_profile(user_id: str, db: AsyncSession) -> list[dict]:
    """Return [{youtube_channel_id, name, handle, text, weight}] for the
    user's most-engaged subscriptions, most engaged first."""
    result = await db.execute(
        select(
            Channel.id, Channel.youtube_channel_id, Channel.name, Channel.description
        )
        .join(UserSubscription, UserSubscription.channel_id == Channel.id)
        .where(UserSubscription.user_id == user_id)
    )
    channels = [
        {
            "id": row[0],
            "youtube_channel_id": row[1],
            "name": row[2],
            "description": row[3] or "",
        }
        for row in result.all()
    ]
    if not channels:
        return []

    stats = await _get_engagement(user_id, db)
    channels.sort(
        key=lambda c: stats.get(c["id"], (0, 0)),
        reverse=True,
    )
    channels = channels[:MAX_PROFILE_CHANNELS]

    titles = await _get_recent_titles(db, [c["id"] for c in channels])
    profile: list[dict] = []
    for c in channels:
        watched, refs = stats.get(c["id"], (0, 0))
        profile.append(
            {
                "youtube_channel_id": c["youtube_channel_id"],
                "name": c["name"],
                "handle": None,
                "text": _profile_text(
                    c["name"], c["description"], titles.get(c["id"], [])
                ),
                "weight": watched,
            }
        )
    return profile


async def _get_engagement(user_id: str, db: AsyncSession) -> dict[str, tuple[int, int]]:
    """channel.id -> (watched_count, ref_count) for sorting by engagement."""
    result = await db.execute(
        select(
            Video.channel_id,
            func.count().filter(UserVideoRef.is_watched == True).label("watched"),  # noqa: E712
            func.count(UserVideoRef.video_id).label("refs"),
        )
        .select_from(UserVideoRef)
        .join(Video, UserVideoRef.video_id == Video.id)
        .where(
            UserVideoRef.user_id == user_id,
            UserVideoRef.removed_at.is_(None),
        )
        .group_by(Video.channel_id)
    )
    return {row[0]: (row[1] or 0, row[2] or 0) for row in result.all()}


async def _get_recent_titles(
    db: AsyncSession, channel_ids: list[str]
) -> dict[str, list[str]]:
    if not channel_ids:
        return {}
    rn = (
        func.row_number()
        .over(partition_by=Video.channel_id, order_by=Video.uploaded_at.desc())
        .label("rn")
    )
    subq = (
        select(Video.channel_id, Video.title, rn)
        .where(Video.channel_id.in_(channel_ids))
        .subquery()
    )
    result = await db.execute(
        select(subq.c.channel_id, subq.c.title).where(
            subq.c.rn <= PROFILE_TITLES_PER_CHANNEL
        )
    )
    titles: dict[str, list[str]] = {}
    for channel_id, title in result.all():
        titles.setdefault(channel_id, []).append(title)
    return titles


def _profile_text(name: str, description: str, titles: list[str]) -> str:
    parts = [name]
    if description:
        parts.append(description[:500])
    if titles:
        parts.append("Recent videos: " + "; ".join(t[:120] for t in titles))
    return "\n".join(parts)[:MAX_PROFILE_TEXT_CHARS]


async def _get_all_subscribed_ids(user_id: str, db: AsyncSession) -> set[str]:
    result = await db.execute(
        select(Channel.youtube_channel_id)
        .join(UserSubscription, UserSubscription.channel_id == Channel.id)
        .where(UserSubscription.user_id == user_id)
    )
    return {row[0] for row in result.all()}


async def _get_dismissed(user_id: str, db: AsyncSession) -> tuple[list[str], set[str]]:
    """Dismissed recommendation names + whatever ids/handles were stored."""
    result = await db.execute(
        select(Recommendation.channel_name, Recommendation.youtube_channel_id).where(
            Recommendation.user_id == user_id,
            Recommendation.dismissed == True,  # noqa: E712
        )
    )
    names: list[str] = []
    ids: set[str] = set()
    for name, stored_id in result.all():
        names.append(name)
        if stored_id:
            ids.add(stored_id)
    return names, ids


# ---------------------------------------------------------------------------
# Stage 2: search queries


async def _generate_queries(profile: list[dict]) -> list[str]:
    prompt = QUERY_PROMPT.format(
        profile=_profile_summary(profile), n=MAX_SEARCH_QUERIES
    )
    try:
        raw = await llm_providers.rank_complete(prompt)
        queries = _parse_json_array(raw)
        queries = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
    except Exception as exc:
        logger.warning("Query generation failed (%s); using keyword fallback", exc)
        queries = []
    if not queries:
        queries = _fallback_queries(profile)
    return queries[:MAX_SEARCH_QUERIES]


def _profile_summary(profile: list[dict]) -> str:
    lines = []
    for entry in profile:
        text = entry["text"].replace("\n", " — ")
        lines.append(f"- {text[:300]}")
    return "\n".join(lines)


def _fallback_queries(profile: list[dict]) -> list[str]:
    """Deterministic fallback: frequent title keywords, chunked into queries."""
    counts: Counter[str] = Counter()
    for entry in profile:
        for word in re.findall(r"[a-zA-Z][a-zA-Z0-9'+-]{2,}", entry["text"].lower()):
            if word not in _STOPWORDS:
                counts[word] += 1
    top = [w for w, c in counts.most_common(12) if c >= 2] or [
        w for w, _ in counts.most_common(9)
    ]
    queries = []
    for start in range(0, len(top), 3):
        chunk = top[start : start + 3]
        if chunk:
            queries.append(" ".join(chunk))
    return queries[:MAX_SEARCH_QUERIES]


# ---------------------------------------------------------------------------
# Stage 3: candidate harvest (yt-dlp flat search)


def _search_channels_sync(query: str) -> list[dict]:
    """Blocking ytsearch via the yt-dlp CLI; returns raw entry dicts.

    Failures return [] — a dead search engine should degrade discovery, not
    crash it. Callers run this via asyncio.to_thread.
    """
    cmd = [
        "yt-dlp",
        *cookie_args(),
        "--no-update",
        "--flat-playlist",
        "-J",
        f"ytsearch{SEARCH_RESULTS_PER_QUERY}:{query}",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=SEARCH_TIMEOUT_SECONDS
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("yt-dlp search failed for %r: %s", query, exc)
        return []
    if result.returncode != 0 or not result.stdout.strip():
        stderr_lines = (result.stderr or "").strip().splitlines()
        logger.warning(
            "yt-dlp search returned no results for %r: %s",
            query,
            stderr_lines[-1][:200] if stderr_lines else "no output",
        )
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    return [e for e in entries or [] if isinstance(e, dict)]


async def _harvest_candidates(
    queries: list[str],
    exclude_ids: set[str],
    exclude_names: set[str],
) -> list[dict]:
    """Run searches concurrently and aggregate per-channel candidates."""
    if not queries:
        return []
    results = await asyncio.gather(
        *(asyncio.to_thread(_search_channels_sync, q) for q in queries)
    )
    candidates: dict[str, dict] = {}
    for entries in results:
        for entry in entries:
            channel_id = entry.get("channel_id")
            name = entry.get("channel") or entry.get("uploader") or ""
            if not channel_id or not name:
                continue
            if channel_id in exclude_ids or name.lower() in exclude_names:
                continue
            handle = entry.get("uploader_id")
            if not (isinstance(handle, str) and handle.startswith("@")):
                handle = None
            if handle and handle in exclude_ids:
                continue
            cand = candidates.setdefault(
                channel_id,
                {
                    "youtube_channel_id": channel_id,
                    "name": name,
                    "handle": handle,
                    "titles": [],
                    "hits": 0,
                },
            )
            cand["hits"] += 1
            if handle and not cand["handle"]:
                cand["handle"] = handle
            title = entry.get("title")
            if isinstance(title, str) and title and len(cand["titles"]) < 6:
                cand["titles"].append(title)

    ranked = sorted(candidates.values(), key=lambda c: c["hits"], reverse=True)
    ranked = ranked[:MAX_CANDIDATES_TO_EMBED]
    for cand in ranked:
        cand["text"] = _profile_text(cand["name"], "", cand["titles"])
    return ranked


# ---------------------------------------------------------------------------
# Stage 4: embed + score


async def _embed_cached(
    db: AsyncSession, entries: list[dict], model_key: str
) -> dict[str, list[float]]:
    """Embed entries' text, reusing channel_embeddings rows when the text is
    unchanged. Returns {youtube_channel_id: vector}."""
    ids = [e["youtube_channel_id"] for e in entries]
    result = await db.execute(
        select(ChannelEmbedding).where(
            ChannelEmbedding.model == model_key,
            ChannelEmbedding.youtube_channel_id.in_(ids),
        )
    )
    cached = {row.youtube_channel_id: row for row in result.scalars().all()}

    vectors: dict[str, list[float]] = {}
    to_embed: list[dict] = []
    for entry in entries:
        text_hash = hashlib.sha256(entry["text"].encode("utf-8")).hexdigest()
        entry["_text_hash"] = text_hash
        row = cached.get(entry["youtube_channel_id"])
        if row is not None and row.text_hash == text_hash:
            vectors[entry["youtube_channel_id"]] = row.vector
        else:
            to_embed.append(entry)

    if to_embed:
        embedded = await llm_providers.embed_texts([e["text"] for e in to_embed])
        for entry, vector in zip(to_embed, embedded):
            channel_id = entry["youtube_channel_id"]
            vectors[channel_id] = vector
            row = cached.get(channel_id)
            if row is None:
                row = ChannelEmbedding(
                    youtube_channel_id=channel_id, model=model_key, vector=[]
                )
                db.add(row)
            row.text_hash = entry["_text_hash"]
            row.content = entry["text"]
            row.name = entry["name"][:255]
            row.handle = entry.get("handle")
            row.vector = vector
            row.dim = len(vector)
            row.updated_at = utcnow_naive()
        await db.commit()

    return vectors


def _cosine(a: list[float], b: list[float]) -> float:
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


def _score_candidates(
    candidates: list[dict],
    candidate_vectors: dict[str, list[float]],
    profile_vectors: dict[str, list[float]],
) -> list[dict]:
    """Max cosine against any subscription vector, so a candidate matching a
    single niche interest scores as well as one matching the dominant one."""
    if not profile_vectors:
        return []
    scored = []
    for cand in candidates:
        vector = candidate_vectors.get(cand["youtube_channel_id"])
        if not vector:
            continue
        similarity = max(
            (_cosine(vector, pv) for pv in profile_vectors.values()), default=0.0
        )
        cand["similarity"] = similarity
        scored.append(cand)
    scored.sort(key=lambda c: (c["similarity"], c["hits"]), reverse=True)
    return scored[:RERANK_CANDIDATES]


# ---------------------------------------------------------------------------
# Stage 5: rerank


async def _rerank(
    profile: list[dict], scored: list[dict], dismissed_names: list[str]
) -> list[dict]:
    by_id = {c["youtube_channel_id"]: c for c in scored}
    candidate_lines = []
    for cand in scored:
        titles = "; ".join(t[:80] for t in cand["titles"][:4])
        candidate_lines.append(
            f"- [{cand['youtube_channel_id']}] {cand['name']} "
            f"(similarity {cand['similarity']:.2f}) — {titles}"
        )
    excluded = [entry["name"] for entry in profile] + dismissed_names
    prompt = RERANK_PROMPT.format(
        profile=_profile_summary(profile),
        excluded="\n".join(f"- {name}" for name in excluded) or "- (none)",
        candidates="\n".join(candidate_lines),
        n=FINAL_RECOMMENDATIONS,
    )
    raw = await llm_providers.rank_complete(prompt)
    items = _parse_json_array(raw)

    picks: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        picked = by_id.get(item.get("channel_id"))
        if picked is None:
            # The model invented a channel or mangled the id; drop it rather
            # than storing something we can't attribute to a real candidate.
            logger.info("Reranker pick not in candidate set: %r", item)
            continue
        reason = item.get("reason")
        picks.append(
            {
                "channel_name": picked["name"],
                # @handle preferred (nicer to display, resolves on subscribe);
                # the subscribe endpoint accepts raw UC ids equally well.
                "youtube_channel_id": picked["handle"] or picked["youtube_channel_id"],
                "reason": reason if isinstance(reason, str) else "",
            }
        )
        if len(picks) >= FINAL_RECOMMENDATIONS:
            break
    return picks


def _parse_json_array(raw: str) -> list:
    """Parse a JSON array out of model text, tolerating markdown fences."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.removeprefix("json").strip()
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("Expected a JSON array")
    return data


# ---------------------------------------------------------------------------
# Storage (mirrors the legacy engine's replace-non-dismissed semantics)


async def _store_recommendations(
    user: User, db: AsyncSession, picks: list[dict]
) -> list[Recommendation]:
    old_result = await db.execute(
        select(Recommendation).where(
            Recommendation.user_id == user.id,
            Recommendation.dismissed == False,  # noqa: E712
        )
    )
    for old_rec in old_result.scalars().all():
        await db.delete(old_rec)

    new_recs: list[Recommendation] = []
    for pick in picks:
        rec = Recommendation(
            id=str(uuid.uuid4()),
            user_id=user.id,
            channel_name=pick["channel_name"],
            youtube_channel_id=pick["youtube_channel_id"],
            reason=pick["reason"],
        )
        db.add(rec)
        new_recs.append(rec)

    await db.commit()
    for rec in new_recs:
        await db.refresh(rec)
    return new_recs
