"""Batch generate NotebookLM podcast episodes for all unprocessed events.

Iterates through Paper Club events that have both papers and YouTube videos,
skips any that already have episodes, and generates via NotebookLM CLI.
"""

import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("batch_nblm")

from src.config import load_config
from src.podcast import generate_episode_slug
from src.scraper import scrape_events
from src.youtube import _list_channel_videos, find_paper_club_video, parse_yt_date, title_similarity

config = load_config(Path("config.yaml"))
output_dir = Path("docs/episodes")
output_dir.mkdir(parents=True, exist_ok=True)


def run_nblm(*args, json_output=False, timeout=120):
    """Run a notebooklm CLI command and return output."""
    cmd = ["notebooklm"] + list(args)
    if json_output:
        cmd.append("--json")
    logger.debug(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        logger.error(f"Command failed: {' '.join(cmd)}")
        logger.error(f"stderr: {result.stderr[:500]}")
        raise RuntimeError(f"notebooklm command failed: {result.stderr[:200]}")
    if json_output:
        return json.loads(result.stdout)
    return result.stdout


def generate_episode(event, video_id=None):
    """Generate a single NotebookLM episode for an event."""
    slug = generate_episode_slug(event)
    mp3_path = output_dir / f"{slug}.mp3"

    if mp3_path.exists() and mp3_path.stat().st_size > 100_000:
        logger.info(f"SKIP (already exists): {slug} - {event.title}")
        return mp3_path

    paper_only = video_id is None
    logger.info(f"Generating: {event.title} ({event.date.date()}) {'[PAPER ONLY]' if paper_only else ''}")
    logger.info(f"  Papers: {event.paper_urls}")
    if video_id:
        logger.info(f"  Video: https://www.youtube.com/watch?v={video_id}")

    # Create notebook
    notebook_title = f"LSPC: {event.title} ({event.date.strftime('%Y-%m-%d')})"
    create_result = run_nblm("create", notebook_title, json_output=True)
    notebook_id = create_result.get("notebook", {}).get("id")
    if not notebook_id:
        raise RuntimeError(f"Could not get notebook ID: {create_result}")
    logger.info(f"  Notebook: {notebook_id}")
    run_nblm("use", notebook_id)

    try:
        # Add sources
        for paper_url in event.paper_urls:
            logger.info(f"  Adding paper: {paper_url}")
            try:
                run_nblm("source", "add", paper_url, "-n", notebook_id)
            except Exception as e:
                logger.warning(f"  Failed to add paper: {e}")

        if video_id:
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            logger.info(f"  Adding video: {video_url}")
            try:
                run_nblm("source", "add", video_url, "-n", notebook_id)
            except Exception as e:
                logger.warning(f"  Failed to add video: {e}")

        # Add supplementary URLs (skip social media)
        skip_domains = {"x.com", "twitter.com", "instagram.com", "luma.com", "lu.ma",
                        "help.luma.com", "sli.do", "app.sli.do"}
        for supp_url in event.supplementary_urls:
            host = (urlparse(supp_url).hostname or "").lower().removeprefix("www.")
            if host in skip_domains:
                continue
            logger.info(f"  Adding supplementary: {supp_url}")
            try:
                run_nblm("source", "add", supp_url, "-n", notebook_id)
            except Exception as e:
                logger.warning(f"  Failed: {e}")

        # Generate audio
        prompt = config.notebooklm.prompt
        length = "default" if config.notebooklm.length == "standard" else config.notebooklm.length
        gen_result = run_nblm(
            "generate", "audio", prompt,
            "-n", notebook_id,
            "--format", config.notebooklm.format,
            "--length", length,
            "--no-wait",
            json_output=True,
            timeout=120,
        )
        task_id = gen_result.get("task_id")
        if not task_id:
            raise RuntimeError(f"No task_id: {gen_result}")

        logger.info(f"  Waiting for generation (task {task_id})...")
        wait_result = run_nblm(
            "artifact", "wait", task_id,
            "-n", notebook_id,
            "--timeout", "600",
            json_output=True,
            timeout=660,
        )

        if wait_result.get("status") != "completed":
            raise RuntimeError(f"Generation failed: {wait_result}")

        # Download
        raw_path = output_dir / f"{slug}_raw.mp3"
        run_nblm(
            "download", "audio", str(raw_path),
            "-n", notebook_id,
            "--latest", "--force",
            timeout=300,
        )

        if not raw_path.exists():
            raise RuntimeError("Download failed - file not found")

        # Convert M4A to real MP3
        logger.info("  Converting to MP3...")
        conv_result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(raw_path),
             "-map", "0:a", "-map_metadata", "-1",
             "-codec:a", "libmp3lame", "-ar", "44100", "-b:a", "128k",
             "-id3v2_version", "3", str(mp3_path)],
            capture_output=True, text=True, timeout=300,
        )
        raw_path.unlink(missing_ok=True)

        if conv_result.returncode != 0 or not mp3_path.exists():
            raise RuntimeError(f"ffmpeg failed: {conv_result.stderr[:200]}")

        size_mb = mp3_path.stat().st_size / 1_000_000
        logger.info(f"  Done: {mp3_path} ({size_mb:.1f} MB)")
        return mp3_path

    finally:
        logger.info(f"  Cleaning up notebook {notebook_id}")
        try:
            run_nblm("delete", "-n", notebook_id, "-y")
        except Exception as e:
            logger.warning(f"  Failed to delete notebook: {e}")


