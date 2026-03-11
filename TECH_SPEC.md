# Latent Space Paper Club Podcast Pipeline — Technical Specification

## Overview

Python 3.10+ CLI application that automates the pipeline from Latent Space Paper Club event discovery to podcast RSS feed publication. Runs as a **daily cron job** on WSL2. Exits quickly (code 0) when no unprocessed events exist. Orchestrates web scraping, content download, podcast generation via NotebookLM (with LLM+TTS fallback), and GitHub Pages publishing.

## System Requirements

| Requirement | Details |
|-------------|---------|
| **OS** | WSL2 (Ubuntu recommended) |
| **Python** | 3.10+ |
| **ffmpeg** | Required by yt-dlp for audio extraction |
| **git** | For GitHub Pages publishing (SSH auth configured) |
| **Chromium/Chrome** | Required by `notebooklm-py[browser]` (Playwright) |
| **Playwright deps** | `playwright install --with-deps chromium` |

**Important**: The repository and lock files should reside on the WSL ext4 filesystem (e.g., `~/projects/lspc-to-nblm`), not on `/mnt/*` mounted Windows drives, for reliable file locking and better I/O performance. Working on `/mnt/o` is acceptable for development but production cron should use the native filesystem.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                      src/pipeline.py                              │
│  (orchestrator: scrape → download → generate → publish)           │
├──────────┬──────────┬──────────┬──────────┬──────────┬───────────┤
│scraper.py│papers.py │youtube.py│podcast_  │ rss.py   │publisher  │
│          │          │          │gen.py    │          │.py        │
│ Luma     │ arXiv    │ yt-dlp   │notebklm │ feedgen  │ git ops   │
│ scrape   │ PDF dl   │ audio dl │+ fallbk │ XML gen  │ push      │
└──────────┴──────────┴──────────┴──────────┴──────────┴───────────┘
     ↓           ↓          ↓           ↓          ↓         ↓
  config.py                        state.py
  (typed dataclass from config.yaml) (processed.json I/O)
```

### Module Responsibilities

| Module | Responsibility | Key Dependencies |
|--------|---------------|-----------------|
| `src/config.py` | Load + validate `config.yaml` into typed dataclass | `pyyaml`, `dataclasses` |
| `src/scraper.py` | Scrape Luma event pages, extract `PaperClubEvent` | `requests`, `beautifulsoup4` |
| `src/papers.py` | Download paper PDFs from arXiv or direct URLs | `arxiv`, `requests` |
| `src/youtube.py` | Find matching video, download audio, return metadata | `yt-dlp` (subprocess) |
| `src/supplementary.py` | Fetch blog posts, research pages as text | `trafilatura`, `requests` |
| `src/podcast_gen.py` | Generate podcast via NotebookLM + LLM+TTS fallback | `notebooklm-py`, `litellm`, `openai` |
| `src/rss.py` | Generate/update RSS feed XML | `xml.etree`, `mutagen`, `arxiv`, `pymupdf` |
| `src/publisher.py` | Git commit + push to GitHub Pages | `subprocess` (git) |
| `src/state.py` | Read/write `processed.json`, deduplication logic | `json`, `hashlib` |
| `src/pipeline.py` | Orchestrate full pipeline, CLI, retry logic, logging | all above |

## Data Models

### PaperClubEvent (scraper output)

```python
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class PaperClubEvent:
    title: str
    date: datetime  # timezone-aware, original TZ preserved (validated at parse time)
    event_url: str  # stable identifier, Luma event page URL
    paper_urls: list[str] = field(default_factory=list)
    supplementary_urls: list[str] = field(default_factory=list)
```

### VideoMetadata (youtube output)

```python
@dataclass
class VideoMetadata:
    video_id: str
    video_url: str
    title: str
    audio_path: Path  # downloaded MP3
```

### EpisodeBundle (pipeline intermediate)

```python
@dataclass
class EpisodeBundle:
    event: PaperClubEvent
    paper_paths: list[Path]          # downloaded PDFs
    video: VideoMetadata | None      # video info + downloaded audio
    supplementary_paths: list[Path]  # downloaded text files
    episode_slug: str                # {date_YYYYMMDD}-{hash[:8]}
```

### ProcessedEntry (state tracking)

```python
@dataclass
class ProcessedEntry:
    event_url: str         # stable identifier (key in processed.json)
    title: str
    date: str              # ISO 8601 with timezone offset
    paper_urls: list[str]  # canonicalized
    episode_slug: str
    episode_file: str      # relative path: docs/episodes/{slug}.mp3
    processed_at: str      # ISO 8601 with timezone offset
```

Note: `processed.json` is a JSON object keyed by canonicalized `event_url`. The `event_url` field in `ProcessedEntry` is redundant with the key but included for self-contained serialization.

### PipelineConfig (config output)

```python
@dataclass
class LumaConfig:
    calendar_url: str = "https://lu.ma/ls"
    event_filter: str = "Paper Club"

@dataclass
class YouTubeConfig:
    channel_url: str = "https://www.youtube.com/@LatentSpaceTV"
    match_window_days: int = 7
    playlist_depth: int = 30  # how many recent videos to scan; increase for backfill

@dataclass
class NotebookLMConfig:
    prompt: str = "Deeply explain this paper..."  # full prompt in config.example.yaml
    format: str = "deep-dive"
    length: str = "standard"

@dataclass
class FallbackConfig:
    enabled: bool = True  # set False to skip fallback when NotebookLM fails
    llm_model: str = "gpt-4o-mini"
    tts_model: str = "tts-1"
    tts_voices: list[str] = field(default_factory=lambda: ["alloy", "echo"])
    # Two voices for two-speaker dialogue: first voice = Host, second = Expert
    chunk_max_chars: int = 4096  # TTS per-call limit; script is chunked

@dataclass
class RSSConfig:
    title: str = "Latent Space Paper Club Deep Dives"
    description: str = ""
    author: str = "Mason Grimshaw"
    base_url: str = ""  # required, no default
    owner_name: str = "Mason Grimshaw"
    owner_email: str = ""  # required, no default
    category: str = "Technology"
    subcategory: str = "Tech News"
    explicit: bool = False

@dataclass
class ErrorConfig:
    max_retries: int = 3
    backoff_base: int = 60

@dataclass
class ScheduleConfig:
    run_days: list[str] = field(default_factory=lambda: [
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    ])
    # Default: run every day. Pipeline exits quickly (code 0) if no unprocessed
    # events exist. This ensures late-arriving papers/videos are caught promptly.
    # Note: schedule time is controlled by cron, not by this config.

@dataclass
class SecurityConfig:
    allowed_domains: list[str] = field(default_factory=lambda: [
        "arxiv.org", "openai.com", "anthropic.com", "ai.meta.com",
        "blog.google", "deepmind.google", "huggingface.co",
        "github.com", "github.io",
    ])
    max_download_bytes: int = 100_000_000  # 100MB per download (papers)
    max_supplementary_bytes: int = 5_000_000  # 5MB (HTML/text extraction)
    enforce_https: bool = True

@dataclass
class PipelineConfig:
    luma: LumaConfig
    youtube: YouTubeConfig
    notebooklm: NotebookLMConfig
    fallback: FallbackConfig
    rss: RSSConfig
    errors: ErrorConfig
    schedule: ScheduleConfig
    security: SecurityConfig
```

## URL Canonicalization

### Paper URLs

All paper URLs are canonicalized before comparison or storage:

```python
import re
from urllib.parse import urlparse, urlunparse

def canonicalize_paper_url(url: str) -> str:
    """Normalize arXiv URLs to abs/ form, strip version for arXiv only.
    For all URLs: strip fragments, upgrade http→https. Query params are
    preserved for non-arXiv URLs (may be needed for signed/download URLs).
    URLs without a scheme default to https."""
    # Only accept http/https; reject mailto:, javascript:, ftp:, etc.
    if url.startswith(("mailto:", "javascript:", "ftp:", "data:")):
        return url  # return as-is; downstream domain check will reject it
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    # arXiv: normalize to https://arxiv.org/abs/{id}
    if host == "arxiv.org" or host.endswith(".arxiv.org"):
        # Extract paper ID from /abs/XXXX, /pdf/XXXX, /html/XXXX
        match = re.search(r"/(abs|pdf|html)/(\d{4}\.\d{4,5})(v\d+)?", parsed.path)
        if match:
            return f"https://arxiv.org/abs/{match.group(2)}"
    # Non-arXiv: strip fragment only, preserve query params (may be required
    # for signed URLs, ?download=1, etc.), upgrade http to https
    scheme = "https" if parsed.scheme == "http" else parsed.scheme
    return urlunparse(parsed._replace(fragment="", scheme=scheme))
