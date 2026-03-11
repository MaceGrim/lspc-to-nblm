"""Update RSS feed with all episodes in docs/episodes/ that aren't already in the feed."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import load_config
from src.scraper import scrape_events
from src.podcast import generate_episode_slug
from src.rss import update_rss_feed, feed_contains_guid

config = load_config(Path("config.yaml"))
all_events = scrape_events(config.luma, limit=20)
episodes_dir = Path("docs/episodes")

for ev in all_events:
    if not ev.paper_urls:
        continue
    slug = generate_episode_slug(ev)
    mp3_path = episodes_dir / f"{slug}.mp3"

    if not mp3_path.exists():
        print(f"  SKIP (no MP3): {slug} - {ev.title}")
        continue

    if feed_contains_guid(slug):
        print(f"  SKIP (in feed): {slug} - {ev.title}")
        continue

    # Check if this was a paper-only episode (no video match)
    # For now, just add it to the feed
    print(f"  ADDING: {slug} - {ev.title}")
    update_rss_feed(
        mp3_path=mp3_path,
        event=ev,
        slug=slug,
        paper_paths=[],
        rss_config=config.rss,
    )
    print(f"    Added to feed.xml")

print("\nDone. Check docs/feed.xml")
