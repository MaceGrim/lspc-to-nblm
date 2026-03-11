"""Pipeline orchestrator: scrape -> download -> generate -> publish.

Provides the main CLI entry point and full pipeline execution with
retry logic, file locking, structured logging, and secret redaction.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout

from src.config import PipelineConfig, load_config
from src.errors import (
    LSPCError,
    PodcastGenerationError,
    PublishError,
    VideoNotFoundError,
)
from src.papers import download_all_papers
from src.podcast import ContentBundle, generate_episode_slug, generate_podcast
from src.publish import publish_episode, publish_state_update, reencode_mp3
from src.rss import update_rss_feed
from src.scraper import PaperClubEvent, canonicalize_paper_url, scrape_events
from src.state import is_processed, load_state, save_state
from src.supplementary import download_supplementary
from src.youtube import find_and_download_video

logger = logging.getLogger("lspc")


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------


class SecretRedactFormatter(logging.Formatter):
    """Custom log formatter that redacts API keys and secrets."""

    patterns = [re.compile(p) for p in [
        r"sk-[a-zA-Z0-9_-]{20,}",      # OpenAI API keys (incl. sk-proj-)
        r"AIza[a-zA-Z0-9_-]{35}",       # Google API keys
        r"ghp_[a-zA-Z0-9]{36}",         # GitHub PATs
    ]]

    def format(self, record: logging.LogRecord) -> str:
        output = super().format(record)
        for pat in self.patterns:
            output = pat.sub("[REDACTED]", output)
        return output


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def setup_logging(config: PipelineConfig | None = None) -> logging.Logger:
    """Configure file + console logging with secret redaction.

    Returns the root 'lspc' logger.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"run_{timestamp}.log"

    fmt = "%(asctime)s %(name)s %(levelname)s %(message)s"
    redact_formatter = SecretRedactFormatter(fmt)

    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(redact_formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(redact_formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)

    return logging.getLogger("lspc")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Latent Space Paper Club -> Podcast pipeline"
    )
    parser.add_argument(
        "--paper-url", action="append", dest="paper_urls",
        help="Manual paper URL (repeatable, skips Luma scraping)",
    )
    parser.add_argument(
        "--video-url",
        help="Manual YouTube video URL (requires --paper-url)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-process event even if in processed.json.",
    )
    parser.add_argument(
        "--backfill", type=int, default=1, metavar="N",
        help="Scrape N most recent past events (default: 1)",
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def is_scheduled_day(schedule) -> bool:
    """Check if today is a scheduled run day."""
    today = datetime.now(timezone.utc).strftime("%A").lower()
    return today in [d.lower() for d in schedule.run_days]


def build_manual_event(
    paper_urls: list[str], video_url: str | None = None,
) -> PaperClubEvent:
    """Create a PaperClubEvent from manual CLI inputs."""
    canonical_urls = sorted(canonicalize_paper_url(u) for u in paper_urls)
    url_hash = hashlib.sha256(
        ",".join(canonical_urls).encode()
    ).hexdigest()[:12]
    event = PaperClubEvent(
        title="Manual Paper Club Entry",
        date=datetime.now(timezone.utc),
        event_url=f"https://manual.local/{url_hash}",
        paper_urls=canonical_urls,
        supplementary_urls=[],
    )
    if video_url:
        event._manual_video_url = video_url  # type: ignore[attr-defined]
    return event


def cleanup_tmp() -> None:
    """Remove temporary files from tmp/ directory."""
    tmp_dir = Path("tmp")
    if tmp_dir.exists():
        for f in tmp_dir.iterdir():
            if f.is_file() and f.name != "pipeline.lock":
                try:
                    f.unlink()
                except OSError:
                    pass


def retry_with_backoff(func, max_retries: int = 3, backoff_base: int = 60):
    """Retry a function with exponential backoff.

    Returns the function result on success, or re-raises
    the last exception on final failure.
    """
    import time

    last_exc = None
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                wait = backoff_base * (2 ** attempt)
                logger.warning(
                    "Attempt %d/%d failed: %s. Retrying in %ds...",
                    attempt + 1, max_retries, exc, wait,
                )
                time.sleep(wait)
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Core pipeline logic
# ---------------------------------------------------------------------------


def process_single_event(
    event: PaperClubEvent, state: dict, config: PipelineConfig,
) -> int:
    """Process a single event through the full pipeline.

    Returns exit code:
        0 = success
        1 = fatal error
        2 = video not found (retriable)
        4 = publish error
        5 = no papers (retriable)
    """
    slug = generate_episode_slug(event)
    episode_path = Path(f"docs/episodes/{slug}.mp3")

    # Idempotent retry: episode exists but state/feed incomplete
    if episode_path.exists():
        try:
            publish_state_update(
                event, slug, state, config,
                repair_feed=True, episode_path=episode_path,
            )
            return 0
        except PublishError:
            return 4

    # Check paper availability
    if not event.paper_urls:
        logger.warning("No paper URLs for event: %s", event.title)
        return 5

    # Download papers
    tmp_dir = Path("tmp")
    tmp_dir.mkdir(exist_ok=True)
    papers = download_all_papers(event.paper_urls, tmp_dir, config.security)

    # Download video
    manual_video_url = getattr(event, "_manual_video_url", None)
    if manual_video_url:
        from src.youtube import download_audio
        # Extract video ID from URL
        video_id = manual_video_url.split("v=")[-1].split("&")[0]
        audio_path = download_audio(video_id, tmp_dir)
        from src.youtube import VideoMetadata
        video = VideoMetadata(
            video_id=video_id,
            video_url=manual_video_url,
            title=event.title,
            audio_path=audio_path,
        )
    else:
        video = retry_with_backoff(
            lambda: find_and_download_video(event, config.youtube),
            max_retries=config.errors.max_retries,
            backoff_base=config.errors.backoff_base,
        )

    if video is None:
        return 2  # video not yet uploaded

    # Download supplementary
    supplementary = download_supplementary(
        event.supplementary_urls, tmp_dir,
        max_bytes=config.security.max_supplementary_bytes,
    )

    # Build content bundle
    bundle = ContentBundle(
        paper_paths=papers,
        audio_path=video.audio_path if video else None,
        supplementary_paths=supplementary,
    )

    # Generate podcast
    try:
        mp3_path = retry_with_backoff(
            lambda: generate_podcast(bundle, event, config.notebooklm),
            max_retries=config.errors.max_retries,
            backoff_base=config.errors.backoff_base,
        )
    except PodcastGenerationError:
        if not config.fallback.enabled:
            raise
        logger.warning("NotebookLM failed, trying LLM+TTS fallback")
        try:
            from src.fallback import generate_fallback_podcast
            mp3_path = generate_fallback_podcast(bundle, event, config)
        except ImportError:
            logger.error("Fallback module not available")
            return 1

    # Re-encode if >95MB BEFORE RSS update
    file_size = mp3_path.stat().st_size
    if file_size > 95_000_000:
        reencode_mp3(mp3_path, bitrate="auto")
        file_size = mp3_path.stat().st_size
        if file_size > 95_000_000:
            raise PublishError(f"MP3 still too large after re-encode: {file_size}")

    # Update RSS feed
    update_rss_feed(mp3_path, event, slug, papers, config.rss)

    # Publish
    try:
        publish_episode(mp3_path, event, slug, state, config)
    except PublishError as e:
        logger.error("Publish failed: %s", e)
        return 4

    return 0


def run_pipeline(
    config: PipelineConfig, args: argparse.Namespace | None = None,
) -> int:
    """Run the full pipeline.

    Returns exit code:
        0 = success (or skipped)
        1 = unrecoverable error
        2 = retriable: no matching YouTube video
        3 = lock held by another process
        4 = git push failed (retriable)
        5 = retriable: paper URLs not yet in event
    """
    if args is None:
        args = parse_args([])

    Path("tmp").mkdir(exist_ok=True)
    Path("logs").mkdir(exist_ok=True)

    # Acquire file lock
    lock = FileLock("tmp/pipeline.lock")
    try:
        lock.acquire(timeout=0)
    except Timeout:
        logger.warning("Lock held by another process")
        return 3

    try:
        # Sync with remote
        result = subprocess.run(
            ["git", "pull", "--ff-only", "origin", "main"],
            capture_output=True,
        )
        if result.returncode != 0:
            logger.error(
                "git pull failed: %s", result.stderr.decode()
            )
            return 1

        # Validate: --video-url requires --paper-url
        if args.video_url and not args.paper_urls:
            logger.error("--video-url requires --paper-url")
            return 1

        # Day-gating
        manual_override = args.paper_urls or args.force
        if not manual_override and not is_scheduled_day(config.schedule):
            return 0

        # Scrape or use manual input
        if args.paper_urls:
            events = [build_manual_event(args.paper_urls, args.video_url)]
        else:
            events = scrape_events(config.luma, limit=args.backfill)

        # Filter to unprocessed events
        state = load_state(Path("processed.json"))
        if not args.force:
            events = [e for e in events if not is_processed(e, state)]
        if not events:
            return 0

        # Process each event
        severity = {1: 4, 4: 3, 5: 2, 2: 1}
        worst_code = 0
        for event in events:
            exit_code = process_single_event(event, state, config)
            if exit_code == 1:
                return 1
            if exit_code != 0:
                if severity.get(exit_code, 0) > severity.get(worst_code, 0):
                    worst_code = exit_code

        return worst_code

    except LSPCError as e:
        logger.error("Pipeline failed: %s", e)
        return 1
    except subprocess.CalledProcessError as e:
        logger.error("Subprocess failed: %s", e)
        return 1
    finally:
        lock.release()
        cleanup_tmp()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point for ``python -m src.pipeline``."""
    args = parse_args()
    config = load_config(args.config)
    setup_logging(config)
    exit_code = run_pipeline(config, args)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
