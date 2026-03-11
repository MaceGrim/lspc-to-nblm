"""Generate a podcast episode from downloaded Paper Club content.

Uses Gemini for dialogue generation and edge-tts for audio synthesis.
Run after e2e_test.py has downloaded paper + audio.
"""

import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("podcast_gen")

from src.config import load_config
from src.podcast import extract_text_from_pdfs, generate_episode_slug, ContentBundle
from src.scraper import scrape_events
from src.youtube import find_paper_club_video, _list_channel_videos
from src.fallback import strip_vtt_timestamps

config = load_config(Path("config.yaml"))
tmp_dir = Path("tmp")


# ── Step 1: Find the event with a matching video ─────────────────────
logger.info("Finding event with matching YouTube video...")
all_events = scrape_events(config.luma, limit=10)

# Find the most recent event that has both paper URLs and a YouTube match
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

logger.info(f"Event: {event.title} ({event.date.date()})")
logger.info(f"Video: https://www.youtube.com/watch?v={video_id}")
logger.info(f"Papers: {event.paper_urls}")

slug = generate_episode_slug(event)

# ── Step 2: Get paper text ───────────────────────────────────────────
logger.info("Extracting paper text...")
from src.papers import download_all_papers
paper_paths = download_all_papers(event.paper_urls, tmp_dir, config.security)
paper_text = extract_text_from_pdfs(paper_paths)
logger.info(f"Extracted {len(paper_text)} chars from {len(paper_paths)} paper(s)")

# ── Step 3: Get transcript from YouTube captions ─────────────────────
logger.info("Extracting YouTube captions...")
video_url = f"https://www.youtube.com/watch?v={video_id}"

# Try to get captions
for stale in tmp_dir.glob("captions*.vtt"):
    stale.unlink(missing_ok=True)

transcript = ""
for sub_flag in ["--write-sub", "--write-auto-sub"]:
    result = subprocess.run(
        ["yt-dlp", sub_flag, "--sub-lang", "en", "--skip-download",
         "--sub-format", "vtt", "-o", "tmp/captions", video_url],
        capture_output=True, text=True, timeout=60,
    )
    vtt_files = list(tmp_dir.glob("captions*.vtt"))
    if vtt_files:
        raw_vtt = vtt_files[0].read_text()
        transcript = strip_vtt_timestamps(raw_vtt)
        logger.info(f"Got transcript: {len(transcript)} chars")
        break

if not transcript:
    logger.warning("No captions available, generating podcast from paper text only")

# ── Step 4: Generate dialogue via Gemini ─────────────────────────────
logger.info("Generating dialogue via Gemini...")
from litellm import completion

prompt = config.notebooklm.prompt

messages = [
    {
        "role": "system",
        "content": (
            "You are creating a two-speaker deep-dive podcast script about an AI research paper. "
            "The two speakers are HOST (curious, asks probing 'how exactly does that work?' questions, "
            "pushes for concrete details) and EXPERT (deeply knowledgeable, explains the actual mechanisms, "
            "algorithms, math, and architecture choices — not just high-level summaries). "
            "Format EVERY line as either 'HOST: ...' or 'EXPERT: ...'. "
            "\n\nCRITICAL: When discussing any contribution or solution, you MUST explain HOW it works "
            "mechanistically. Don't say 'they use a novel loss function' — describe the loss function. "
            "Don't say 'they improved the architecture' — explain what changed and why. Walk through "
            "equations, training procedures, architectural diagrams, and ablation results with concrete "
            "numbers from the paper. The listener is a technical ML practitioner.\n\n"
            f"Additional guidance: {prompt}\n\n"
            "The podcast should be approximately 15-20 minutes when read aloud (about 3000-4000 words total)."
        ),
    },
    {
        "role": "user",
        "content": (
            f"Paper title: {event.title}\n\n"
            f"Paper text (first 50k chars):\n{paper_text[:50000]}\n\n"
            + (f"Discussion transcript (from live Paper Club session):\n{transcript[:30000]}" if transcript else
               "No discussion transcript available - focus on the paper content.")
        ),
    },
]

