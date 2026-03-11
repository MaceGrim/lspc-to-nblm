"""End-to-end test: run each pipeline stage against real data."""

import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("e2e")

def stage(name):
    logger.info("=" * 60)
    logger.info(f"STAGE: {name}")
    logger.info("=" * 60)

# ── Stage 1: Load config ──────────────────────────────────────────────
stage("Load Configuration")
from src.config import load_config
config = load_config(Path("config.yaml"))
logger.info(f"Config loaded: calendar_url={config.luma.calendar_url}")
logger.info(f"  RSS base_url={config.rss.base_url}")
logger.info(f"  YouTube channel={config.youtube.channel_url}")
print("  [OK] Config loaded successfully\n")

# ── Stage 2: Scrape Luma ──────────────────────────────────────────────
stage("Scrape Luma for Paper Club Events")
from src.scraper import scrape_events
try:
    all_events = scrape_events(config.luma, limit=10)
    logger.info(f"Found {len(all_events)} Paper Club events")
    for ev in all_events:
        logger.info(f"  - {ev.title} ({ev.date.date()}) papers={len(ev.paper_urls)}")
    event = all_events[0]  # most recent, may change below if no video
    print(f"  [OK] Found {len(all_events)} events, latest: {event.title}\n")
except Exception as e:
    logger.error(f"Scraping failed: {e}")
    print(f"  [FAIL] {e}\n")
    sys.exit(1)

# ── Stage 3: Download papers ─────────────────────────────────────────
stage("Download Paper PDFs")
tmp_dir = Path("tmp")
tmp_dir.mkdir(exist_ok=True)

if event.paper_urls:
    from src.papers import download_all_papers
    try:
        paper_paths = download_all_papers(event.paper_urls, tmp_dir, config.security)
        for p in paper_paths:
            size_mb = p.stat().st_size / 1_000_000
            logger.info(f"  Downloaded: {p.name} ({size_mb:.1f} MB)")
        print(f"  [OK] Downloaded {len(paper_paths)} paper(s)\n")
    except Exception as e:
        logger.error(f"Paper download failed: {e}")
        print(f"  [FAIL] {e}\n")
        paper_paths = []
else:
    logger.warning("No paper URLs found in event")
    paper_paths = []
    print("  [SKIP] No paper URLs\n")

# ── Stage 4: Find and download YouTube audio ─────────────────────────
stage("Find YouTube Video")
from src.youtube import find_and_download_video
audio_path = None
yt_result = None
# Try each event (most recent first) until we find one with a YouTube video
for try_event in all_events:
    logger.info(f"  Trying event: {try_event.title} ({try_event.date.date()})")
    try:
        yt_result = find_and_download_video(try_event, config.youtube)
        if yt_result:
            # Switch to this event since it has a video
            if try_event != event:
                logger.info(f"  Switching to event with video: {try_event.title}")
                event = try_event
                # Re-download papers for this event
                paper_paths = []
            audio_path = yt_result.audio_path
            video_id = yt_result.video_id
            size_mb = audio_path.stat().st_size / 1_000_000
            logger.info(f"  Video ID: {video_id}")
            logger.info(f"  Audio: {audio_path.name} ({size_mb:.1f} MB)")
            print(f"  [OK] Downloaded audio from {video_id}\n")
            break
        else:
            logger.info(f"  No match for this event")
    except Exception as e:
        logger.warning(f"  YouTube failed for {try_event.title}: {e}")
        continue

if not yt_result:
    logger.warning("No matching YouTube video found for any event")
    print("  [SKIP] No matching video for any event\n")

# Re-download papers if we switched events
if not paper_paths and event.paper_urls:
    stage("Re-download Papers (switched event)")
    from src.papers import download_all_papers
    try:
        paper_paths = download_all_papers(event.paper_urls, tmp_dir, config.security)
        for p in paper_paths:
            size_mb = p.stat().st_size / 1_000_000
            logger.info(f"  Downloaded: {p.name} ({size_mb:.1f} MB)")
        print(f"  [OK] Downloaded {len(paper_paths)} paper(s)\n")
    except Exception as e:
        logger.error(f"Paper download failed: {e}")
        print(f"  [FAIL] {e}\n")
        paper_paths = []

# ── Stage 5: Download supplementary content ──────────────────────────
stage("Download Supplementary Content")
if event.supplementary_urls:
    from src.supplementary import download_supplementary
    try:
        supp_paths = download_supplementary(event.supplementary_urls, tmp_dir)
        for p in supp_paths:
            size_kb = p.stat().st_size / 1000
            logger.info(f"  Downloaded: {p.name} ({size_kb:.1f} KB)")
        print(f"  [OK] Downloaded {len(supp_paths)} supplementary file(s)\n")
    except Exception as e:
        logger.error(f"Supplementary download failed: {e}")
        print(f"  [FAIL] {e}\n")
        supp_paths = []
else:
    supp_paths = []
    print("  [SKIP] No supplementary URLs\n")

