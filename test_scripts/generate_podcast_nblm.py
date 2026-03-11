"""Generate a podcast episode using NotebookLM.

Creates a notebook, uploads paper + YouTube video + supplementary content,
then generates an audio deep-dive podcast.
"""

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("nblm_gen")


def run_nblm(*args, json_output=False, timeout=120):
    """Run a notebooklm CLI command and return output."""
    cmd = ["notebooklm"] + list(args)
    if json_output:
        cmd.append("--json")
    logger.debug(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        logger.error(f"Command failed: {' '.join(cmd)}")
        logger.error(f"stdout: {result.stdout}")
        logger.error(f"stderr: {result.stderr}")
        raise RuntimeError(f"notebooklm command failed: {result.stderr}")
    if json_output:
        return json.loads(result.stdout)
    return result.stdout


from src.config import load_config
from src.podcast import generate_episode_slug
from src.scraper import scrape_events
from src.youtube import find_paper_club_video

config = load_config(Path("config.yaml"))

# ── Step 1: Find event with matching video ───────────────────────────
logger.info("Finding event with matching YouTube video...")
all_events = scrape_events(config.luma, limit=10)

event = None
video_id = None
for ev in all_events:
    if not ev.paper_urls:
        continue
    vid = find_paper_club_video(ev, config.youtube)
    if vid:
        event = ev
        video_id = vid
        break

if not event or not video_id:
    logger.error("No event found with both papers and YouTube video")
    sys.exit(1)

slug = generate_episode_slug(event)
video_url = f"https://www.youtube.com/watch?v={video_id}"
logger.info(f"Event: {event.title} ({event.date.date()})")
logger.info(f"Video: {video_url}")
logger.info(f"Papers: {event.paper_urls}")

# ── Step 2: Create NotebookLM notebook ───────────────────────────────
logger.info("Creating NotebookLM notebook...")
notebook_title = f"LSPC: {event.title} ({event.date.strftime('%Y-%m-%d')})"
create_result = run_nblm("create", notebook_title, json_output=True)
notebook_id = create_result.get("notebook", {}).get("id")
if not notebook_id:
    logger.error(f"Could not get notebook ID from: {create_result}")
    sys.exit(1)
logger.info(f"Created notebook: {notebook_id}")

# Set as current notebook
run_nblm("use", notebook_id)

try:
    # ── Step 3: Add sources ──────────────────────────────────────────
    logger.info("Adding sources to notebook...")

    # Add paper URLs
    for paper_url in event.paper_urls:
        logger.info(f"  Adding paper: {paper_url}")
        try:
            run_nblm("source", "add", paper_url, "-n", notebook_id)
        except Exception as e:
            logger.warning(f"  Failed to add paper URL: {e}")

    # Add YouTube video
    logger.info(f"  Adding YouTube video: {video_url}")
    try:
        run_nblm("source", "add", video_url, "-n", notebook_id)
    except Exception as e:
        logger.warning(f"  Failed to add YouTube video: {e}")

    # Add supplementary URLs (only useful ones, skip social media)
    skip_domains = {"x.com", "twitter.com", "instagram.com", "luma.com", "lu.ma",
                    "help.luma.com", "sli.do", "app.sli.do"}
    for supp_url in event.supplementary_urls:
        from urllib.parse import urlparse
        host = (urlparse(supp_url).hostname or "").lower().removeprefix("www.")
        if host in skip_domains:
            continue
        logger.info(f"  Adding supplementary: {supp_url}")
        try:
            run_nblm("source", "add", supp_url, "-n", notebook_id)
        except Exception as e:
            logger.warning(f"  Failed to add supplementary URL: {e}")

    # List sources to confirm
    sources = run_nblm("source", "list", "-n", notebook_id)
    logger.info(f"Sources in notebook:\n{sources}")

    # ── Step 4: Generate audio podcast ───────────────────────────────
    prompt = config.notebooklm.prompt
    logger.info(f"Generating audio with prompt: {prompt[:100]}...")

    length = "default" if config.notebooklm.length == "standard" else config.notebooklm.length
    gen_result = run_nblm(
        "generate", "audio",
        prompt,
        "-n", notebook_id,
        "--format", config.notebooklm.format,
        "--length", length,
        "--no-wait",
        json_output=True,
        timeout=120,
    )
    logger.info(f"Generation submitted: {json.dumps(gen_result, indent=2)[:500]}")

    task_id = gen_result.get("task_id")
    if not task_id:
        logger.error(f"No task_id in generation result: {gen_result}")
        sys.exit(1)

    # Wait for completion using artifact wait
    logger.info(f"Waiting for audio generation (task {task_id}) - this can take 5-10 minutes...")
    wait_result = run_nblm(
        "artifact", "wait", task_id,
        "-n", notebook_id,
        "--timeout", "600",
        json_output=True,
        timeout=660,
    )
    logger.info(f"Wait result: {json.dumps(wait_result, indent=2)}")

    status = wait_result.get("status", "")
    if status != "completed":
        logger.error(f"Audio generation failed with status: {status}")
        if wait_result.get("error"):
            logger.error(f"Error: {wait_result['error']}")
        sys.exit(1)

    logger.info("Audio generation complete!")

    # ── Step 5: Download the generated audio ─────────────────────────
    output_dir = Path("docs/episodes")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{slug}_nblm.mp3"

    logger.info(f"Downloading audio to {output_path}...")
    run_nblm(
        "download", "audio",
        str(output_path),
        "-n", notebook_id,
        "--latest", "--force",
        timeout=300,
    )

    if output_path.exists():
        size_mb = output_path.stat().st_size / 1_000_000
        logger.info(f"Downloaded: {output_path} ({size_mb:.1f} MB)")
    else:
        logger.error("Output file not found after download")
        sys.exit(1)

    # NotebookLM downloads M4A (MPEG-4) despite .mp3 extension — convert to real MP3
    logger.info("Converting to MP3 (NotebookLM outputs M4A)...")
    converted_path = output_path.with_suffix(".converted.mp3")
    conv_result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(output_path),
         "-codec:a", "libmp3lame", "-b:a", "128k", str(converted_path)],
        capture_output=True, text=True, timeout=300,
    )
    if conv_result.returncode == 0 and converted_path.exists():
        converted_path.replace(output_path)
        size_mb = output_path.stat().st_size / 1_000_000
        logger.info(f"Converted to MP3: {output_path} ({size_mb:.1f} MB)")
    else:
        logger.warning(f"ffmpeg conversion failed: {conv_result.stderr[:200]}")

    print(f"\n{'='*60}")
    print(f"  NOTEBOOKLM PODCAST GENERATED!")
    print(f"{'='*60}")
    print(f"  Event:    {event.title}")
    print(f"  Date:     {event.date.date()}")
    print(f"  Paper:    {event.paper_urls}")
    print(f"  Video:    {video_url}")
    print(f"  Output:   {output_path}")
    print(f"  Size:     {size_mb:.1f} MB")
    print(f"{'='*60}")

finally:
    # Clean up: delete the notebook
    logger.info(f"Cleaning up notebook {notebook_id}...")
    try:
        run_nblm("delete", "-n", notebook_id, "-y")
        logger.info("Notebook deleted.")
    except Exception as e:
        logger.warning(f"Failed to delete notebook: {e}")