# ── Matching helper (uses pre-fetched video list + LLM verification) ──
def match_video(event, videos, match_window_days):
    """Match an event to a video using LLM verification."""
    from litellm import completion

    # Gather candidates within date window
    candidates = []
    for v in videos:
        v_title = v.get("title", "").lower()
        if "paper club" not in v_title:
            continue
        upload_date = parse_yt_date(v.get("upload_date") or v.get("release_date"))
        if upload_date:
            delta_days = abs((upload_date - event.date.date()).days)
            if delta_days <= match_window_days:
                candidates.append(v)

    if not candidates:
        return None

    # Use Gemini Flash to pick the correct match
    candidate_list = "\n".join(
        f"  {i+1}. [{v['id']}] {v.get('title', 'no title')}"
        for i, v in enumerate(candidates)
    )
    prompt = (
        f"Which YouTube video is the recording of this Paper Club event?\n\n"
        f"Event: {event.title} (date: {event.date.date()})\n"
        f"Papers: {event.paper_urls}\n\n"
        f"Candidate videos:\n{candidate_list}\n\n"
        f"Reply with ONLY the video ID (e.g. 'abc123') of the best match, "
        f"or 'NONE' if none of these videos match this event."
    )

    try:
        resp = completion(
            model="gemini/gemini-2.0-flash",
            messages=[{"role": "user", "content": prompt}],
        )
        answer = resp.choices[0].message.content.strip()
        logger.info(f"  LLM match: '{answer}'")

        if answer.upper() == "NONE":
            return None

        # Extract video ID from response
        for v in candidates:
            if v["id"] in answer:
                return v["id"]

        # If LLM returned something unexpected, no match
        logger.warning(f"  LLM returned unexpected match: {answer}")
        return None
    except Exception as e:
        logger.warning(f"  LLM matching failed: {e}, falling back to title similarity")
        # Fallback to original scoring
        scored = []
        for v in candidates:
            title_sim = title_similarity(event.title.lower(), v.get("title", "").lower())
            ud = parse_yt_date(v.get("upload_date") or v.get("release_date"))
            delta = abs((ud - event.date.date()).days) if ud else 999
            score = delta - (title_sim * 3)
            scored.append((v, score, ud))
        if not scored:
            return None
        best = min(scored, key=lambda x: (x[1], -x[2].toordinal()))[0]
        return best["id"]


# ── Main ──────────────────────────────────────────────────────────────
logger.info("Finding events with papers + YouTube videos...")
all_events = scrape_events(config.luma, limit=20)

# Fetch YouTube video list ONCE
logger.info("Fetching YouTube video list (one-time)...")
videos = _list_channel_videos(config.youtube)
logger.info(f"Found {len(videos)} Paper Club videos on YouTube")

episodes = []
for ev in all_events:
    if not ev.paper_urls:
        continue
    vid = match_video(ev, videos, config.youtube.match_window_days)
    episodes.append((ev, vid))  # vid may be None (paper-only)

logger.info(f"Found {len(episodes)} events ready for podcast generation")

generated = []
failed = []
skipped = []

for i, (event, video_id) in enumerate(episodes):
    slug = generate_episode_slug(event)
    mp3_path = output_dir / f"{slug}.mp3"

    paper_only = video_id is None
    tag = " [Paper Only]" if paper_only else ""

    print(f"\n{'='*60}")
    print(f"  [{i+1}/{len(episodes)}] {event.title}{tag}")
    print(f"  Date: {event.date.date()}")
    print(f"{'='*60}")

    if mp3_path.exists() and mp3_path.stat().st_size > 100_000:
        logger.info(f"Already exists, skipping")
        skipped.append(event.title)
        continue

    try:
        result = generate_episode(event, video_id)
        generated.append(event.title)
    except Exception as e:
        logger.error(f"FAILED: {e}")
        failed.append((event.title, str(e)))

# ── Summary ───────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  BATCH GENERATION COMPLETE")
print(f"{'='*60}")
print(f"  Generated: {len(generated)}")
print(f"  Skipped:   {len(skipped)}")
print(f"  Failed:    {len(failed)}")
for title in generated:
    print(f"    + {title}")
for title in skipped:
    print(f"    = {title}")
for title, err in failed:
    print(f"    ! {title}: {err[:80]}")
print(f"{'='*60}")