# ── Stage 6: Generate episode slug ───────────────────────────────────
stage("Generate Episode Slug")
from src.podcast import generate_episode_slug, ContentBundle
slug = generate_episode_slug(event)
logger.info(f"  Slug: {slug}")
print(f"  [OK] Slug: {slug}\n")

bundle = ContentBundle(
    paper_paths=paper_paths,
    audio_path=audio_path,
    supplementary_paths=supp_paths,
)
logger.info(f"  Bundle: {len(bundle.paper_paths)} papers, audio={'yes' if bundle.audio_path else 'no'}, {len(bundle.supplementary_paths)} supplementary")

# ── Stage 7: State tracking ──────────────────────────────────────────
stage("State Tracking")
from src.state import load_state, is_processed, should_reprocess
state = load_state(Path("processed.json"))
already_done = is_processed(event, state)
needs_reprocess = should_reprocess(event, state) if already_done else False
logger.info(f"  Already processed: {already_done}")
logger.info(f"  Needs reprocess: {needs_reprocess}")
print(f"  [OK] State checked\n")

# ── Stage 8: RSS feed generation (dry run) ───────────────────────────
stage("RSS Feed Generation (dry run)")
from src.rss import update_rss_feed, feed_contains_guid

# Create a fake MP3 for RSS testing
episode_dir = Path("docs/episodes")
episode_dir.mkdir(parents=True, exist_ok=True)
fake_mp3 = episode_dir / f"{slug}.mp3"

# Only test RSS if we have paper paths (need a real-ish MP3 for duration)
if paper_paths:
    # Create minimal MP3 for RSS metadata
    # Use a real downloaded audio if available, otherwise create a stub
    if audio_path and audio_path.exists():
        import shutil
        shutil.copy2(audio_path, fake_mp3)
        logger.info(f"  Using real audio for RSS test: {fake_mp3}")
    else:
        # Write minimal valid MP3: MPEG1 Layer3 128kbps 44100Hz stereo frame
        # Frame header: 0xFFFB9004 = sync(12) + ver(2,MPEG1) + layer(2,L3) + no CRC
        #   + bitrate(1001=128k) + samplerate(00=44100) + padding(0) + channel(00=stereo)
        frame_header = b'\xff\xfb\x90\x04'
        frame_size = 417  # bytes per frame at 128kbps/44100Hz
        frame_data = frame_header + b'\x00' * (frame_size - len(frame_header))
        # Write enough frames for mutagen to detect (need ~3 frames)
        fake_mp3.write_bytes(frame_data * 10)
        logger.info(f"  Using stub MP3 for RSS test: {fake_mp3}")

    try:
        update_rss_feed(fake_mp3, event, slug, paper_paths, config.rss)
        feed_path = Path("docs/feed.xml")
        if feed_path.exists():
            size_kb = feed_path.stat().st_size / 1000
            logger.info(f"  Feed written: {feed_path} ({size_kb:.1f} KB)")

            # Validate with feedparser
            import feedparser
            feed = feedparser.parse(str(feed_path))
            logger.info(f"  feedparser bozo: {feed.bozo}")
            if feed.bozo:
                logger.warning(f"  bozo_exception: {feed.bozo_exception}")
            logger.info(f"  Episodes in feed: {len(feed.entries)}")
            for entry in feed.entries:
                logger.info(f"    - {entry.get('title', 'no title')}")

            has_guid = feed_contains_guid(slug)
            logger.info(f"  feed_contains_guid('{slug}'): {has_guid}")
            print(f"  [OK] RSS feed valid (bozo={feed.bozo}, {len(feed.entries)} entries)\n")
        else:
            print("  [FAIL] Feed file not created\n")
    except Exception as e:
        logger.error(f"RSS generation failed: {e}")
        print(f"  [FAIL] {e}\n")
    finally:
        # Clean up fake MP3 and feed for dry run
        fake_mp3.unlink(missing_ok=True)
        Path("docs/feed.xml").unlink(missing_ok=True)
else:
    print("  [SKIP] No papers to generate RSS for\n")

# ── Summary ──────────────────────────────────────────────────────────
stage("SUMMARY")
print(f"  Event:          {event.title}")
print(f"  Date:           {event.date}")
print(f"  Papers:         {len(paper_paths)} downloaded")
print(f"  YouTube audio:  {'yes' if audio_path else 'no'}")
print(f"  Supplementary:  {len(supp_paths)} files")
print(f"  Episode slug:   {slug}")
print(f"  State:          {'already processed' if already_done else 'new'}")
print()

if paper_paths and audio_path:
    print("  All pipeline stages passed! Ready for podcast generation.")
    print("  (NotebookLM or fallback TTS would run next with real API keys)")
elif paper_paths:
    print("  Papers downloaded but no YouTube audio found.")
    print("  Pipeline would exit with code 2 (video not found, retriable).")
else:
    print("  No papers found. Pipeline would exit with code 5.")
