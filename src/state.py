"""Read/write processed.json and deduplication logic."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.scraper import (
    PaperClubEvent,
    canonicalize_event_url,
    canonicalize_paper_url,
)


def load_state(path: Path) -> dict:
    """Load processed.json state file. Returns empty dict if missing."""
    path = Path(path)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict, path: Path) -> None:
    """Write processed.json atomically via temp file then rename."""
    path = Path(path)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def is_processed(event: PaperClubEvent, state: dict) -> bool:
    """Check if the event's canonicalized URL exists in state."""
    key = canonicalize_event_url(event.event_url)
    return key in state


def should_reprocess(event: PaperClubEvent, state: dict) -> bool:
    """Return True if live paper URLs differ from stored paper URL set.

    Uses sorted canonicalized paper URLs for comparison.
    If the event is not in state at all, returns False (use is_processed
    to check existence first).
    """
    key = canonicalize_event_url(event.event_url)
    if key not in state:
        return False
    stored = state[key]
    stored_papers = sorted(stored.get("paper_urls", []))
    live_papers = sorted(canonicalize_paper_url(u) for u in event.paper_urls)
    return stored_papers != live_papers


def mark_processed(
    event: PaperClubEvent, slug: str, state: dict
) -> dict:
    """Add event to state with processing metadata.

    Returns the updated state dict.
    """
    key = canonicalize_event_url(event.event_url)
    state[key] = {
        "event_url": event.event_url,
        "title": event.title,
        "date": event.date.isoformat(),
        "paper_urls": sorted(
            canonicalize_paper_url(u) for u in event.paper_urls
        ),
        "episode_slug": slug,
        "episode_file": f"docs/episodes/{slug}.mp3",
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }
    return state
