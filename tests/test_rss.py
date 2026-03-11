"""Tests for src/rss.py — RSS feed generation and update."""

from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import pytest

from src.config import RSSConfig
from src.rss import (
    ATOM_NS,
    ITUNES_NS,
    feed_contains_guid,
    update_rss_feed,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class FakeEvent:
    """Minimal stand-in for PaperClubEvent (duck-typed)."""

    title: str = "Paper Club: Attention Is All You Need"
    date: datetime = field(
        default_factory=lambda: datetime(2025, 6, 15, 18, 0, tzinfo=timezone.utc)
    )
    event_url: str = "https://lu.ma/test-event-1"
    paper_urls: list[str] = field(
        default_factory=lambda: ["https://arxiv.org/abs/1706.03762"]
    )
    supplementary_urls: list[str] = field(default_factory=list)


def make_rss_config(**overrides) -> RSSConfig:
    """Return a minimal valid RSSConfig with optional overrides."""
    defaults = dict(
        title="Test Podcast",
        description="A test podcast feed",
        author="Test Author",
        base_url="https://example.github.io/podcast",
        owner_name="Test Author",
        owner_email="test@example.com",
        category="Technology",
        subcategory="Tech News",
        explicit=False,
    )
    defaults.update(overrides)
    return RSSConfig(**defaults)


def _create_fake_mp3(path: Path, duration_seconds: float = 10.0) -> Path:
    """Create a minimal valid MP3 file that mutagen can read.

    Writes a single MPEG audio frame header followed by enough padding
    to make the file recognizable. Uses a CBR frame at 128 kbps, 44100 Hz,
    stereo (MPEG1 Layer 3).
    """
    # MPEG1 Layer3 128kbps 44100Hz stereo frame header: 0xFFFB9004
    frame_header = b"\xff\xfb\x90\x04"
    # Frame size for 128kbps 44100Hz MPEG1 Layer3 = 417 bytes (with padding bit=0 in header, ~417)
    frame_size = 417
    frame_data = frame_header + b"\x00" * (frame_size - len(frame_header))

    # Calculate how many frames we need for the desired duration
    # Each frame at 44100 Hz MPEG1 Layer3 = 1152 samples = ~26.12ms
    samples_per_frame = 1152
    sample_rate = 44100
    frame_duration = samples_per_frame / sample_rate
    num_frames = max(1, int(duration_seconds / frame_duration))

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        for _ in range(num_frames):
            f.write(frame_data)

    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreateNewFeed:
    """Test creating a feed from scratch (no existing feed.xml)."""

    def test_creates_feed_file(self, tmp_path: Path):
        mp3 = _create_fake_mp3(tmp_path / "episode.mp3")
        event = FakeEvent()
        config = make_rss_config()

        update_rss_feed(mp3, event, "20250615-abc12345", [], config, feed_dir=tmp_path)

        assert (tmp_path / "feed.xml").exists()

    def test_new_feed_has_channel_metadata(self, tmp_path: Path):
        mp3 = _create_fake_mp3(tmp_path / "episode.mp3")
        event = FakeEvent()
        config = make_rss_config()

        update_rss_feed(mp3, event, "20250615-abc12345", [], config, feed_dir=tmp_path)

        tree = ET.parse(tmp_path / "feed.xml")
        channel = tree.getroot().find("channel")
        assert channel.find("title").text == "Test Podcast"
        assert channel.find("description").text == "A test podcast feed"
        assert channel.find(f"{{{ITUNES_NS}}}author").text == "Test Author"

    def test_new_feed_has_itunes_image(self, tmp_path: Path):
        mp3 = _create_fake_mp3(tmp_path / "episode.mp3")
        config = make_rss_config()

        update_rss_feed(mp3, FakeEvent(), "slug-1", [], config, feed_dir=tmp_path)

        tree = ET.parse(tmp_path / "feed.xml")
        channel = tree.getroot().find("channel")
        img = channel.find(f"{{{ITUNES_NS}}}image")
        assert img is not None
        assert "cover.jpg" in img.get("href", "")

    def test_new_feed_has_atom_link(self, tmp_path: Path):
        mp3 = _create_fake_mp3(tmp_path / "episode.mp3")
        config = make_rss_config()

        update_rss_feed(mp3, FakeEvent(), "slug-1", [], config, feed_dir=tmp_path)

        tree = ET.parse(tmp_path / "feed.xml")
        channel = tree.getroot().find("channel")
        atom_link = channel.find(f"{{{ATOM_NS}}}link")
        assert atom_link is not None
        assert atom_link.get("rel") == "self"
        assert atom_link.get("type") == "application/rss+xml"


class TestAddEpisode:
    """Test adding episodes to an existing feed."""

    def test_adds_episode_to_existing_feed(self, tmp_path: Path):
        mp3 = _create_fake_mp3(tmp_path / "episode.mp3")
        config = make_rss_config()

        # First episode
        update_rss_feed(mp3, FakeEvent(), "slug-1", [], config, feed_dir=tmp_path)
        # Second episode
        event2 = FakeEvent(title="Paper Club: GPT-4")
        update_rss_feed(mp3, event2, "slug-2", [], config, feed_dir=tmp_path)

        tree = ET.parse(tmp_path / "feed.xml")
        channel = tree.getroot().find("channel")
        items = channel.findall("item")
        assert len(items) == 2

    def test_dedup_skips_existing_guid(self, tmp_path: Path):
        mp3 = _create_fake_mp3(tmp_path / "episode.mp3")
        config = make_rss_config()

        update_rss_feed(mp3, FakeEvent(), "slug-1", [], config, feed_dir=tmp_path)
        # Same slug again
        update_rss_feed(mp3, FakeEvent(), "slug-1", [], config, feed_dir=tmp_path)

        tree = ET.parse(tmp_path / "feed.xml")
        channel = tree.getroot().find("channel")
        items = channel.findall("item")
        assert len(items) == 1


class TestFeedValidation:
    """Test that generated feeds pass feedparser validation."""

    def test_feedparser_bozo_zero(self, tmp_path: Path):
        mp3 = _create_fake_mp3(tmp_path / "episode.mp3")
        config = make_rss_config()

        update_rss_feed(mp3, FakeEvent(), "slug-1", [], config, feed_dir=tmp_path)

        feed_text = (tmp_path / "feed.xml").read_text(encoding="utf-8")
        parsed = feedparser.parse(feed_text)
        assert parsed.bozo == 0, f"feedparser bozo: {parsed.bozo_exception}"

    def test_feedparser_valid_after_multiple_episodes(self, tmp_path: Path):
        mp3 = _create_fake_mp3(tmp_path / "episode.mp3")
        config = make_rss_config()

        for i in range(3):
            event = FakeEvent(title=f"Paper Club: Paper {i}")
            update_rss_feed(mp3, event, f"slug-{i}", [], config, feed_dir=tmp_path)

        feed_text = (tmp_path / "feed.xml").read_text(encoding="utf-8")
        parsed = feedparser.parse(feed_text)
        assert parsed.bozo == 0, f"feedparser bozo: {parsed.bozo_exception}"
        assert len(parsed.entries) == 3


class TestEpisodeOrder:
    """Test that episodes are ordered newest first."""

    def test_newest_episode_first(self, tmp_path: Path):
        mp3 = _create_fake_mp3(tmp_path / "episode.mp3")
        config = make_rss_config()

        update_rss_feed(
            mp3, FakeEvent(title="First Added"), "slug-1", [], config,
            feed_dir=tmp_path,
        )
        update_rss_feed(
            mp3, FakeEvent(title="Second Added"), "slug-2", [], config,
            feed_dir=tmp_path,
        )
        update_rss_feed(
            mp3, FakeEvent(title="Third Added"), "slug-3", [], config,
            feed_dir=tmp_path,
        )

        tree = ET.parse(tmp_path / "feed.xml")
        channel = tree.getroot().find("channel")
        items = channel.findall("item")
        titles = [item.find("title").text for item in items]

        # The last added should appear first in the feed
        assert "Third Added" in titles[0]
        assert "Second Added" in titles[1]
        assert "First Added" in titles[2]


class TestFeedContainsGuid:
    """Test feed_contains_guid function."""

    def test_returns_false_no_feed(self, tmp_path: Path):
        assert feed_contains_guid("nonexistent", feed_dir=tmp_path) is False

    def test_returns_true_for_existing_guid(self, tmp_path: Path):
        mp3 = _create_fake_mp3(tmp_path / "episode.mp3")
        config = make_rss_config()

        update_rss_feed(mp3, FakeEvent(), "my-slug", [], config, feed_dir=tmp_path)

        assert feed_contains_guid("my-slug", feed_dir=tmp_path) is True

    def test_returns_false_for_missing_guid(self, tmp_path: Path):
        mp3 = _create_fake_mp3(tmp_path / "episode.mp3")
        config = make_rss_config()

        update_rss_feed(mp3, FakeEvent(), "my-slug", [], config, feed_dir=tmp_path)

        assert feed_contains_guid("other-slug", feed_dir=tmp_path) is False


class TestRequiredElements:
    """Test that all required RSS/iTunes elements are present on each item."""

    def test_item_has_required_elements(self, tmp_path: Path):
        mp3 = _create_fake_mp3(tmp_path / "episode.mp3", duration_seconds=65.0)
        config = make_rss_config()

        update_rss_feed(mp3, FakeEvent(), "slug-1", [], config, feed_dir=tmp_path)

        tree = ET.parse(tmp_path / "feed.xml")
        channel = tree.getroot().find("channel")
        item = channel.find("item")

        # Required per spec
        assert item.find("title") is not None
        assert item.find("title").text is not None
        assert item.find("description") is not None
        assert item.find("description").text is not None
        assert item.find("pubDate") is not None
        assert item.find("pubDate").text is not None
        assert item.find("guid") is not None
        assert item.find("guid").text == "slug-1"
        assert item.find("guid").get("isPermaLink") == "false"

        enclosure = item.find("enclosure")
        assert enclosure is not None
        assert enclosure.get("url").endswith(".mp3")
        assert enclosure.get("type") == "audio/mpeg"
        assert int(enclosure.get("length")) > 0

        duration = item.find(f"{{{ITUNES_NS}}}duration")
        assert duration is not None
        assert ":" in duration.text  # HH:MM:SS format

    def test_enclosure_length_matches_file_size(self, tmp_path: Path):
        mp3 = _create_fake_mp3(tmp_path / "episode.mp3")
        config = make_rss_config()

        update_rss_feed(mp3, FakeEvent(), "slug-1", [], config, feed_dir=tmp_path)

        tree = ET.parse(tmp_path / "feed.xml")
        item = tree.getroot().find("channel").find("item")
        enclosure = item.find("enclosure")
        assert int(enclosure.get("length")) == mp3.stat().st_size


class TestChannelMetadata:
    """Test channel-level metadata."""

    def test_channel_has_required_elements(self, tmp_path: Path):
        mp3 = _create_fake_mp3(tmp_path / "episode.mp3")
        config = make_rss_config(
            title="My Podcast",
            description="My Description",
            author="Author Name",
        )

        update_rss_feed(mp3, FakeEvent(), "slug-1", [], config, feed_dir=tmp_path)

        tree = ET.parse(tmp_path / "feed.xml")
        channel = tree.getroot().find("channel")

        assert channel.find("title").text == "My Podcast"
        assert channel.find("description").text == "My Description"
        assert channel.find(f"{{{ITUNES_NS}}}author").text == "Author Name"

        img = channel.find(f"{{{ITUNES_NS}}}image")
        assert img is not None
        assert img.get("href") is not None

        atom_link = channel.find(f"{{{ATOM_NS}}}link")
        assert atom_link is not None

    def test_itunes_owner_present(self, tmp_path: Path):
        mp3 = _create_fake_mp3(tmp_path / "episode.mp3")
        config = make_rss_config(owner_name="Owner", owner_email="o@e.com")

        update_rss_feed(mp3, FakeEvent(), "slug-1", [], config, feed_dir=tmp_path)

        tree = ET.parse(tmp_path / "feed.xml")
        channel = tree.getroot().find("channel")
        owner = channel.find(f"{{{ITUNES_NS}}}owner")
        assert owner is not None
        assert owner.find(f"{{{ITUNES_NS}}}name").text == "Owner"
        assert owner.find(f"{{{ITUNES_NS}}}email").text == "o@e.com"

    def test_itunes_category_present(self, tmp_path: Path):
        mp3 = _create_fake_mp3(tmp_path / "episode.mp3")
        config = make_rss_config(category="Science", subcategory="Physics")

        update_rss_feed(mp3, FakeEvent(), "slug-1", [], config, feed_dir=tmp_path)

        tree = ET.parse(tmp_path / "feed.xml")
        channel = tree.getroot().find("channel")
        cat = channel.find(f"{{{ITUNES_NS}}}category")
        assert cat is not None
        assert cat.get("text") == "Science"

    def test_rss_version_and_namespaces(self, tmp_path: Path):
        mp3 = _create_fake_mp3(tmp_path / "episode.mp3")
        config = make_rss_config()

        update_rss_feed(mp3, FakeEvent(), "slug-1", [], config, feed_dir=tmp_path)

        tree = ET.parse(tmp_path / "feed.xml")
        root = tree.getroot()
        assert root.tag == "rss"
        assert root.get("version") == "2.0"
        # Namespaces should be present (check via raw XML)
        raw = (tmp_path / "feed.xml").read_text(encoding="utf-8")
        assert "xmlns:itunes" in raw
        assert "xmlns:atom" in raw
