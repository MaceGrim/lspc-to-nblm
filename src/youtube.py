"""YouTube video discovery and audio download for Paper Club episodes.

Uses yt-dlp (via subprocess) to list channel videos, match against a
PaperClubEvent by title and date, and download audio as MP3.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from src.config import YouTubeConfig
from src.errors import VideoNotFoundError, YouTubeDiscoveryError
from src.scraper import PaperClubEvent

logger = logging.getLogger("lspc.youtube")


@dataclass
class VideoMetadata:
    """Metadata for a matched YouTube video."""

    video_id: str
    video_url: str
    title: str
    audio_path: Path


def parse_yt_date(date_str: str | None) -> date | None:
    """Parse yt-dlp date string (YYYYMMDD format) to date object."""
    if not date_str or len(date_str) != 8:
        return None
    try:
        return date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
    except ValueError:
        return None


def title_similarity(event_title: str, video_title: str) -> float:
    """Simple word-overlap similarity score (0.0 to 1.0).

    Compares significant words (3+ chars) between event and video titles.
    Strips punctuation and lowercases for accurate matching.
    """
    stop_words = {"the", "and", "for", "with", "paper", "club"}
    event_words = set(re.findall(r"\b\w{3,}\b", event_title.lower())) - stop_words
    video_words = set(re.findall(r"\b\w{3,}\b", video_title.lower())) - stop_words
    if not event_words or not video_words:
        return 0.0
    return len(event_words & video_words) / len(event_words | video_words)


def find_paper_club_video(
    event: PaperClubEvent, config: YouTubeConfig
) -> Optional[str]:
    """Find a matching Paper Club video on the configured YouTube channel.

    Two-step discovery:
    1. List recent videos from /videos and /streams tabs using yt-dlp.
    2. Match by title containing 'Paper Club' (case-insensitive) and
       upload/release date within *match_window_days* of the event date.

    Returns the video ID of the best match, or None if no match is found.
    Logs a WARNING when no match is found.
    """
    videos = _list_channel_videos(config)

    if not videos:
        raise YouTubeDiscoveryError("yt-dlp found no videos on any tab")

    # Match by title + date (symmetric window) + title similarity
    candidates: list[tuple[dict, float, date]] = []
    event_title_lower = event.title.lower()
    for v in videos:
        v_title = v.get("title", "").lower()
        if "paper club" not in v_title:
            continue
        upload_date = parse_yt_date(
            v.get("upload_date") or v.get("release_date")
        )
        if upload_date:
            delta_days = abs((upload_date - event.date.date()).days)
            if delta_days <= config.match_window_days:
                title_sim = title_similarity(event_title_lower, v_title)
                score = delta_days - (title_sim * 3)
                candidates.append((v, score, upload_date))

    if not candidates:
        logger.warning("No matching video found for event: %s", event.title)
        return None

    # Best match: lowest score, then newest upload date on tie.
    best = min(candidates, key=lambda x: (x[1], -x[2].toordinal()))[0]
    return best["id"]


def download_audio(video_id: str, tmp_dir: Path) -> Path:
    """Download audio-only MP3 for *video_id* into *tmp_dir*.

    Uses yt-dlp with ``-x --audio-format mp3 --audio-quality 128K``.
    Returns the Path of the saved MP3 file.
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(tmp_dir / "%(id)s.%(ext)s")
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    subprocess.run(
        [
            "yt-dlp",
            "-x",
            "--audio-format",
            "mp3",
            "--audio-quality",
            "128K",
            "-o",
            output_template,
            video_url,
        ],
        check=True,
        timeout=600,
    )

    audio_path = tmp_dir / f"{video_id}.mp3"
    if not audio_path.exists():
        # yt-dlp may produce a different extension; try to find it.
        matches = list(tmp_dir.glob(f"{video_id}.*"))
        if matches:
            audio_path = matches[0]
        else:
            raise VideoNotFoundError(
                f"Downloaded audio not found for {video_id}"
            )

    return audio_path


def find_and_download_video(
    event: PaperClubEvent, config: YouTubeConfig
) -> VideoMetadata | None:
    """Convenience wrapper: find a matching video and download its audio.

    Returns ``VideoMetadata`` with the video ID, URL, title, and local
    audio path, or ``None`` if no matching video is found.
    """
    tmp_dir = Path("tmp")
    tmp_dir.mkdir(exist_ok=True)

    videos = _list_channel_videos(config)

    if not videos:
        raise YouTubeDiscoveryError("yt-dlp found no videos on any tab")

    # Match
    candidates: list[tuple[dict, float, date]] = []
    event_title_lower = event.title.lower()
    for v in videos:
        v_title = v.get("title", "").lower()
        if "paper club" not in v_title:
            continue
        upload_date = parse_yt_date(
            v.get("upload_date") or v.get("release_date")
        )
        if upload_date:
            delta_days = abs((upload_date - event.date.date()).days)
            if delta_days <= config.match_window_days:
                title_sim = title_similarity(event_title_lower, v_title)
                score = delta_days - (title_sim * 3)
                candidates.append((v, score, upload_date))

    if not candidates:
        logger.warning("No matching video found for event: %s", event.title)
        return None

    best = min(candidates, key=lambda x: (x[1], -x[2].toordinal()))[0]

    audio_path = download_audio(best["id"], tmp_dir)

    return VideoMetadata(
        video_id=best["id"],
        video_url=best["webpage_url"],
        title=best["title"],
        audio_path=audio_path,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _list_channel_videos(config: YouTubeConfig) -> list[dict]:
    """Fetch recent video metadata from the configured channel.

    Uses ``--flat-playlist`` for fast listing, then fetches full metadata
    only for videos whose title contains "paper club" (case-insensitive).
    Searches both ``/videos`` and ``/streams`` tabs.
    """
    # Phase 1: Fast flat listing to get titles + IDs
    flat_videos: list[dict] = []
    for tab in ["/videos", "/streams"]:
        result = subprocess.run(
            [
                "yt-dlp",
                "--flat-playlist",
                "--dump-json",
                "--no-download",
                "--playlist-end",
                str(config.playlist_depth),
                f"{config.channel_url}{tab}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.warning(
                "yt-dlp flat listing failed for %s (code %d)",
                tab,
                result.returncode,
            )
            continue
        for line in result.stdout.strip().split("\n"):
            if line:
                try:
                    flat_videos.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    # Phase 2: For "paper club" candidates, fetch full metadata to get upload_date
    seen_ids: set[str] = set()
    videos: list[dict] = []
    for v in flat_videos:
        title = v.get("title", "")
        vid_id = v.get("id", "")
        if not vid_id or vid_id in seen_ids:
            continue
        seen_ids.add(vid_id)

        if "paper club" not in title.lower():
            continue

        # Fetch full metadata for this single video
        url = v.get("url") or v.get("webpage_url") or f"https://www.youtube.com/watch?v={vid_id}"
        full_result = subprocess.run(
            [
                "yt-dlp",
                "--dump-json",
                "--no-download",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if full_result.returncode == 0 and full_result.stdout.strip():
            try:
                full_meta = json.loads(full_result.stdout.strip())
                videos.append(full_meta)
                logger.debug("Fetched metadata for: %s", title)
            except json.JSONDecodeError:
                # Fall back to flat metadata
                videos.append(v)
        else:
            # Fall back to flat metadata
            videos.append(v)

    return videos
