"""Tests for channel_poller metadata refresh logic."""

from unittest.mock import MagicMock, patch

from app.models.channel import Channel
from app.services.channel_poller import _refresh_single_channel_metadata


def _make_channel(**overrides):
    ch = Channel(
        id="ch-1",
        youtube_channel_id="@testchannel",
        name="@testchannel",
        slug="testchannel",
    )
    for k, v in overrides.items():
        setattr(ch, k, v)
    return ch


@patch("app.services.channel_poller.fetch_channel_images")
@patch("app.services.channel_poller.fetch_channel_metadata")
def test_refresh_updates_name_and_images(mock_meta, mock_images):
    mock_meta.return_value = {
        "name": "Test Channel",
        "channel_id": "UCabc123",
        "handle": "@testchannel",
    }
    mock_images.return_value = {
        "avatar_url": "https://example.com/avatar.jpg",
        "banner_url": "https://example.com/banner.jpg",
    }
    db = MagicMock()
    # No duplicate channel found
    db.execute.return_value.scalar_one_or_none.return_value = None

    channel = _make_channel()
    _refresh_single_channel_metadata(channel, db)

    assert channel.name == "Test Channel"
    assert channel.youtube_channel_id == "UCabc123"
    assert channel.avatar_url == "https://example.com/avatar.jpg"
    assert channel.banner_url == "https://example.com/banner.jpg"
    assert channel.metadata_refreshed_at is not None
    db.commit.assert_called_once()


@patch("app.services.channel_poller.fetch_channel_images")
@patch("app.services.channel_poller.fetch_channel_metadata")
def test_refresh_skips_canonicalization_on_duplicate(mock_meta, mock_images):
    mock_meta.return_value = {
        "name": "Test Channel",
        "channel_id": "UCabc123",
        "handle": "@testchannel",
    }
    mock_images.return_value = {"avatar_url": None, "banner_url": None}
    db = MagicMock()
    # Simulate a duplicate channel owning this UC ID
    db.execute.return_value.scalar_one_or_none.return_value = _make_channel(
        id="ch-other", youtube_channel_id="UCabc123"
    )

    channel = _make_channel()
    _refresh_single_channel_metadata(channel, db)

    # Should NOT canonicalize since another channel owns the UC ID
    assert channel.youtube_channel_id == "@testchannel"


@patch("app.services.channel_poller.fetch_channel_images")
@patch("app.services.channel_poller.fetch_channel_metadata")
def test_refresh_keeps_existing_name(mock_meta, mock_images):
    mock_meta.return_value = {
        "name": "Resolved Name",
        "channel_id": "UCabc123",
        "handle": "@testchannel",
    }
    mock_images.return_value = {"avatar_url": None, "banner_url": None}
    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = None

    # Channel already has a custom display name
    channel = _make_channel(name="My Custom Name")
    _refresh_single_channel_metadata(channel, db)

    # Name should NOT be overwritten
    assert channel.name == "My Custom Name"