```

### Event URLs

Event URLs (Luma pages) have their own canonicalization:

```python
def canonicalize_event_url(url: str) -> str:
    """Normalize Luma event URLs for consistent keying in processed.json.

    Strips query params, fragments, trailing slashes, lowercases host,
    removes www. prefix. Requires a valid hostname.
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not host:
        raise ConfigError(f"Cannot canonicalize event URL without hostname: {url}")
    path = parsed.path.rstrip("/")
    return f"https://{host}{path}"
```

**Important**: `canonicalize_event_url()` is used for all `processed.json` keys and lookups. Never use `canonicalize_paper_url()` on event URLs.

## Episode Slug Generation

```python
import hashlib
from datetime import datetime

def generate_slug(event_url: str, event_date: datetime) -> str:
    """Stable slug: date + short hash of canonicalized event URL."""
    date_str = event_date.strftime("%Y%m%d")
    canonical = canonicalize_event_url(event_url)
    url_hash = hashlib.sha256(canonical.encode()).hexdigest()[:8]
    return f"{date_str}-{url_hash}"
```

## Helper Functions

```python
from datetime import date

def parse_yt_date(date_str: str | None) -> date | None:
    """Parse yt-dlp date string (YYYYMMDD format) to date object."""
    if not date_str or len(date_str) != 8:
        return None
    try:
        return date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
    except ValueError:
        return None

def find_pdf_for_url(url: str, paper_paths: list[Path]) -> Path | None:
    """Find the downloaded PDF corresponding to a paper URL.

    Matches by arXiv ID in filename or URL hash prefix.
    """
    canonical = canonicalize_paper_url(url)
    if "arxiv.org" in canonical:
        arxiv_id = canonical.split("/abs/")[-1]
        for p in paper_paths:
            if arxiv_id in p.name:
                return p
    else:
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]
        for p in paper_paths:
            if url_hash in p.name:
                return p
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


def download_video_direct(video_url: str) -> VideoMetadata:
    """Download audio from a specific YouTube URL (manual override)."""
    # Validate domain (only YouTube allowed)
    parsed = urlparse(video_url)
    host = (parsed.hostname or "").lower()
    if not (host == "youtube.com" or host.endswith(".youtube.com")
            or host == "youtu.be"):
        raise VideoNotFoundError(f"Video URL domain not allowed: {host}")
    Path("tmp").mkdir(exist_ok=True)
    result = subprocess.run(
        ["yt-dlp", "--dump-json", "--no-download", "--no-playlist", video_url],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise VideoNotFoundError(f"Cannot fetch metadata for {video_url}")
    meta = json.loads(result.stdout)

    output_template = f"tmp/%(id)s.%(ext)s"
    subprocess.run(
        ["yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "128K",
         "--no-playlist", "-o", output_template, video_url],
        check=True, timeout=600,
    )
    audio_path = Path(f"tmp/{meta['id']}.mp3")
    if not audio_path.exists():
        matches = list(Path("tmp").glob(f"{meta['id']}.*"))
        audio_path = matches[0] if matches else None
    if not audio_path:
        raise VideoNotFoundError(f"Downloaded audio not found for {meta['id']}")

    return VideoMetadata(
        video_id=meta["id"],
        video_url=video_url,
        title=meta.get("title", "Unknown"),
        audio_path=audio_path,
    )


def transcribe_with_whisper(audio_path: Path) -> str:
    """Transcribe audio using OpenAI Whisper API.

    Uses the openai package (already a dependency for TTS fallback).
    Requires OPENAI_API_KEY in environment.
    """
    from openai import OpenAI
    client = OpenAI()
    with open(audio_path, "rb") as f:
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
        )
    return transcript.text


def is_scheduled_day(schedule: ScheduleConfig) -> bool:
    """Check if today is a scheduled run day."""
    today = datetime.now().strftime("%A").lower()
    return today in [d.lower() for d in schedule.run_days]


def load_state(path: str) -> dict:
    """Load processed.json state file. Returns empty dict if missing."""
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text())
    return {}


def should_skip(event: PaperClubEvent, state: dict) -> bool:
    """Check if event should be skipped (already processed).

    Uses canonicalize_event_url for key lookup.
    Compares canonicalized paper_urls sets.
    """
    key = canonicalize_event_url(event.event_url)
    if key not in state:
        return False
    stored = state[key]
    stored_papers = set(stored.get("paper_urls", []))
    live_papers = set(canonicalize_paper_url(u) for u in event.paper_urls)
    # Skip only if paper sets match exactly
    return stored_papers == live_papers


def update_processed_json(event: PaperClubEvent, slug: str, state: dict):
    """Add event to processed.json state. Atomic write via temp file."""
    key = canonicalize_event_url(event.event_url)
    state[key] = {
        "event_url": event.event_url,
        "title": event.title,
        "date": event.date.isoformat(),
        "paper_urls": [canonicalize_paper_url(u) for u in event.paper_urls],
        "episode_slug": slug,
        "episode_file": f"docs/episodes/{slug}.mp3",
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }
    # Atomic write: write to temp file then rename
    tmp_path = Path("processed.json.tmp")
    tmp_path.write_text(json.dumps(state, indent=2))
    tmp_path.replace(Path("processed.json"))


def cleanup_tmp():
    """Remove temporary files from tmp/ directory.

    Preserves lock file and auth-expired flag.
    """
    preserve = {"pipeline.lock", "notebooklm_auth_expired"}
    tmp = Path("tmp")
    if tmp.exists():
        for f in tmp.iterdir():
            if f.name not in preserve:
                try:
                    f.unlink()
                except OSError:
                    pass
```

## CLI Interface

```python
# src/pipeline.py — CLI entry point
import argparse

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Latent Space Paper Club → Podcast pipeline")
    parser.add_argument("--paper-url", action="append", dest="paper_urls",
                        help="Manual paper URL (repeatable, skips Luma scraping)")
    parser.add_argument("--video-url",
                        help="Manual YouTube video URL (requires --paper-url)")
    parser.add_argument("--force", action="store_true",
                        help="Re-process event even if in processed.json. "
                        "Note: if the MP3 already exists, only state/feed are updated "
                        "(episodes are immutable once generated)")
    parser.add_argument("--backfill", type=int, default=1, metavar="N",
                        help="Scrape N most recent past events, then filter to unprocessed (default: 1)")
    parser.add_argument("--config", default="config.yaml",
                        help="Path to config file (default: config.yaml)")
    return parser.parse_args()
```

## Pipeline Flow

```python
def run_pipeline(config: PipelineConfig, args: argparse.Namespace) -> int:
    """Returns exit code:
    0 = success (or skipped: not a scheduled day, already processed)
    1 = unrecoverable error
    2 = retriable: no matching YouTube video yet
    3 = lock held by another process
    4 = git push failed (retriable)
    5 = retriable: paper URLs not yet in event description
    """

    # 0. Ensure directories exist
    Path("tmp").mkdir(exist_ok=True)
    Path("logs").mkdir(exist_ok=True)

    # 0.1. Acquire file lock
    try:
        lock = FileLock("tmp/pipeline.lock")
        lock.acquire(timeout=0)
    except Timeout:
        logger.warning("Lock held by another process")
        return 3

    try:
        # 0.5. Sync with remote (required — stale state causes duplicates)
        result = subprocess.run(["git", "pull", "--ff-only", "origin", "main"],
                                capture_output=True)
        if result.returncode != 0:
            logger.error("git pull failed (non-fast-forward?): %s",
                         result.stderr.decode())
            return 1  # cannot proceed on stale state

        # Validate: --video-url requires --paper-url
        if args.video_url and not args.paper_urls:
            logger.error("--video-url requires --paper-url")
            return 1

        # 1. Day-gating (skip if not a scheduled day, unless manual override)
        manual_override = args.paper_urls or args.force
        if not manual_override and not is_scheduled_day(config.schedule):
            return 0

        # 2. Scrape or use manual input
        if args.paper_urls:
            events = [build_manual_event(args.paper_urls, args.video_url)]
        else:
            events = scrape_events(config.luma, limit=args.backfill)

        # 3. Filter to unprocessed events
        state = load_state("processed.json")
        if not args.force:
            events = [e for e in events if not should_skip(e, state)]
        if not events:
            return 0

        # 4. Process each event (continue past retriable failures)
        # Severity order: 1 (unrecoverable) > 4 (git) > 5 (paper TBD) > 2 (video TBD)
        severity = {1: 4, 4: 3, 5: 2, 2: 1}
        worst_code = 0
        for event in events:
            exit_code = process_single_event(event, state, config)
            if exit_code == 1:  # unrecoverable: stop immediately
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


def process_single_event(event: PaperClubEvent, state: dict,
                         config: PipelineConfig) -> int:
    """Process a single event through the full pipeline."""
    slug = generate_slug(event.event_url, event.date)
    episode_path = Path(f"docs/episodes/{slug}.mp3")

    # Idempotent retry: episode exists locally but state/feed incomplete
    # (previous run generated MP3 but failed before full publish)
    if episode_path.exists():
        try:
            # publish_state_update syncs with remote first (git reset --hard),
            # then checks if episode needs to be re-staged/committed.
            # repair_feed is always True here — checked AFTER reset inside
            # the function (not before, since local state may differ from remote).
            publish_state_update(event, slug, state, config,
                                 repair_feed=True, episode_path=episode_path)
            return 0
        except PublishError:
            return 4

    # Check paper availability
    if not event.paper_urls:
        logger.warning("No paper URLs for event: %s", event.title)
        return 5  # paper TBD, retriable

    # Download content
    papers = download_papers(event.paper_urls, config)

    # Download video: use manual URL if provided, else search channel
    manual_video_url = getattr(event, "_manual_video_url", None)
    if manual_video_url:
        video = download_video_direct(manual_video_url)
    else:
        video = retry_with_backoff(
            lambda: find_and_download_video(event, config.youtube),
            max_retries=config.errors.max_retries,
            backoff_base=config.errors.backoff_base,
        )
    if video is None:
        return 2  # video not yet uploaded, retriable

    supplementary = download_supplementary(event.supplementary_urls, config.security)

    bundle = EpisodeBundle(
        event=event, paper_paths=papers, video=video,
        supplementary_paths=supplementary, episode_slug=slug,
    )

    # Generate podcast
    try:
        mp3_path = retry_with_backoff(
            lambda: generate_podcast_notebooklm(bundle, config),
            max_retries=config.errors.max_retries,
            backoff_base=config.errors.backoff_base,
        )
    except PodcastGenerationError:
        if not config.fallback.enabled:
            raise  # fallback disabled, propagate error
        logger.warning("NotebookLM failed, trying LLM+TTS fallback")
        mp3_path = generate_podcast_fallback(bundle, config)

    # Re-encode if too large BEFORE RSS update (so enclosure length matches)
    file_size = mp3_path.stat().st_size
    if file_size > 95_000_000:  # 95MB
        reencode_mp3(mp3_path, bitrate="auto")
        file_size = mp3_path.stat().st_size
        if file_size > 95_000_000:
            raise PublishError(f"MP3 still too large after re-encode: {file_size}")

    # Generate/update RSS feed (after reencode so metadata matches final file)
    update_rss_feed(mp3_path, event, slug, bundle.paper_paths, config.rss)

    # Publish (two-commit sequence)
    try:
        publish_episode(mp3_path, event, slug, state, config)
    except PublishError as e:
        logger.error("Publish failed: %s", e)
        return 4

    return 0
```

### build_manual_event

```python
def build_manual_event(paper_urls: list[str], video_url: str | None) -> PaperClubEvent:
    """Create a PaperClubEvent from manual CLI inputs.

    video_url is stored so the pipeline can download it directly
    instead of searching the channel.
    """
    canonical_urls = sorted(canonicalize_paper_url(u) for u in paper_urls)
    url_hash = hashlib.sha256(",".join(canonical_urls).encode()).hexdigest()[:12]
    event = PaperClubEvent(
        title="Manual Paper Club Entry",
        date=datetime.now(timezone.utc),
        event_url=f"https://manual.local/{url_hash}",
        paper_urls=canonical_urls,
        supplementary_urls=[],
    )
    event._manual_video_url = video_url  # carried through to download step
    return event
```

## Luma Scraping Strategy

### Approach: HTML parsing with JSON-LD fallback

```python
def scrape_events(config: LumaConfig, limit: int = 1) -> list[PaperClubEvent]:
    """Scrape lu.ma/ls for recent past Paper Club events.

    Returns up to `limit` unprocessed events, oldest first.
    """
    # 1. Validate calendar URL host (SSRF protection for configurable URL)
    cal_host = urlparse(config.calendar_url).hostname or ""
    if not (cal_host == "lu.ma" or cal_host.endswith(".lu.ma")):
        raise ConfigError(f"calendar_url must be on lu.ma, got: {cal_host}")

    # 2. Fetch calendar page HTML (use real UA to avoid bot-blocking)
    headers = {"User-Agent": "lspc-pipeline/1.0 (podcast automation)"}
    resp = requests.get(config.calendar_url, timeout=30, headers=headers)
    resp.raise_for_status()

    # 2. Try JSON-LD/embedded JSON first (more stable than HTML parsing)
    events = extract_events_from_json(resp.text, config.event_filter)
    if not events:
        # 3. Fallback: parse HTML event cards
        soup = BeautifulSoup(resp.text, "html.parser")
        events = extract_event_cards(soup, config.event_filter)

    if not events:
        raise NoEventsFoundError("No Paper Club events found on calendar page")

    # 4. Select past events, sorted oldest-first for backfill
    now = datetime.now(timezone.utc)
    past_events = sorted(
        [e for e in events if e.date < now],
        key=lambda e: e.date,
    )

    # 5. Take the N most recent (but process oldest first)
    candidates = past_events[-limit:]

    # 6. Fetch full event pages and extract URLs (validate host)
    enriched = []
    for event in candidates:
        ev_host = urlparse(event.event_url).hostname or ""
        if not (ev_host == "lu.ma" or ev_host.endswith(".lu.ma")):
            logger.warning("Skipping event with non-Luma URL: %s", event.event_url)
            continue
        event_page = requests.get(event.event_url, timeout=30, headers=headers)
        event_page.raise_for_status()
        event.paper_urls, event.supplementary_urls = extract_urls_from_description(
            event_page.text
        )
        enriched.append(event)

    return enriched


def extract_events_from_json(html: str, event_filter: str) -> list[PaperClubEvent]:
    """Extract events from embedded JSON/JSON-LD in Luma page.

    More stable than HTML parsing since it uses structured data.
    """
    soup = BeautifulSoup(html, "html.parser")
    # Look for Next.js __NEXT_DATA__ or JSON-LD script tags
    for script in soup.find_all("script", type=["application/json", "application/ld+json"]):
        try:
            data = json.loads(script.string)
            events = parse_luma_json_data(data, event_filter)
            if events:
                return events
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return []
```

```python
def parse_luma_json_data(data: dict, event_filter: str) -> list[PaperClubEvent]:
    """Extract events from Luma's embedded JSON/Next.js data.

    Navigates the JSON structure to find event objects, filtering by
    event_filter (e.g., "Paper Club") in the event title.
    Returns list of PaperClubEvent with title, date, event_url populated.
    paper_urls and supplementary_urls are populated later from individual event pages.
    Implementation depends on Luma's JSON structure (discovered during first scrape).

    All parsed datetimes MUST be timezone-aware. If the source data lacks
    timezone info, default to America/Los_Angeles (Paper Club is Pacific time).
    All comparisons (e.g., YouTube matching) normalize to UTC.
    Raise ScrapingError if date parsing fails.
    """
    ...  # Implementation discovered during development


def extract_event_cards(soup: BeautifulSoup, event_filter: str) -> list[PaperClubEvent]:
    """Fallback: parse HTML event cards from Luma calendar page.

    Finds event card elements, extracts title/date/URL from each.
    Filters to events containing event_filter in the title.
    """
    ...  # Implementation depends on Luma's HTML structure


def extract_urls_from_description(event_html: str) -> tuple[list[str], list[str]]:
    """Extract paper URLs and supplementary URLs from a Luma event page.

    Returns (paper_urls, supplementary_urls).
    paper_urls: URLs on arXiv, or PDFs on allowed domains.
    supplementary_urls: blog posts, research pages on allowed domains.
    Uses hostname-based classification (not substring matching).
    Only accepts http/https URLs; ignores mailto:, javascript:, etc.
    """
    soup = BeautifulSoup(event_html, "html.parser")
    seen = set()
    paper_urls = []
    supplementary_urls = []

    paper_domains = {"arxiv.org"}
    for link in soup.find_all("a", href=True):
        url = link["href"].strip()
        # Skip non-web schemes explicitly
        if url.startswith(("mailto:", "javascript:", "ftp:", "data:", "#", "/")):
            continue
        # Accept scheme-less URLs by adding https://
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if url in seen:
            continue
        seen.add(url)

        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if host == "arxiv.org" or host.endswith(".arxiv.org"):
            paper_urls.append(url)
        elif parsed.path.endswith(".pdf"):
            paper_urls.append(url)
        else:
            supplementary_urls.append(url)
    return paper_urls, supplementary_urls
```

**Fallback strategy** (in order):
1. Embedded JSON/JSON-LD extraction
2. HTML parsing with BeautifulSoup
3. If both fail: pipeline exits with code 1 + logs error; user uses `--paper-url` manual override

## YouTube Video Discovery

### Two-step approach with metadata return

```python
def find_and_download_video(event: PaperClubEvent,
                            config: YouTubeConfig) -> VideoMetadata | None:
    """Find matching Paper Club video and download audio.

    Returns VideoMetadata (including video_id and video_url for caption extraction)
    or None if no match found.
    """
    # Ensure tmp/ exists
    Path("tmp").mkdir(exist_ok=True)

    # Step 1: List channel videos with full metadata
    # Search both /videos and /streams tabs (Paper Club may be live-streamed)
    videos = []
    for tab in ["/videos", "/streams"]:
        result = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-download",
             "--playlist-end", str(config.playlist_depth),
             f"{config.channel_url}{tab}"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            logger.warning("yt-dlp discovery failed for %s (code %d)",
                           tab, result.returncode)
            continue
        for line in result.stdout.strip().split("\n"):
            if line:
                try:
                    videos.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    if not videos:
        raise YouTubeDiscoveryError("yt-dlp found no videos on any tab")

    # Step 2: Match by title + date (symmetric window) + title similarity
    candidates = []  # list of (video_dict, score, parsed_date)
    event_title_lower = event.title.lower()
    for v in videos:
        v_title = v.get("title", "").lower()
        if "paper club" not in v_title:
            continue
        upload_date = parse_yt_date(v.get("upload_date") or v.get("release_date"))
        if upload_date:
            delta_days = abs((upload_date - event.date.date()).days)
            if delta_days <= config.match_window_days:
                # Score: lower is better. Date proximity + title similarity bonus.
                title_sim = title_similarity(event_title_lower, v_title)
                score = delta_days - (title_sim * 3)  # bonus for title match
                candidates.append((v, score, upload_date))

    if not candidates:
        logger.warning("No matching video found for event: %s", event.title)
        return None

    # Sort by score (lower is better), then by upload date (newer wins on tie)
    best = min(candidates, key=lambda x: (x[1], -x[2].toordinal()))[0]

    # Step 3: Download audio
    output_template = f"tmp/%(id)s.%(ext)s"
    subprocess.run(
        ["yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "128K",
         "-o", output_template, best["webpage_url"]],
        check=True, timeout=600
    )

    # Find the actual output file (yt-dlp may produce different extension)
    audio_path = Path(f"tmp/{best['id']}.mp3")
    if not audio_path.exists():
        # Try finding any file with the video ID
        matches = list(Path("tmp").glob(f"{best['id']}.*"))
        if matches:
            audio_path = matches[0]
        else:
            raise VideoNotFoundError(f"Downloaded audio not found for {best['id']}")

    return VideoMetadata(
        video_id=best["id"],
        video_url=best["webpage_url"],
        title=best["title"],
        audio_path=audio_path,
    )
```

## Paper Download

```python
import time

def download_papers(paper_urls: list[str], config: PipelineConfig) -> list[Path]:
    """Download all paper PDFs. Returns list of paths."""
    Path("tmp").mkdir(exist_ok=True)
    paths = []
    for i, url in enumerate(paper_urls):
        if i > 0:
            time.sleep(3)  # arXiv rate limiting
        path = download_single_paper(url, config.security)
        paths.append(path)
    return paths


def _is_allowed_domain(netloc_or_hostname: str, allowed: list[str]) -> bool:
    """Check if hostname matches allowed domain list (exact or subdomain).

    Uses parsed.hostname (not netloc) to avoid port/userinfo bypass.
    """
    # Extract hostname if full netloc was passed
    parsed = urlparse(f"https://{netloc_or_hostname}")
    host = (parsed.hostname or netloc_or_hostname).lower().removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in allowed)


def download_single_paper(url: str, security: SecurityConfig) -> Path:
    """Download a single paper PDF."""
    canonical = canonicalize_paper_url(url)

    # Validate domain (papers also checked against allowlist)
    parsed = urlparse(canonical)
    if security.enforce_https and parsed.scheme != "https":
        raise PaperDownloadError(url, 0, "HTTPS required")
    if not _is_allowed_domain(parsed.netloc, security.allowed_domains):
        raise PaperDownloadError(url, 0, f"Domain not allowed: {parsed.netloc}")

    # Determine download URL and filename
    host = parsed.netloc.lower().removeprefix("www.")
    if host == "arxiv.org" or host.endswith(".arxiv.org"):
        arxiv_id = canonical.split("/abs/")[-1]
        download_url = f"https://arxiv.org/pdf/{arxiv_id}"
        filename = f"{arxiv_id}.pdf"
    else:
        download_url = canonical
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]
        filename = f"{url_hash}.pdf"

    output_path = Path(f"tmp/{filename}")
    resp = requests.get(download_url, timeout=30, stream=True,
                        allow_redirects=True, headers={"Accept": "application/pdf"})
    resp.raise_for_status()

    # Validate final URL after redirects (prevent allowlist + HTTPS bypass)
    final_parsed = urlparse(resp.url)
    if not _is_allowed_domain(final_parsed.netloc, security.allowed_domains):
        raise PaperDownloadError(url, 0, f"Redirect to disallowed domain: {final_parsed.netloc}")
    if security.enforce_https and final_parsed.scheme != "https":
        raise PaperDownloadError(url, 0, f"Redirect downgraded to {final_parsed.scheme}")

    # Validate content type (for arXiv, expect PDF; for direct URLs, accept any)
    content_type = resp.headers.get("content-type", "")
    if host == "arxiv.org" and "pdf" not in content_type.lower():
        raise PaperDownloadError(url, 0, f"Unexpected content type: {content_type}")

    # Stream with hard byte limit (don't trust Content-Length header alone)
    bytes_read = 0
    with open(output_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            bytes_read += len(chunk)
            if bytes_read > security.max_download_bytes:
                output_path.unlink(missing_ok=True)
                raise PaperDownloadError(url, 413,
                    f"Download exceeded {security.max_download_bytes} bytes")
            f.write(chunk)

    # Validate PDF magic bytes
    with open(output_path, "rb") as f:
        magic = f.read(5)
    if magic != b"%PDF-":
        output_path.unlink(missing_ok=True)
        raise PaperDownloadError(url, 0, "File is not a valid PDF")

    return output_path
```

## Supplementary Content Download

```python
def download_supplementary(urls: list[str], security: SecurityConfig) -> list[Path]:
    """Fetch supplementary content as text files. Best-effort."""
    Path("tmp").mkdir(exist_ok=True)
    paths = []
    for url in urls:
        try:
            parsed = urlparse(url)

            # Domain validation (exact match or subdomain)
            if not _is_allowed_domain(parsed.netloc, security.allowed_domains):
                logger.warning("Skipping URL (domain not allowed): %s", url)
                continue

            if security.enforce_https and parsed.scheme != "https":
                logger.warning("Skipping non-HTTPS URL: %s", url)
                continue

            # Pre-check: resolve redirects and validate final domain (optional)
            # HEAD may be blocked by some servers; on failure, skip to GET
            try:
                head_resp = requests.head(url, timeout=10, allow_redirects=True)
                final_host = urlparse(head_resp.url).netloc
                if not _is_allowed_domain(final_host, security.allowed_domains):
                    logger.warning("Redirect to disallowed domain: %s → %s", url, final_host)
                    continue
            except requests.RequestException:
                pass  # HEAD failed; GET will validate redirects below

            # Controlled fetch with byte cap (don't use trafilatura.fetch_url
            # which has no size limit)
            import trafilatura
            resp = requests.get(url, timeout=15, stream=True,
                                allow_redirects=True)
            resp.raise_for_status()

            # Re-validate final URL after GET redirects (HEAD can differ)
            get_final = urlparse(resp.url)
            if not _is_allowed_domain(get_final.netloc, security.allowed_domains):
                logger.warning("GET redirected to disallowed domain: %s", resp.url)
                continue
            if security.enforce_https and get_final.scheme != "https":
                logger.warning("GET redirected to non-HTTPS: %s", resp.url)
                continue
            # Enforce byte limit while streaming (separate cap for text content)
            content_parts = []
            bytes_read = 0
            for chunk in resp.iter_content(chunk_size=8192):
                bytes_read += len(chunk)
                if bytes_read > security.max_supplementary_bytes:
                    logger.warning("Supplementary URL too large, truncating: %s", url)
                    break
                content_parts.append(chunk)
            downloaded = b"".join(content_parts).decode("utf-8", errors="replace")
            if downloaded:
                text = trafilatura.extract(downloaded)
                if text:
                    url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]
                    out_path = Path(f"tmp/{url_hash}.txt")
                    out_path.write_text(text)
                    paths.append(out_path)
        except Exception as e:
            logger.warning("Failed to fetch supplementary URL %s: %s", url, e)
            continue
    return paths
```

## NotebookLM Integration

```python
def generate_podcast_notebooklm(bundle: EpisodeBundle, config: PipelineConfig) -> Path:
    """Primary podcast generation via notebooklm-py.

    Note: notebooklm-py requires browser-based Google auth.
    First-time setup: `notebooklm login` (interactive, requires display).
    Credentials are cached; cron runs use cached credentials.

    Auth expiry handling:
    - If credentials expire, pipeline catches the error as PodcastGenerationError
    - Falls through to LLM+TTS fallback automatically
    - Logs at ERROR level: "NotebookLM auth expired. Run `notebooklm login` to re-authenticate."
    - Creates a flag file `tmp/notebooklm_auth_expired` that cron_setup.sh
      can check to trigger a desktop notification
    - Service accounts are not supported by NotebookLM (unofficial API)
    """
    from notebooklm import NotebookLMClient

    try:
        client = NotebookLMClient.from_storage()
    except Exception as e:
        # Detect auth expiry specifically
        err_msg = str(e).lower()
        if "auth" in err_msg or "credential" in err_msg or "login" in err_msg:
            logger.error("NotebookLM auth expired. Run `notebooklm login` to re-authenticate.")
            Path("tmp/notebooklm_auth_expired").touch()
        raise PodcastGenerationError(f"NotebookLM client init failed: {e}") from e

    event = bundle.event
    nb = client.notebooks.create(
        f"LSPC: {event.title} ({event.date.strftime('%Y-%m-%d')})"
    )

    try:
        # Upload sources
        for paper_path in bundle.paper_paths:
            nb.sources.add_file(str(paper_path))
        if bundle.video:
            nb.sources.add_file(str(bundle.video.audio_path))
        for supp_path in bundle.supplementary_paths:
            nb.sources.add_text(supp_path.read_text())

        # Generate (with timeout to prevent indefinite hangs)
        # NOTE: Do NOT use ThreadPoolExecutor as a context manager here.
        # The context manager calls shutdown(wait=True), which blocks until
        # the worker thread finishes — defeating the timeout if the API hangs.
        import concurrent.futures
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            nb.artifacts.generate_audio,
            format=config.notebooklm.format,
            length=config.notebooklm.length,
            focus=config.notebooklm.prompt,
        )
        try:
            audio = future.result(timeout=1800)  # 30 min max
        except concurrent.futures.TimeoutError:
            raise PodcastGenerationError("NotebookLM generation timed out after 30 minutes")
        finally:
            executor.shutdown(wait=False)  # don't block on hung thread

        output_path = Path(f"docs/episodes/{bundle.episode_slug}.mp3")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        audio.download_audio(str(output_path))

        # Clear auth-expired flag on successful generation
        auth_flag = Path("tmp/notebooklm_auth_expired")
        if auth_flag.exists():
            auth_flag.unlink(missing_ok=True)

        return output_path
    finally:
        # Always cleanup: delete notebook to avoid clutter (even on failure)
        try:
            client.notebooks.delete(nb.id)
        except Exception:
            logger.warning("Failed to delete NotebookLM notebook: %s", nb.id)
```

## LLM+TTS Fallback

```python
def generate_podcast_fallback(bundle: EpisodeBundle, config: PipelineConfig) -> Path:
    """Fallback: LLM dialogue script + chunked OpenAI TTS."""
    # 0. Validate config
    if not config.fallback.tts_voices:
        raise FallbackConfigError(["tts_voices must contain at least one voice"])

    # 0.1 Check required env vars
    # OPENAI_API_KEY always required: TTS uses OpenAI regardless of LLM provider
    required_vars = ["OPENAI_API_KEY"]
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        raise FallbackConfigError(missing)

    # 1. Get transcript (using video URL, not audio path)
    transcript = ""
    if bundle.video:
        transcript = extract_youtube_captions(bundle.video.video_url)
        if not transcript:
            transcript = transcribe_with_whisper(bundle.video.audio_path)

    # 2. Extract paper text
    paper_text = extract_text_from_pdfs(bundle.paper_paths)

    # 3. Generate dialogue via LLM
    from litellm import completion
    dialogue = completion(
        model=config.fallback.llm_model,
        messages=[{
            "role": "system",
            "content": (
                "Create a two-speaker deep-dive podcast script between HOST and EXPERT. "
                "Format each line as 'HOST: ...' or 'EXPERT: ...'. "
                f"{config.notebooklm.prompt}"
            )
        }, {
            "role": "user",
            "content": f"Paper:\n{paper_text[:50000]}\n\nDiscussion transcript:\n{transcript[:30000]}"
        }]
    ).choices[0].message.content

    # 4. Chunked TTS (OpenAI limit is 4096 chars per call)
    from openai import OpenAI
    client = OpenAI()
    output_path = Path(f"docs/episodes/{bundle.episode_slug}.mp3")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Parse dialogue into speaker-tagged segments for two-voice rendering
    # LLM prompt produces format: "HOST: ..." and "EXPERT: ..."
    speaker_chunks = parse_speaker_chunks(dialogue, config.fallback.chunk_max_chars)
    audio_segments = []
    voices = config.fallback.tts_voices  # ["alloy", "echo"] — Host, Expert

    for i, (speaker, chunk) in enumerate(speaker_chunks):
        segment_path = Path(f"tmp/tts_segment_{i}.mp3")
        voice = voices[0] if speaker == "HOST" else voices[1] if len(voices) > 1 else voices[0]
        response = client.audio.speech.create(
            model=config.fallback.tts_model,
            voice=voice,
            input=chunk,
        )
        response.stream_to_file(str(segment_path))
        audio_segments.append(segment_path)

    # 5. Concatenate segments
    concatenate_audio_segments(audio_segments, output_path)

    logger.info("Generated episode via LLM+TTS fallback (%d segments)", len(audio_segments))
    return output_path


def chunk_dialogue(dialogue: str, max_chars: int = 4096) -> list[str]:
    """Split dialogue into chunks at sentence boundaries.

    Handles oversized sentences by force-splitting at max_chars.
    """
    sentences = re.split(r'(?<=[.!?])\s+', dialogue)
    chunks = []
    current = ""
    for sentence in sentences:
        # Force-split sentences that exceed max_chars on their own
        # First try splitting on word boundaries; fall back to hard char split
        if len(sentence) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            words = sentence.split()
            word_chunk = ""
            for word in words:
                if len(word_chunk) + len(word) + 1 > max_chars:
                    if word_chunk:
                        chunks.append(word_chunk.strip())
                    # Single word exceeds max_chars: hard-split as last resort
                    if len(word) > max_chars:
                        for i in range(0, len(word), max_chars):
                            chunks.append(word[i:i + max_chars])
                        word_chunk = ""
                    else:
                        word_chunk = word
                else:
                    word_chunk = f"{word_chunk} {word}" if word_chunk else word
            if word_chunk.strip():
                chunks.append(word_chunk.strip())
            continue
        if len(current) + len(sentence) + 1 > max_chars and current:
            chunks.append(current.strip())
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence
    if current.strip():
        chunks.append(current.strip())
    return chunks


def parse_speaker_chunks(dialogue: str, max_chars: int) -> list[tuple[str, str]]:
    """Parse two-speaker dialogue into (speaker, text) chunks.

    Expected LLM output format:
    HOST: Hello, welcome to...
    EXPERT: Thanks for having me...

    Each chunk is ≤ max_chars. Long turns are split at sentence boundaries.
    """
    segments = []
    current_speaker = "HOST"
    current_text = ""

    # Regex allows variations: "HOST:", "Host:", "**HOST**:", whitespace
    speaker_re = re.compile(r"^\s*\*{0,2}(HOST|EXPERT)\*{0,2}\s*:\s*", re.IGNORECASE)

    for line in dialogue.split("\n"):
        line = line.strip()
        if not line:
            continue
        match = speaker_re.match(line)
        if match:
            if current_text:
                segments.append((current_speaker, current_text.strip()))
            current_speaker = match.group(1).upper()
            current_text = line[match.end():].strip()
        else:
            current_text += " " + line

    if current_text.strip():
        segments.append((current_speaker, current_text.strip()))

    # Warn if LLM output didn't produce meaningful speaker variation
    unique_speakers = {s for s, _ in segments}
    if len(segments) > 0 and len(unique_speakers) < 2:
        logger.warning("LLM output has only %d speaker(s); expected HOST + EXPERT",
                       len(unique_speakers))

    # Sub-chunk long segments at sentence boundaries
    result = []
    for speaker, text in segments:
        if len(text) <= max_chars:
            result.append((speaker, text))
        else:
            for sub in chunk_dialogue(text, max_chars):
                result.append((speaker, sub))

    return result


def concatenate_audio_segments(segments: list[Path], output: Path):
    """Concatenate MP3 segments using ffmpeg."""
    list_file = Path("tmp/segments.txt")
    list_file.write_text("\n".join(f"file '{s.resolve()}'" for s in segments))
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", str(list_file), "-c", "copy", str(output)],
        check=True, timeout=120,
    )


def extract_youtube_captions(video_url: str) -> str:
    """Extract auto-captions from a YouTube video URL.

    Prefers human-uploaded captions over auto-generated.
    Strips VTT timestamps and markup to return plain text.
    """
    # Clean up any stale caption files from previous runs
    for stale in Path("tmp").glob("captions*.vtt"):
        stale.unlink(missing_ok=True)

    # Try human captions first, then auto-generated
    for sub_flag in ["--write-sub", "--write-auto-sub"]:
        result = subprocess.run(
            ["yt-dlp", sub_flag, "--sub-lang", "en",
             "--skip-download", "--sub-format", "vtt",
             "-o", "tmp/captions", video_url],
            capture_output=True, text=True, timeout=60,
        )
        # Look for any .vtt file produced
        vtt_files = list(Path("tmp").glob("captions*.vtt"))
        if vtt_files:
            raw_vtt = vtt_files[0].read_text()
            return strip_vtt_timestamps(raw_vtt)
    return ""


def strip_vtt_timestamps(vtt_text: str) -> str:
    """Strip VTT timestamps and markup, returning plain text."""
    lines = []
    for line in vtt_text.split("\n"):
        line = line.strip()
        # Skip WEBVTT header, timestamps, and empty lines
        if not line or line.startswith("WEBVTT") or "-->" in line:
            continue
        if re.match(r"^\d+$", line):  # sequence numbers
            continue
        # Strip HTML tags
        clean = re.sub(r"<[^>]+>", "", line)
        if clean and clean not in lines[-1:]:  # deduplicate adjacent lines
            lines.append(clean)
    return " ".join(lines)


def extract_text_from_pdfs(paper_paths: list[Path], max_chars: int = 100_000) -> str:
    """Extract text from PDFs using pymupdf.

    Stops extracting pages once max_chars is reached to avoid
    loading entire multi-hundred-page documents into memory.
    """
    import pymupdf
    texts = []
    total_chars = 0
    per_paper_budget = max_chars // max(len(paper_paths), 1)
    for path in paper_paths:
        doc = pymupdf.open(str(path))
        try:
            pages_text = []
            paper_chars = 0
            for page in doc:
                if paper_chars >= per_paper_budget or total_chars >= max_chars:
                    break
                page_text = page.get_text()
                pages_text.append(page_text)
                paper_chars += len(page_text)
                total_chars += len(page_text)
            texts.append("\n".join(pages_text))
        finally:
            doc.close()
        if total_chars >= max_chars:
            break
    return "\n\n---\n\n".join(texts)
```

## RSS Feed Generation

```python
import xml.etree.ElementTree as ET
from email.utils import format_datetime
import html

# Namespace registration (must happen before any XML operations)
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"
ET.register_namespace("itunes", ITUNES_NS)
ET.register_namespace("atom", ATOM_NS)


def update_rss_feed(mp3_path: Path, event: PaperClubEvent, slug: str,
                    paper_paths: list[Path], config: RSSConfig):
    """Generate/update docs/feed.xml with new episode."""
    feed_path = Path("docs/feed.xml")

    # Load existing feed or create new
    if feed_path.exists():
        tree = ET.parse(feed_path)
        channel = tree.getroot().find("channel")
    else:
        root, channel = create_feed_skeleton(config)
        tree = ET.ElementTree(root)

    # Dedup: check if episode already in feed
    for item in channel.findall("item"):
        guid = item.find("guid")
        if guid is not None and guid.text == slug:
            logger.info("Episode %s already in feed, skipping RSS update", slug)
            return

    # Build episode item
    item = build_episode_item(mp3_path, event, slug, paper_paths, config)

    # Update lastBuildDate on every write
    last_build = channel.find("lastBuildDate")
    if last_build is not None:
        last_build.text = format_datetime(datetime.now(timezone.utc))

    # Prepend (newest first) — insert after last channel-level element that isn't an item
    non_item_count = sum(1 for c in channel if c.tag != "item")
    channel.insert(non_item_count, item)

    # Atomic write: write to temp file then replace
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(tree, space="  ")
    tmp_feed = feed_path.with_suffix(".xml.tmp")
    tree.write(str(tmp_feed), encoding="utf-8", xml_declaration=True)
    tmp_feed.replace(feed_path)


def feed_contains_guid(slug: str) -> bool:
    """Check if feed.xml already contains an episode with this GUID."""
    feed_path = Path("docs/feed.xml")
    if not feed_path.exists():
        return False
    tree = ET.parse(feed_path)
    for item in tree.getroot().iter("item"):
        guid = item.find("guid")
        if guid is not None and guid.text == slug:
            return True
    return False


def update_rss_feed_for_existing(mp3_path: Path, event: PaperClubEvent,
                                  slug: str, config: PipelineConfig):
    """Add an existing episode to the feed (idempotent retry case).

    Re-downloads papers for full-quality description, then updates feed.
    feed.xml is staged by publish_state_update (after git reset).
    """
    # Re-download papers for description quality
    try:
        paper_paths = download_papers(event.paper_urls, config)
    except (PaperDownloadError, Exception):
        logger.warning("Could not re-download papers on retry; RSS description may be incomplete")
        paper_paths = []

    update_rss_feed(mp3_path, event, slug, paper_paths, config.rss)
```

### Feed skeleton and episode item builders

```python
def create_feed_skeleton(config: RSSConfig) -> tuple[ET.Element, ET.Element]:
    """Create a new RSS feed with channel metadata."""
    root = ET.Element("rss", version="2.0")
    root.set("xmlns:itunes", ITUNES_NS)
    root.set("xmlns:atom", ATOM_NS)

    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = config.title
    ET.SubElement(channel, "link").text = config.base_url
    ET.SubElement(channel, "description").text = config.description or config.title
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    ET.SubElement(channel, f"{{{ITUNES_NS}}}author").text = config.author
    ET.SubElement(channel, f"{{{ITUNES_NS}}}image").set("href", f"{config.base_url}/cover.jpg")

    # Apple Podcasts required tags
    owner = ET.SubElement(channel, f"{{{ITUNES_NS}}}owner")
    ET.SubElement(owner, f"{{{ITUNES_NS}}}name").text = config.owner_name
    ET.SubElement(owner, f"{{{ITUNES_NS}}}email").text = config.owner_email
    category = ET.SubElement(channel, f"{{{ITUNES_NS}}}category")
    category.set("text", config.category)
    ET.SubElement(category, f"{{{ITUNES_NS}}}category").set("text", config.subcategory)
    ET.SubElement(channel, f"{{{ITUNES_NS}}}explicit").text = str(config.explicit).lower()

    atom_link = ET.SubElement(channel, f"{{{ATOM_NS}}}link")
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")
    atom_link.set("href", f"{config.base_url}/feed.xml")

    return root, channel


def build_episode_item(mp3_path: Path, event: PaperClubEvent, slug: str,
                       paper_paths: list[Path], config: RSSConfig) -> ET.Element:
    """Build an RSS <item> element for a single episode."""
    from mutagen.mp3 import MP3

    item = ET.Element("item")
    date_str = event.date.strftime("%Y-%m-%d")
    ET.SubElement(item, "title").text = f"{event.title} ({date_str})"
    ET.SubElement(item, "description").text = get_episode_description(event, paper_paths)

    guid = ET.SubElement(item, "guid")
    guid.set("isPermaLink", "false")
    guid.text = slug

    # Use current time as pubDate (not event date) so backfilled episodes
    # appear as new in podcast clients
    ET.SubElement(item, "pubDate").text = format_datetime(datetime.now(timezone.utc))

    enclosure = ET.SubElement(item, "enclosure")
    enclosure.set("url", f"{config.base_url}/episodes/{slug}.mp3")
    enclosure.set("type", "audio/mpeg")
    enclosure.set("length", str(mp3_path.stat().st_size))

    # Duration from MP3 metadata
    audio_info = MP3(str(mp3_path)).info
    duration_secs = int(audio_info.length)
    h, m, s = duration_secs // 3600, (duration_secs % 3600) // 60, duration_secs % 60
    ET.SubElement(item, f"{{{ITUNES_NS}}}duration").text = f"{h:02d}:{m:02d}:{s:02d}"
    ET.SubElement(item, f"{{{ITUNES_NS}}}summary").text = get_episode_description(event, paper_paths)

    return item
```

### Episode description extraction

```python
def get_episode_description(event: PaperClubEvent, paper_paths: list[Path]) -> str:
    """Get abstract/description for RSS <description> field."""
    description_parts = []

    for url in event.paper_urls:
        canonical = canonicalize_paper_url(url)
        if "arxiv.org" in canonical:
            # Fetch abstract via arxiv API (best-effort, don't block publishing)
            try:
                import arxiv
                paper_id = canonical.split("/abs/")[-1]
                results = list(arxiv.Client().results(arxiv.Search(id_list=[paper_id])))
                if results:
                    description_parts.append(results[0].summary)
                else:
                    description_parts.append("Abstract unavailable.")
            except Exception:
                logger.warning("arXiv API failed for %s, using fallback description", canonical)
                description_parts.append("Abstract unavailable.")
        else:
            # Extract first 500 chars from PDF via pymupdf
            try:
                import pymupdf
                pdf_path = find_pdf_for_url(url, paper_paths)
                if pdf_path:
                    doc = pymupdf.open(str(pdf_path))
                    text = doc[0].get_text()[:500]
                    description_parts.append(text if len(text) >= 50 else "No abstract available.")
            except Exception:
                description_parts.append("No abstract available.")

    # Append links
    links = [f"Paper: {url}" for url in event.paper_urls]
    description_parts.extend(links)

    # Return raw text; ElementTree handles XML escaping during serialization
    return "\n\n".join(description_parts)
```

### Required podcast tags

Each episode `<item>` must include:
- `<title>`: event title + date
- `<description>`: plain text abstract + links (ElementTree handles XML escaping)
- `<pubDate>`: RFC 2822 format via `email.utils.format_datetime()` — uses publish time (not event date)
- `<guid isPermaLink="false">`: episode slug
- `<enclosure url="..." type="audio/mpeg" length="...">`: file size in bytes
- `<itunes:duration>`: HH:MM:SS via `mutagen.mp3.MP3.info.length`
- `<itunes:summary>`: same as description

Channel-level required tags (Apple Podcasts compliant):
- `<title>`, `<description>`, `<language>en</language>`
- `<itunes:author>`, `<itunes:owner>` (with `<itunes:email>`)
- `<itunes:image href="{base_url}/cover.jpg">` — **Cover image**: a 3000x3000 JPEG stored at `docs/cover.jpg`, created once during initial setup. Simple branded image with podcast title.
- `<itunes:category text="Technology">` with subcategory `<itunes:category text="Tech News"/>`
- `<itunes:explicit>false</itunes:explicit>`
- `<atom:link rel="self" type="application/rss+xml" href="{base_url}/feed.xml">`

## MP3 Re-encoding

```python
def reencode_mp3(mp3_path: Path, bitrate: str = "96k"):
    """Re-encode MP3 to lower bitrate using ffmpeg. Preserves metadata.

    If bitrate is "auto", calculates target bitrate to fit under 90MB.
    """
    if bitrate == "auto":
        from mutagen.mp3 import MP3
        duration_secs = MP3(str(mp3_path)).info.length
        if duration_secs > 0:
            # Target 90MB with safety margin
            target_kbps = int((90_000_000 * 8) / duration_secs / 1000)
            bitrate = f"{max(32, min(target_kbps, 128))}k"
        else:
            bitrate = "96k"

    tmp_output = mp3_path.with_suffix(".reencoded.mp3")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(mp3_path),
         "-b:a", bitrate, "-map_metadata", "0", str(tmp_output)],
        check=True, timeout=300,
    )
    tmp_output.replace(mp3_path)
```

## Git Publishing (Two-Commit Sequence)

```python
def publish_episode(mp3_path: Path, event: PaperClubEvent, slug: str,
                    state: dict, config: PipelineConfig):
    """Two-commit publish: episode first, then state update.

    Raises PublishError on failure (never calls sys.exit).
    """
    date_str = event.date.strftime("%Y-%m-%d")

    # NOTE: MP3 re-encoding (if needed) happens in process_single_event
    # BEFORE RSS update, so enclosure length/duration match the final file.

    # Step 1-3: Stage and commit episode
    try:
        subprocess.run(["git", "add", str(mp3_path), "docs/feed.xml"], check=True)
        # Guard: only commit if there are staged changes
        has_changes = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], capture_output=True
        ).returncode != 0
        if not has_changes:
            logger.info("No staged changes, skipping episode commit")
            # Still proceed to state update (don't return early)
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
    result = subprocess.run(["git", "push", "origin", "main"],
                            capture_output=True, timeout=120, env=env)
    if result.returncode != 0:
        logger.error("Push failed: %s", result.stderr.decode())
        subprocess.run(["git", "reset", "--hard", "HEAD~1"], check=True)
        raise PublishError("Episode push failed")

    # Step 5: Update state and push (no feed repair needed, feed already committed)
    publish_state_update(event, slug, state, config, repair_feed=False)


def publish_state_update(event: PaperClubEvent, slug: str,
                         state: dict, config: PipelineConfig,
                         repair_feed: bool = False,
                         episode_path: Path | None = None):
    """Commit and push the processed.json state update.

    If this fails, the episode is already live. Next run detects
    the existing episode file and retries this step (idempotent).

    If repair_feed=True, checks AFTER reset whether the feed needs repair
    (local state before reset may differ from remote).

    If episode_path is provided, ensures the MP3 is staged and committed
    (handles the case where a previous run generated but never pushed it).
    """
    try:
        # Sync with remote to avoid non-fast-forward. Uses fetch+reset instead
        # of pull --rebase to avoid interactive conflict resolution in cron.
        # Abort if working tree has modified tracked files (cron should use a clean clone)
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True).stdout.strip()
        if dirty:
            raise PublishError(
                f"Cannot update state: dirty working tree. "
                f"Manual intervention required. Files:\n{dirty}"
            )

        subprocess.run(["git", "fetch", "origin", "main"], check=True,
                       timeout=60)
        subprocess.run(["git", "reset", "--hard", "origin/main"], check=True)

        # Reload state from disk after reset (remote may have newer entries)
        state = load_state("processed.json")

        # Check feed repair AFTER reset (not before, since local != remote)
        if repair_feed and not feed_contains_guid(slug):
            ep_path = episode_path or Path(f"docs/episodes/{slug}.mp3")
            update_rss_feed_for_existing(ep_path, event, slug, config)

        # If episode file exists locally but wasn't pushed, stage it
        if episode_path and episode_path.exists():
            subprocess.run(["git", "add", str(episode_path)], check=True)

        update_processed_json(event, slug, state)
        subprocess.run(["git", "add", "processed.json", "docs/feed.xml"],
                       check=True)
        subprocess.run(
            ["git", "commit", "-m", f"Mark processed: {event.title}"],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise PublishError(f"State commit failed: {e}") from e

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(["git", "push", "origin", "main"],
                            capture_output=True, timeout=120, env=env)
    if result.returncode != 0:
        logger.error("State push failed: %s", result.stderr.decode())
        subprocess.run(["git", "reset", "--hard", "HEAD~1"], check=True)
        raise PublishError("State update push failed (episode is live, will retry)")
```

## Error Handling

### Exception Hierarchy

```python
class LSPCError(Exception):
    """Base exception for all pipeline errors."""

class ConfigError(LSPCError):
    """Invalid or missing configuration."""

class ScrapingError(LSPCError):
    """Failed to scrape Luma events."""

class NoEventsFoundError(ScrapingError):
    """No matching Paper Club events found."""

class PaperDownloadError(LSPCError):
    """Failed to download a paper PDF."""
    def __init__(self, url: str, status_code: int, message: str):
        self.url = url
        self.status_code = status_code
        super().__init__(f"Failed to download {url}: {status_code} {message}")

class YouTubeDiscoveryError(LSPCError):
    """yt-dlp failed to list channel videos."""

class VideoNotFoundError(LSPCError):
    """No matching YouTube video found."""

class PodcastGenerationError(LSPCError):
    """NotebookLM podcast generation failed."""

class FallbackConfigError(LSPCError):
    """Missing env vars for fallback pipeline."""
    def __init__(self, missing_vars: list[str]):
        self.missing_vars = missing_vars
        super().__init__(f"Missing env vars: {', '.join(missing_vars)}")

class PublishError(LSPCError):
    """Git push failed."""
```

### Retry Strategy

```python
import time

def retry_with_backoff(func, max_retries: int, backoff_base: int):
    """Retry a function with exponential backoff.

    Used by: video discovery, NotebookLM generation.
    NOT used by: paper download (has own rate limiting),
    supplementary download (best-effort, no retry).

    Catches transient exceptions (network timeouts, HTTP errors, video/podcast
    generation failures). Config/validation errors propagate immediately.
    """
    retryable = (
        VideoNotFoundError, YouTubeDiscoveryError, PodcastGenerationError,
        requests.RequestException, json.JSONDecodeError,
        subprocess.TimeoutExpired,
    )
    for attempt in range(max_retries):
        try:
            return func()
        except retryable as e:
            if attempt == max_retries - 1:
                raise
            wait = backoff_base * (2 ** attempt)
            logger.warning("Attempt %d failed (%s): %s. Retrying in %ds",
                           attempt + 1, type(e).__name__, e, wait)
            time.sleep(wait)
```

## File Lock

```python
from filelock import FileLock, Timeout

# Usage in pipeline:
# lock = FileLock("tmp/pipeline.lock")
# lock.acquire(timeout=0)  # raises Timeout if held
# ... pipeline runs ...
# lock.release()  # in finally block
```

Uses the `filelock` package (cross-platform, works on WSL + Windows drives). The lock is held for the entire pipeline run and released in the `finally` block of `run_pipeline()`.

## Logging

```python
# Setup in pipeline.py
def setup_logging():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = Path(f"logs/run_{timestamp}.log")
    log_path.parent.mkdir(exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(),
        ]
    )

    # Runtime secret redaction via custom Formatter (not Filter).
    # Filters run before formatting, so exc_text generated during format()
    # would bypass a Filter. A custom Formatter redacts the final output.
    class SecretRedactFormatter(logging.Formatter):
        patterns = [re.compile(p) for p in [
            r"sk-[a-zA-Z0-9_-]{20,}",      # OpenAI API keys (incl. sk-proj-)
            r"AIza[a-zA-Z0-9_-]{35}",       # Google API keys
            r"ghp_[a-zA-Z0-9]{36}",         # GitHub PATs
        ]]
        def format(self, record):
            output = super().format(record)
            for pat in self.patterns:
                output = pat.sub("[REDACTED]", output)
            return output

    fmt = "%(asctime)s %(name)s %(levelname)s %(message)s"
    redact_formatter = SecretRedactFormatter(fmt)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(redact_formatter)

    logger = logging.getLogger("lspc")
    return logger
```

### Logger Hierarchy

- `lspc` — root pipeline logger
- `lspc.scraper` — Luma scraping
- `lspc.papers` — paper download
- `lspc.youtube` — video discovery + download
- `lspc.supplementary` — blog/thread fetching
- `lspc.podcast` — NotebookLM + fallback
- `lspc.rss` — feed generation
- `lspc.publisher` — git operations
- `lspc.state` — processed.json I/O

## Security

- **Data egress**: NotebookLM (primary) uploads paper PDFs, audio, and supplementary text to Google servers. LLM+TTS fallback (when enabled) sends paper text and transcripts to the LLM provider (default: OpenAI via litellm) and audio generation to OpenAI TTS. Set `fallback.enabled: false` to prevent data from being sent to LLM/TTS providers. Both paths send content to third-party services by design.
- **Google auth**: Managed by `notebooklm-py` credential store (browser-based login, stored locally). Path added to `.gitignore`.
- **API keys**: `OPENAI_API_KEY` for fallback pipeline, read from environment. Never logged.
- **Git auth**: SSH keys for GitHub push. No credentials in code or config.
- **`.gitignore`**: `tmp/`, `logs/`, `.env`, `*.credential`, `processed.json.tmp`, `docs/feed.xml.tmp`, NotebookLM auth paths
- **Log scrubbing**: Custom `SecretRedactFormatter` redacts patterns matching API keys (`sk-...`, `AIza...`, `ghp_...`) in the final formatted output (including exception stack traces). Applied to all handlers. Test assertions verify no secrets in sample log output.
- **SSRF mitigation**: Both paper and supplementary URL fetching restricted to `config.security.allowed_domains`. Domain checks use exact match or subdomain match (`host == d or host.endswith("." + d)`) to prevent bypass via `notgithub.com`. Redirect chains are validated: final URL domain is re-checked after redirects resolve. All downloads enforce HTTPS by default. Maximum download size enforced via streaming byte counting (not Content-Length header alone).
- **PDF parsing**: Uses `pymupdf` which runs in-process; text extraction only (no JavaScript execution). Download size capped. PDF magic bytes (`%PDF-`) validated after download.
- **Content-type validation**: arXiv paper downloads verify `Content-Type` contains `pdf`. Non-arXiv direct URLs accept any content type but validate PDF magic bytes.

## Testing Strategy

### Unit Tests (mocked dependencies)

| Test File | What it tests | Mocking strategy |
|-----------|--------------|-----------------|
| `tests/test_scraper.py` | Luma HTML parsing, JSON extraction, URL extraction | Static HTML fixtures |
| `tests/test_papers.py` | arXiv URL conversion, download logic, domain validation | `responses` library for HTTP |
| `tests/test_youtube.py` | Video matching algorithm, symmetric window, yt-dlp errors | JSON fixtures for yt-dlp output |
| `tests/test_supplementary.py` | Blog text extraction, domain allowlist, error handling | `responses` + static HTML |
| `tests/test_podcast_gen.py` | NotebookLM client calls, notebook cleanup | Mocked `NotebookLMClient` |
| `tests/test_fallback.py` | LLM+TTS pipeline, chunking, concatenation | Mocked `litellm`, `openai` |
| `tests/test_rss.py` | RSS XML generation, feedparser validation, dedup | File fixtures |
| `tests/test_publish.py` | Git commit/push/reset sequence, PublishError | Temp git repo with bare remote |
| `tests/test_state.py` | processed.json I/O, deduplication, event_url keying | Temp files |
| `tests/test_config.py` | YAML loading, validation, defaults, security config | Temp YAML files |
| `tests/test_pipeline.py` | Orchestration, exit codes, retry, backfill, lock | All modules mocked |

### Integration Tests (gated)

- Run with `RUN_INTEGRATION=1`
- Test real NotebookLM auth + generation
- Test real yt-dlp download
- Test real Luma scraping
- Require manual setup (Google auth, network)

## Deployment

### GitHub Pages Setup

1. Repository: `lspc-to-nblm`
2. Settings → Pages → Source: `main` branch, `/docs` directory
3. Feed URL: `https://{username}.github.io/lspc-to-nblm/feed.xml`

### Cron Setup

```bash
# cron_setup.sh outputs this line for user to confirm:
0 8 * * * cd /home/mgrim/projects/lspc-to-nblm && /home/mgrim/.local/bin/python -m src.pipeline >> logs/cron.log 2>&1
```

**Daily at 08:00 local time.** By default, the pipeline runs every day and exits quickly (code 0) when no unprocessed events exist. The `run_days` config can restrict runs to specific days if desired. Daily execution ensures late-arriving papers/videos are caught promptly without manual retries.

**WSL2 cron requirements:**
- WSL must have systemd enabled (Ubuntu 22.04+ with `[boot] systemd=true` in `/etc/wsl.conf`)
- Or use Windows Task Scheduler: `schtasks /create /sc daily /st 08:00 /tn "lspc-pipeline" /tr "wsl -d Ubuntu -e bash -lc 'cd ~/projects/lspc-to-nblm && python -m src.pipeline'"`
- Environment variables (`OPENAI_API_KEY`) must be set in the cron environment. **Preferred**: use a wrapper script that sources a chmod-600 `.env` file, or use `EnvironmentFile=` in a systemd unit. **Avoid** embedding API keys directly in crontab lines (visible to other users via `crontab -l`, may leak in backups/audits). Note: cron does NOT source `~/.bashrc`
- **Cron should use a dedicated clean clone** (not the dev checkout) to avoid dirty-tree conflicts during publishing

**Exit code semantics for monitoring:**
- `0`: success or no work to do (normal)
- `1`: unrecoverable error (alert immediately)
- `2`: video not yet uploaded (expected, retry tomorrow — alert only after 5 consecutive)
- `3`: lock held (another instance running, rare)
- `4`: git push failed (transient, retry next run)
- `5`: paper URL missing from event (expected for new events, retry — alert after 5 consecutive)

### Dependencies (requirements.txt)

```
notebooklm-py[browser]
yt-dlp
arxiv
beautifulsoup4
requests
trafilatura
mutagen
pymupdf
pyyaml
litellm
openai
filelock
feedparser  # for test validation
responses   # for test HTTP mocking
pytest
```

## Scalability Note: MP3 Storage

Episodes are committed as binary files to `docs/episodes/` in the git repo. This is acceptable for MVP — typical NotebookLM deep-dive episodes are 10-20 minutes at 128kbps (~15MB each, ~780MB/year). The re-encoding logic targets 90MB as a *ceiling* (only triggers for files >95MB), not a typical size; most episodes will never hit this. When the repo exceeds 1GB, migrate MP3 hosting to Cloudflare R2, S3, or GitHub Release assets, and update `feed.xml` enclosure URLs accordingly. The RSS feed XML itself remains in git.

## Open Questions

1. GitHub username for Pages URL configuration
2. Exact Luma calendar page HTML structure (may need adjustment after first scrape attempt)
3. `notebooklm-py` API stability and exact method signatures (may need adaptation)
4. NotebookLM credential expiry interval (determines how often manual re-login is needed)
5. Old-style arXiv IDs (e.g., `hep-th/9901001`) are not supported by the canonicalization regex. Latent Space Paper Club discusses modern papers so this is unlikely to matter, but can be added if needed.
6. Whether NotebookLM accepts uploaded audio files as sources (may need to upload transcript text instead)