response = completion(
    model="gemini/gemini-2.0-flash",
    messages=messages,
)
dialogue_text = response.choices[0].message.content
# Strip markdown code fences if present
if dialogue_text.startswith("```"):
    lines = dialogue_text.split("\n")
    # Remove first line (```...) and last line (```)
    if lines[-1].strip() == "```":
        lines = lines[1:-1]
    else:
        lines = lines[1:]
    dialogue_text = "\n".join(lines)
logger.info(f"Generated dialogue: {len(dialogue_text)} chars")

# Save the dialogue for reference
dialogue_path = tmp_dir / f"{slug}_dialogue.txt"
dialogue_path.write_text(dialogue_text)
logger.info(f"Dialogue saved to {dialogue_path}")

# ── Step 5: Parse dialogue into speaker turns ────────────────────────
from src.fallback import parse_speaker_chunks

speaker_chunks = parse_speaker_chunks(dialogue_text, max_chars=4096)
logger.info(f"Parsed {len(speaker_chunks)} speaker turns")

# ── Step 6: Synthesize audio with edge-tts ───────────────────────────
logger.info("Synthesizing audio with edge-tts...")

# Use two distinct voices
HOST_VOICE = "en-US-GuyNeural"
EXPERT_VOICE = "en-US-JennyNeural"

async def synthesize_segment(text: str, voice: str, output_path: Path):
    """Synthesize a single segment with edge-tts."""
    import edge_tts
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(output_path))

async def synthesize_all():
    segment_paths = []
    for i, (speaker, text) in enumerate(speaker_chunks):
        if len(text.strip()) < 5:
            logger.warning(f"  Skipping segment {i+1}: too short ({text!r})")
            continue
        voice = HOST_VOICE if speaker == "HOST" else EXPERT_VOICE
        seg_path = tmp_dir / f"tts_seg_{i:04d}.mp3"
        logger.info(f"  Segment {i+1}/{len(speaker_chunks)}: {speaker} ({len(text)} chars)")
        try:
            await synthesize_segment(text, voice, seg_path)
            segment_paths.append(seg_path)
        except Exception as e:
            logger.warning(f"  Failed segment {i+1}: {e}")
    return segment_paths

segment_paths = asyncio.run(synthesize_all())
logger.info(f"Synthesized {len(segment_paths)} audio segments")

# ── Step 7: Combine audio segments ───────────────────────────────────
logger.info("Combining audio segments...")
from pydub import AudioSegment

combined = AudioSegment.empty()
for seg_path in segment_paths:
    try:
        segment = AudioSegment.from_mp3(str(seg_path))
        combined += segment
        # Add a brief pause between speakers
        combined += AudioSegment.silent(duration=300)  # 300ms pause
    except Exception as e:
        logger.warning(f"Failed to load segment {seg_path}: {e}")

output_dir = Path("docs/episodes")
output_dir.mkdir(parents=True, exist_ok=True)
output_path = output_dir / f"{slug}.mp3"
# Resample to 44.1kHz for Spotify compatibility (MPEG-1 Layer 3 required)
combined = combined.set_frame_rate(44100)
combined.export(str(output_path), format="mp3", bitrate="128k")

duration_mins = len(combined) / 1000 / 60
size_mb = output_path.stat().st_size / 1_000_000
logger.info(f"Podcast generated: {output_path}")
logger.info(f"Duration: {duration_mins:.1f} minutes")
logger.info(f"Size: {size_mb:.1f} MB")

# Clean up segment files
for seg_path in segment_paths:
    seg_path.unlink(missing_ok=True)

print(f"\n{'='*60}")
print(f"  PODCAST GENERATED SUCCESSFULLY!")
print(f"{'='*60}")
print(f"  Event:    {event.title}")
print(f"  Date:     {event.date.date()}")
print(f"  Paper:    {event.paper_urls}")
print(f"  Video:    https://www.youtube.com/watch?v={video_id}")
print(f"  Output:   {output_path}")
print(f"  Duration: {duration_mins:.1f} minutes")
print(f"  Size:     {size_mb:.1f} MB")
print(f"{'='*60}")
