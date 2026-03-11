"""Generate and update the podcast RSS feed (docs/feed.xml)."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

from src.config import RSSConfig

logger = logging.getLogger(__name__)

# Namespace registration (must happen before any XML operations)
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"
ET.register_namespace("itunes", ITUNES_NS)
ET.register_namespace("atom", ATOM_NS)


def create_feed_skeleton(config: RSSConfig) -> tuple[ET.Element, ET.Element]:
    """Create a new RSS feed with channel metadata."""
    root = ET.Element("rss", version="2.0")
    # Namespace declarations are handled by ET.register_namespace above.
    # Do NOT set xmlns:* attributes manually — that causes duplicate attrs.

    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = config.title
    ET.SubElement(channel, "link").text = config.base_url
    ET.SubElement(channel, "description").text = config.description or config.title
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(
        datetime.now(timezone.utc)
    )
    ET.SubElement(channel, f"{{{ITUNES_NS}}}author").text = config.author
    ET.SubElement(channel, f"{{{ITUNES_NS}}}image").set(
        "href", f"{config.base_url}/cover.png"
    )

    # Apple Podcasts required tags
    owner = ET.SubElement(channel, f"{{{ITUNES_NS}}}owner")
    ET.SubElement(owner, f"{{{ITUNES_NS}}}name").text = config.owner_name
    ET.SubElement(owner, f"{{{ITUNES_NS}}}email").text = config.owner_email
    category = ET.SubElement(channel, f"{{{ITUNES_NS}}}category")
    category.set("text", config.category)
    ET.SubElement(category, f"{{{ITUNES_NS}}}category").set(
        "text", config.subcategory
    )
    ET.SubElement(channel, f"{{{ITUNES_NS}}}explicit").text = str(
        config.explicit
    ).lower()

    atom_link = ET.SubElement(channel, f"{{{ATOM_NS}}}link")
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")
    atom_link.set("href", f"{config.base_url}/feed.xml")

    return root, channel


def get_episode_description(event, paper_paths: list[Path]) -> str:
    """Get a simple description for the RSS <description> field.

    For full abstract extraction via arxiv/pymupdf, the pipeline module
    would call this with richer paper_paths. Here we build a basic
    description from the event metadata.
    """
    description_parts: list[str] = []

    # Start with the event title
    description_parts.append(event.title)

    # Append paper links
    for url in event.paper_urls:
        description_parts.append(f"Paper: {url}")

    # Return raw text; ElementTree handles XML escaping during serialization
    return "\n\n".join(description_parts)


def build_episode_item(
    mp3_path: Path,
    event,
    slug: str,
    paper_paths: list[Path],
    config: RSSConfig,
) -> ET.Element:
    """Build an RSS <item> element for a single episode."""
    from mutagen.mp3 import MP3

    item = ET.Element("item")
    date_str = event.date.strftime("%Y-%m-%d")
    ET.SubElement(item, "title").text = f"{event.title} ({date_str})"
    ET.SubElement(item, "description").text = get_episode_description(
        event, paper_paths
    )

    guid = ET.SubElement(item, "guid")
    guid.set("isPermaLink", "false")
    guid.text = slug

    # Use current time as pubDate (not event date) so backfilled episodes
    # appear as new in podcast clients
    ET.SubElement(item, "pubDate").text = format_datetime(
        datetime.now(timezone.utc)
    )

    enclosure = ET.SubElement(item, "enclosure")
    enclosure.set("url", f"{config.base_url}/episodes/{slug}.mp3")
    enclosure.set("type", "audio/mpeg")
    enclosure.set("length", str(mp3_path.stat().st_size))

    # Duration from MP3 metadata
    audio_info = MP3(str(mp3_path)).info
    duration_secs = int(audio_info.length)
    h = duration_secs // 3600
    m = (duration_secs % 3600) // 60
    s = duration_secs % 60
    ET.SubElement(item, f"{{{ITUNES_NS}}}duration").text = (
        f"{h:02d}:{m:02d}:{s:02d}"
    )
    ET.SubElement(item, f"{{{ITUNES_NS}}}summary").text = (
        get_episode_description(event, paper_paths)
    )

    return item


def update_rss_feed(
    mp3_path: Path,
    event,
    slug: str,
    paper_paths: list[Path],
    rss_config: RSSConfig,
    feed_dir: Path | None = None,
) -> None:
    """Generate/update docs/feed.xml with a new episode.

    Parameters
    ----------
    mp3_path : Path
        Path to the episode MP3 file.
    event : PaperClubEvent
        The scraped event (duck-typed: needs .title, .date, .paper_urls).
    slug : str
        Unique episode slug used as GUID.
    paper_paths : list[Path]
        Downloaded paper PDFs (used for description).
    rss_config : RSSConfig
        RSS channel configuration.
    feed_dir : Path | None
        Directory containing feed.xml. Defaults to ``Path("docs")``.
    """
    if feed_dir is None:
        feed_dir = Path("docs")
    feed_path = feed_dir / "feed.xml"

    # Load existing feed or create new
    if feed_path.exists():
        tree = ET.parse(feed_path)
        channel = tree.getroot().find("channel")
    else:
        root, channel = create_feed_skeleton(rss_config)
        tree = ET.ElementTree(root)

    # Dedup: check if episode already in feed
    for item in channel.findall("item"):
        guid_el = item.find("guid")
        if guid_el is not None and guid_el.text == slug:
            logger.info(
                "Episode %s already in feed, skipping RSS update", slug
            )
            return

    # Build episode item
    item = build_episode_item(mp3_path, event, slug, paper_paths, rss_config)

    # Update lastBuildDate on every write
    last_build = channel.find("lastBuildDate")
    if last_build is not None:
        last_build.text = format_datetime(datetime.now(timezone.utc))

    # Prepend (newest first) — insert after last channel-level element
    # that isn't an item
    non_item_count = sum(1 for c in channel if c.tag != "item")
    channel.insert(non_item_count, item)

    # Atomic write: write to temp file then replace
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(tree, space="  ")
    tmp_feed = feed_path.with_suffix(".xml.tmp")
    tree.write(str(tmp_feed), encoding="utf-8", xml_declaration=True)
    tmp_feed.replace(feed_path)


def feed_contains_guid(
    slug: str, feed_dir: Path | None = None
) -> bool:
    """Check if feed.xml already contains an episode with this GUID."""
    if feed_dir is None:
        feed_dir = Path("docs")
    feed_path = feed_dir / "feed.xml"

    if not feed_path.exists():
        return False
    tree = ET.parse(feed_path)
    for item in tree.getroot().iter("item"):
        guid = item.find("guid")
        if guid is not None and guid.text == slug:
            return True
    return False
