"""Provider-selectable discovery pipeline: providers, pipeline, endpoints."""

import json

import pytest
from sqlalchemy import select

import app.services.discovery as discovery
import app.services.llm_providers as llm_providers
from app.config import settings
from app.database import async_session_factory
from app.models.channel_embedding import ChannelEmbedding
from app.models.recommendation import Recommendation
from tests.helpers import (
    fake_completed_process,
    seed_channel,
    seed_ref,
    seed_subscription,
    seed_video,
)

pytestmark = pytest.mark.asyncio

GEMINI_MODEL_KEY = "gemini:gemini-embedding-2"

SEARCH_JSON = {
    "entries": [
        {
            "channel_id": "UCnew1",
            "channel": "Keyboard Channel",
            "uploader_id": "@keebs",
            "title": "Best mechanical keyboards 2026",
        },
        {
            "channel_id": "UCnew1",
            "channel": "Keyboard Channel",
            "uploader_id": "@keebs",
            "title": "Switch lube deep dive",
        },
        {
            "channel_id": "UCortho",
            "channel": "Ortho Builds",
            "uploader_id": None,
            "title": "Building a split keyboard",
        },
    ]
}


def _set_keys(monkeypatch, *, anthropic="", gemini="", openai=""):
    """Pin every provider key so ambient env vars can't leak into a test."""
    monkeypatch.setattr(settings, "anthropic_api_key", anthropic)
    monkeypatch.setattr(settings, "gemini_api_key", gemini)
    monkeypatch.setattr(settings, "openai_api_key", openai)
    monkeypatch.setattr(settings, "discovery_embed_provider", "")
    monkeypatch.setattr(settings, "discovery_rank_provider", "")
    monkeypatch.setattr(settings, "discovery_embed_model", "")
    monkeypatch.setattr(settings, "discovery_rank_model", "")


def _install_search(monkeypatch, payload=None, calls=None):
    def fake_run(cmd, **_kwargs):
        if calls is not None:
            calls.append(cmd)
        return fake_completed_process(payload or SEARCH_JSON)

    monkeypatch.setattr(discovery.subprocess, "run", fake_run)


def _install_embeddings(monkeypatch, counts=None):
    async def fake_embed(texts, model):
        if counts is not None:
            counts.append(len(texts))
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(llm_providers, "_embed_gemini", fake_embed)
    monkeypatch.setattr(llm_providers, "_embed_openai", fake_embed)


def _install_ranker(
    monkeypatch,
    *,
    queries=None,
    picks=None,
    fail_queries=False,
    fail_rerank=False,
    prompts=None,
):
    queries = queries if queries is not None else ["mechanical keyboards review"]
    picks = (
        picks
        if picks is not None
        else [
            {
                "channel_id": "UCnew1",
                "channel_name": "Keyboard Channel",
                "reason": "Because you watch tech reviews",
            }
        ]
    )

    async def fake_complete(prompt, model):
        if prompts is not None:
            prompts.append(prompt)
        if "search queries" in prompt:
            if fail_queries:
                raise RuntimeError("query generation down")
            return json.dumps(queries)
        if fail_rerank:
            return "I could not decide, sorry!"
        return json.dumps(picks)

    monkeypatch.setattr(llm_providers, "_complete_gemini", fake_complete)
    monkeypatch.setattr(llm_providers, "_complete_openai", fake_complete)
    monkeypatch.setattr(llm_providers, "_complete_anthropic", fake_complete)


async def _seed_profile(user_id: str):
    """A subscribed channel with a couple of watched videos."""
    async with async_session_factory() as db:
        channel = await seed_channel(
            db, name="Tech Reviews Weekly", youtube_channel_id="UCsub1"
        )
        await seed_subscription(db, user_id, channel.id)
        for title in (
            "Mechanical keyboard roundup",
            "Mechanical keyboard switches explained",
        ):
            video = await seed_video(db, channel, title=title)
            await seed_ref(db, user_id, video.id, is_watched=True)
        return channel


