"""Git commit + push to GitHub Pages (two-commit sequence)."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from src.config import PipelineConfig
from src.errors import PublishError
from src.rss import feed_contains_guid, update_rss_feed
from src.scraper import PaperClubEvent
from src.state import load_state, mark_processed, save_state

logger = logging.getLogger(__name__)


def reencode_mp3(mp3_path: Path, bitrate: str = "auto") -> None:
    """Re-encode MP3 to lower bitrate using ffmpeg. Preserves metadata.

    If bitrate is "auto", calculates target bitrate to fit under 90MB.
    """
    if bitrate == "auto":
        # Use ffprobe to get duration
        probe_result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(mp3_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if probe_result.returncode == 0 and probe_result.stdout.strip():
            duration_secs = float(probe_result.stdout.strip())
            if duration_secs > 0:
                # Target 90MB with safety margin
                target_kbps = int((90_000_000 * 8) / duration_secs / 1000)
                bitrate = f"{max(32, min(target_kbps, 128))}k"
            else:
                bitrate = "96k"
        else:
            bitrate = "96k"

    tmp_output = mp3_path.with_suffix(".reencoded.mp3")
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(mp3_path),
            "-b:a", bitrate, "-map_metadata", "0", str(tmp_output),
        ],
        check=True, timeout=300,
    )
    tmp_output.replace(mp3_path)


def publish_episode(
    mp3_path: Path,
    event: PaperClubEvent,
    slug: str,
    state: dict,
    config: PipelineConfig,
) -> None:
    """Two-commit publish: episode+feed first, then state update.

    Raises PublishError on failure (never calls sys.exit).
    """
    date_str = event.date.strftime("%Y-%m-%d")

    # Step 1-3: Stage and commit episode + feed
    try:
        subprocess.run(
            ["git", "add", str(mp3_path), "docs/feed.xml"], check=True,
        )
        # Guard: only commit if there are staged changes
        has_changes = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], capture_output=True,
        ).returncode != 0
        if not has_changes:
            logger.info("No staged changes, skipping episode commit")
            publish_state_update(event, slug, state, config)
            return
        subprocess.run(
            ["git", "commit", "-m", f"Add episode: {event.title} ({date_str})"],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise PublishError(f"Git staging/commit failed: {e}") from e

    # Step 4: Push episode (with timeout and non-interactive mode)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(
        ["git", "push", "origin", "main"],
        capture_output=True, timeout=120, env=env,
    )
    if result.returncode != 0:
        logger.error("Push failed: %s", result.stderr.decode())
        subprocess.run(["git", "reset", "--hard", "HEAD~1"], check=True)
        raise PublishError("Episode push failed")

    # Step 5: Update state and push (no feed repair needed)
    publish_state_update(event, slug, state, config, repair_feed=False)


def publish_state_update(
    event: PaperClubEvent,
    slug: str,
    state: dict,
    config: PipelineConfig,
    repair_feed: bool = False,
    episode_path: Path | None = None,
) -> None:
    """Commit and push the processed.json state update.

    If this fails, the episode is already live. Next run detects
    the existing episode file and retries this step (idempotent).

    If repair_feed=True, checks AFTER reset whether the feed needs repair.
    If episode_path is provided, ensures the MP3 is staged and committed.
    """
    try:
        # Sync with remote to avoid non-fast-forward
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True,
        ).stdout.strip()
        if dirty:
            raise PublishError(
                f"Cannot update state: dirty working tree. "
                f"Manual intervention required. Files:\n{dirty}"
            )

        subprocess.run(
            ["git", "fetch", "origin", "main"], check=True, timeout=60,
        )
        subprocess.run(
            ["git", "reset", "--hard", "origin/main"], check=True,
        )

        # Reload state from disk after reset (remote may have newer entries)
        state = load_state(Path("processed.json"))

        # Check feed repair AFTER reset (not before, since local != remote)
        if repair_feed and not feed_contains_guid(slug):
            ep_path = episode_path or Path(f"docs/episodes/{slug}.mp3")
            update_rss_feed(
                ep_path, event, slug, [],
                config.rss, feed_dir=Path("docs"),
            )

        # If episode file exists locally but wasn't pushed, stage it
        if episode_path and episode_path.exists():
            subprocess.run(["git", "add", str(episode_path)], check=True)

        # Mark processed and save state
        mark_processed(event, slug, state)
        save_state(state, Path("processed.json"))

        subprocess.run(
            ["git", "add", "processed.json", "docs/feed.xml"], check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", f"Mark processed: {event.title}"],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise PublishError(f"State commit failed: {e}") from e

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(
        ["git", "push", "origin", "main"],
        capture_output=True, timeout=120, env=env,
    )
    if result.returncode != 0:
        logger.error("State push failed: %s", result.stderr.decode())
        subprocess.run(["git", "reset", "--hard", "HEAD~1"], check=True)
        raise PublishError(
            "State update push failed (episode is live, will retry)"
        )