# --- provider resolution ---------------------------------------------------


async def test_resolve_providers_auto_and_explicit(monkeypatch):
    _set_keys(monkeypatch)
    assert llm_providers.resolve_embed_provider() is None
    assert llm_providers.resolve_rank_provider() is None

    _set_keys(monkeypatch, openai="sk-x")
    assert llm_providers.resolve_embed_provider() == (
        "openai",
        "text-embedding-3-small",
    )
    assert llm_providers.resolve_rank_provider() == ("openai", "gpt-5.6-luna")

    _set_keys(monkeypatch, openai="sk-x", gemini="g-x")
    assert llm_providers.resolve_embed_provider() == ("gemini", "gemini-embedding-2")
    assert llm_providers.resolve_rank_provider() == ("gemini", "gemini-3.5-flash")

    _set_keys(monkeypatch, openai="sk-x", gemini="g-x", anthropic="a-x")
    assert llm_providers.resolve_rank_provider() == ("anthropic", "claude-haiku-4-5")

    # Explicit provider + model override win over auto-detect order.
    monkeypatch.setattr(settings, "discovery_rank_provider", "openai")
    monkeypatch.setattr(settings, "discovery_rank_model", "custom-model")
    assert llm_providers.resolve_rank_provider() == ("openai", "custom-model")

    # Explicit provider without its key, or an unknown provider, disables.
    monkeypatch.setattr(settings, "discovery_rank_model", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    assert llm_providers.resolve_rank_provider() is None
    monkeypatch.setattr(settings, "discovery_embed_provider", "cohere")
    assert llm_providers.resolve_embed_provider() is None

    # A model override WITHOUT an explicit provider is ignored — auto-detect
    # could pair it with the wrong vendor's API.
    _set_keys(monkeypatch, openai="sk-x")
    monkeypatch.setattr(settings, "discovery_rank_model", "custom-model")
    assert llm_providers.resolve_rank_provider() == ("openai", "gpt-5.6-luna")


# --- fallback to the legacy engine ------------------------------------------


async def test_falls_back_to_legacy_without_embed_provider(
    monkeypatch, client, make_user
):
    _set_keys(monkeypatch, anthropic="a-x")  # rank yes, embed no

    called = {}

    async def fake_legacy(user, db):
        called["user_id"] = user.id
        return []

    monkeypatch.setattr(discovery, "legacy_generate_recommendations", fake_legacy)

    user, headers = await make_user()
    resp = await client.post("/api/discover/refresh", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == []
    assert called["user_id"] == user["id"]


# --- full pipeline -----------------------------------------------------------


async def test_pipeline_end_to_end(monkeypatch, client, make_user):
    _set_keys(monkeypatch, gemini="g-x")
    _install_search(monkeypatch)
    _install_embeddings(monkeypatch)
    # The reranker returns an invented channel and a duplicate of a valid
    # pick: both must be dropped.
    _install_ranker(
        monkeypatch,
        picks=[
            {
                "channel_id": "UCnew1",
                "channel_name": "Keyboard Channel",
                "reason": "Because you watch tech reviews",
            },
            {
                "channel_id": "UCnew1",
                "channel_name": "Keyboard Channel",
                "reason": "duplicate pick",
            },
            {
                "channel_id": "UChallucinated",
                "channel_name": "Made Up",
                "reason": "nope",
            },
        ],
    )

    user, headers = await make_user()
    await _seed_profile(user["id"])

    resp = await client.post("/api/discover/refresh", headers=headers)
    assert resp.status_code == 200, resp.text
    recs = resp.json()
    assert len(recs) == 1
    assert recs[0]["channel_name"] == "Keyboard Channel"
    # The @handle from the search result is stored for one-tap subscribe.
    assert recs[0]["youtube_channel_id"] == "@keebs"
    assert recs[0]["reason"] == "Because you watch tech reviews"
    assert recs[0]["dismissed"] is False

    # Both the subscription profile and the candidates were cached per-model.
    async with async_session_factory() as db:
        rows = (await db.execute(select(ChannelEmbedding))).scalars().all()
    by_id = {r.youtube_channel_id: r for r in rows}
    assert set(by_id) == {"UCsub1", "UCnew1", "UCortho"}
    assert all(r.model == GEMINI_MODEL_KEY for r in rows)
    assert by_id["UCnew1"].handle == "@keebs"
    assert by_id["UCsub1"].dim == 2


async def test_get_lazily_generates_when_empty(monkeypatch, client, make_user):
    _set_keys(monkeypatch, gemini="g-x")
    _install_search(monkeypatch)
    _install_embeddings(monkeypatch)
    _install_ranker(monkeypatch)

    user, headers = await make_user()
    await _seed_profile(user["id"])

    resp = await client.get("/api/discover", headers=headers)
    assert resp.status_code == 200, resp.text
    assert [r["channel_name"] for r in resp.json()] == ["Keyboard Channel"]


async def test_candidates_exclude_subscribed_and_dismissed(
    monkeypatch, client, make_user
):
    _set_keys(monkeypatch, gemini="g-x")
    _install_embeddings(monkeypatch)
    prompts: list[str] = []
    _install_ranker(monkeypatch, prompts=prompts)
    # Search returns only the already-subscribed channel and a dismissed one.
    _install_search(
        monkeypatch,
        payload={
            "entries": [
                {
                    "channel_id": "UCsub1",
                    "channel": "Tech Reviews Weekly",
                    "uploader_id": "@techweekly",
                    "title": "Another review",
                },
                {
                    "channel_id": "UCdismissed",
                    "channel": "Boring Channel",
                    "uploader_id": "@boring",
                    "title": "Meh",
                },
            ]
        },
    )

    user, headers = await make_user()
    await _seed_profile(user["id"])
    async with async_session_factory() as db:
        db.add(
            Recommendation(
                user_id=user["id"],
                channel_name="Boring Channel",
                youtube_channel_id="@boring",
                dismissed=True,
            )
        )
        await db.commit()

    resp = await client.post("/api/discover/refresh", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == []
    # Query generation ran, but with no surviving candidates the reranker
    # must never have been consulted.
    assert len(prompts) == 1
    assert "search queries" in prompts[0]


async def test_rerank_failure_keeps_existing_recommendations(
    monkeypatch, client, make_user
):
    _set_keys(monkeypatch, gemini="g-x")
    _install_search(monkeypatch)
    _install_embeddings(monkeypatch)
    _install_ranker(monkeypatch, fail_rerank=True)

    user, headers = await make_user()
    await _seed_profile(user["id"])
    async with async_session_factory() as db:
        db.add(
            Recommendation(
                user_id=user["id"],
                channel_name="Existing Pick",
                youtube_channel_id="@existing",
            )
        )
        await db.commit()

    resp = await client.post("/api/discover/refresh", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == []

    async with async_session_factory() as db:
        remaining = (
            (await db.execute(select(Recommendation.channel_name))).scalars().all()
        )
    assert remaining == ["Existing Pick"]


async def test_embedding_cache_reused_and_per_model(monkeypatch, client, make_user):
    _set_keys(monkeypatch, gemini="g-x")
    _install_search(monkeypatch)
    counts: list[int] = []
    _install_embeddings(monkeypatch, counts=counts)
    _install_ranker(monkeypatch)

    user, headers = await make_user()
    await _seed_profile(user["id"])

    resp = await client.post("/api/discover/refresh", headers=headers)
    assert resp.status_code == 200, resp.text
    first_run_calls = len(counts)
    assert first_run_calls >= 1

    # Second run: identical profile and candidates -> everything cache-hits.
    resp = await client.post("/api/discover/refresh", headers=headers)
    assert resp.status_code == 200, resp.text
    assert len(counts) == first_run_calls

    # Switching the embedding provider writes fresh rows under the new model
    # key; the old rows stay behind (ignored, harmless).
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "sk-x")
    resp = await client.post("/api/discover/refresh", headers=headers)
    assert resp.status_code == 200, resp.text
    assert len(counts) > first_run_calls

    async with async_session_factory() as db:
        models = (
            (await db.execute(select(ChannelEmbedding.model).distinct()))
            .scalars()
            .all()
        )
    assert set(models) == {GEMINI_MODEL_KEY, "openai:text-embedding-3-small"}


async def test_query_fallback_when_llm_query_generation_fails(
    monkeypatch, client, make_user
):
    _set_keys(monkeypatch, gemini="g-x")
    calls: list[list[str]] = []
    _install_search(monkeypatch, calls=calls)
    _install_embeddings(monkeypatch)
    _install_ranker(monkeypatch, fail_queries=True)

    user, headers = await make_user()
    await _seed_profile(user["id"])

    resp = await client.post("/api/discover/refresh", headers=headers)
    assert resp.status_code == 200, resp.text
    assert [r["channel_name"] for r in resp.json()] == ["Keyboard Channel"]

    # Deterministic keyword fallback drove the search: "mechanical" and
    # "keyboard" dominate the seeded titles.
    assert calls, "expected at least one yt-dlp search"
    search_terms = " ".join(cmd[-1] for cmd in calls)
    assert "ytsearch" in search_terms
    assert "mechanical" in search_terms
    assert "keyboard" in search_terms


async def test_no_subscriptions_returns_empty_without_search(
    monkeypatch, client, make_user
):
    _set_keys(monkeypatch, gemini="g-x")
    _install_embeddings(monkeypatch)
    _install_ranker(monkeypatch)
    monkeypatch.setattr(discovery.subprocess, "run", _fail_run)

    _, headers = await make_user()
    resp = await client.post("/api/discover/refresh", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def _fail_run(*_args, **_kwargs):
    raise AssertionError("yt-dlp must not be invoked without subscriptions")


async def test_reembeds_when_candidate_text_changes(monkeypatch, client, make_user):
    _set_keys(monkeypatch, gemini="g-x")
    counts: list[int] = []
    _install_embeddings(monkeypatch, counts=counts)
    _install_ranker(monkeypatch)
    _install_search(monkeypatch)

    user, headers = await make_user()
    await _seed_profile(user["id"])

    resp = await client.post("/api/discover/refresh", headers=headers)
    assert resp.status_code == 200, resp.text
    baseline = list(counts)

    # Same payload -> full cache hit, no embed calls.
    resp = await client.post("/api/discover/refresh", headers=headers)
    assert resp.status_code == 200, resp.text
    assert counts == baseline

    # UCnew1's search titles change -> its profile text (and hash) changes ->
    # exactly that one candidate is re-embedded under the same model key.
    changed = {
        "entries": [
            dict(SEARCH_JSON["entries"][0], title="A totally different upload"),
            SEARCH_JSON["entries"][2],
        ]
    }
    _install_search(monkeypatch, payload=changed)
    resp = await client.post("/api/discover/refresh", headers=headers)
    assert resp.status_code == 200, resp.text
    assert counts == baseline + [1]


async def test_rerank_candidate_and_final_caps(monkeypatch, client, make_user):
    _set_keys(monkeypatch, gemini="g-x")
    _install_embeddings(monkeypatch)
    bulk_entries = [
        {
            "channel_id": f"UCbulk{i:02d}",
            "channel": f"Bulk Channel {i}",
            "uploader_id": f"@bulk{i}",
            "title": f"Bulk video {i}",
        }
        for i in range(50)
    ]
    _install_search(monkeypatch, payload={"entries": bulk_entries})
    prompts: list[str] = []
    _install_ranker(
        monkeypatch,
        prompts=prompts,
        picks=[
            {
                "channel_id": f"UCbulk{i:02d}",
                "channel_name": f"Bulk Channel {i}",
                "reason": "r",
            }
            for i in range(50)
        ],
    )

    user, headers = await make_user()
    await _seed_profile(user["id"])

    resp = await client.post("/api/discover/refresh", headers=headers)
    assert resp.status_code == 200, resp.text
    # Only FINAL_RECOMMENDATIONS survive even when the model returns 50 picks.
    assert len(resp.json()) == discovery.FINAL_RECOMMENDATIONS

    # The reranker was shown at most RERANK_CANDIDATES candidates.
    rerank_prompt = prompts[-1]
    assert rerank_prompt.count("- [UCbulk") == discovery.RERANK_CANDIDATES


async def test_malformed_search_entries_are_tolerated(monkeypatch, client, make_user):
    _set_keys(monkeypatch, gemini="g-x")
    _install_embeddings(monkeypatch)
    _install_ranker(monkeypatch)
    # yt-dlp really does emit null entries for failed extractions; the rest
    # model field-type garbage.
    _install_search(
        monkeypatch,
        payload={
            "entries": [
                None,
                "garbage",
                {"channel_id": 42, "channel": "Numeric Id"},
                {"channel_id": "UCnum", "channel": 42},
                {"channel": "No Id At All"},
                SEARCH_JSON["entries"][0],
            ]
        },
    )

    user, headers = await make_user()
    await _seed_profile(user["id"])

    resp = await client.post("/api/discover/refresh", headers=headers)
    assert resp.status_code == 200, resp.text
    assert [r["channel_name"] for r in resp.json()] == ["Keyboard Channel"]


async def test_llm_queries_are_sanitized_for_subprocess(monkeypatch, client, make_user):
    _set_keys(monkeypatch, gemini="g-x")
    _install_embeddings(monkeypatch)
    calls: list[list[str]] = []
    _install_search(monkeypatch, calls=calls)
    _install_ranker(monkeypatch, queries=["bad\x00query\nsecond line"])

    user, headers = await make_user()
    await _seed_profile(user["id"])

    resp = await client.post("/api/discover/refresh", headers=headers)
    assert resp.status_code == 200, resp.text
    assert calls
    for cmd in calls:
        target = cmd[-1]
        assert target.startswith("ytsearch")
        assert "\x00" not in target
        assert "\n" not in target


# --- small unit pieces -------------------------------------------------------


async def test_cosine_and_json_helpers():
    assert discovery._cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert discovery._cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert discovery._cosine([0.0, 0.0], [1.0, 0.0]) == 0.0

    fenced = '```json\n["a", "b"]\n```'
    assert discovery._parse_json_array(fenced) == ["a", "b"]
    # Models routinely append prose despite "JSON only" instructions.
    with_prose = "```json\n[1, 2]\n```\nThose picks cover all interests!"
    assert discovery._parse_json_array(with_prose) == [1, 2]
    leading_prose = 'Here you go: [{"a": 1}] — hope this helps'
    assert discovery._parse_json_array(leading_prose) == [{"a": 1}]
    upper_fence = '```JSON\n["x"]\n```'
    assert discovery._parse_json_array(upper_fence) == ["x"]
    with pytest.raises(Exception):
        discovery._parse_json_array('{"not": "a list"}')
    with pytest.raises(Exception):
        discovery._parse_json_array("no json here at all")


async def test_fallback_queries_are_deterministic():
    profile = [
        {
            "text": "Tech Reviews Weekly\nRecent videos: Mechanical keyboard roundup; Mechanical keyboard switches explained"
        },
        {
            "text": "Homelab Heaven\nRecent videos: Proxmox cluster build; Proxmox backup strategies"
        },
    ]
    queries = discovery._fallback_queries(profile)
    assert queries
    joined = " ".join(queries)
    assert "mechanical" in joined
    assert "proxmox" in joined
