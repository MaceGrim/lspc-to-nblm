# Latent Space Paper Club Podcast Pipeline — PRD

## Executive Summary

An automated pipeline that transforms Latent Space Paper Club sessions into personalized deep-dive podcasts. Each week, the system scrapes Luma event pages for the latest Paper Club session, downloads the discussed paper(s) and YouTube video, feeds them into NotebookLM to generate a single podcast episode per event, and publishes it to a personal RSS feed hosted on GitHub Pages. The user subscribes to the RSS feed URL in Apple Podcasts via "Add Show by URL" — no directory submission is needed.

This is a personal-use tool. The GitHub Pages site is technically public, but the feed is not submitted to any podcast directory. The generated audio is derivative content for private educational consumption.

## Problem Statement

The Latent Space Paper Club discusses important AI/ML papers weekly, but keeping up requires either attending live (Wednesday 12pm PT) or watching hour-long YouTube recordings. There's no condensed, audio-first format that deeply explains the paper while incorporating the community discussion. Mason wants to absorb these papers during commutes or walks, with explanations tailored to his data science background.

## Target Users

**Primary user: Mason Grimshaw**
- Data scientist / AI engineer
- Strong technical background (MIT, 7+ years experience)
- Wants deep understanding, not surface summaries
- Listens to podcasts on Apple Podcasts during commutes (via "Add Show by URL")
- Uses WSL (Linux on Windows) as development environment

## Key Design Decision: One Episode Per Event

Each Paper Club event produces exactly **one podcast episode**. If an event covers multiple papers, all papers are uploaded as sources to the same NotebookLM notebook. Episode titles, slugs, and state tracking are keyed by the **event** (not individual papers).

## User Stories

### US-001: Scrape Luma Event for Paper Club Session
**As a** user **I want to** automatically discover the latest Paper Club event from Luma **so that** I don't have to manually find the paper URL each week.

**Acceptance Criteria:**
- [ ] Script fetches the Luma calendar page at `https://lu.ma/ls`
- [ ] Filters events containing "Paper Club" (case-insensitive) in the title
- [ ] Selects the most recent past event: parses event datetime as timezone-aware (assume PT if Luma provides no timezone), converts to UTC for comparison against `datetime.now(timezone.utc)`
- [ ] Parses the event description to extract URLs, classified as:
  - **Paper URLs**: URLs matching `arxiv.org` or ending in `.pdf`
  - **Supplementary URLs**: URLs matching `anthropic.com/research`, `openai.com/research`, any URL containing `/blog/`, or other non-paper links. These are treated as text sources (fetched and extracted as text via US-004), not PDFs.
- [ ] URL canonicalization: arXiv URLs are normalized to `https://arxiv.org/abs/{id}` form (stripping `/pdf/`, version suffixes like `v1`, query params, and fragments) before comparison or storage
- [ ] Returns a Python dataclass:
  ```python
  @dataclass
  class PaperClubEvent:
      title: str           # Event title from Luma
      date: datetime       # Timezone-aware datetime (original TZ preserved)
      event_url: str       # Luma event page URL (stable identifier)
      paper_urls: list[str]       # arXiv or direct PDF URLs only
      supplementary_urls: list[str]  # Blog posts, research pages, threads
  ```
- [ ] If no paper URLs found in description, returns event with `paper_urls=[]` and logs at WARNING level via `logging.getLogger("lspc.scraper")`

**Verification:**
```bash
python -m pytest tests/test_scraper.py -v
```

**Priority:** P0

---

### US-002: Download Paper PDF
**As a** user **I want to** automatically download the paper PDF from arXiv or a direct URL **so that** it can be fed into NotebookLM.

**Acceptance Criteria:**
- [ ] Given an arXiv URL (e.g., `https://arxiv.org/abs/2501.12345`), converts to PDF URL (`https://arxiv.org/pdf/2501.12345`) and downloads
- [ ] Given a direct PDF URL, downloads the file
- [ ] Saves PDF to `tmp/` directory with filename format: `{arxiv_id}.pdf` or `{url_hash[:12]}.pdf` for non-arXiv
- [ ] Returns the `pathlib.Path` of the downloaded PDF
- [ ] Raises `PaperDownloadError(url, status_code, message)` on failure (404, timeout >30s, etc.)
- [ ] Respects arXiv rate limiting (3-second delay between requests)
- [ ] For events with multiple papers, downloads all of them and returns `list[pathlib.Path]`

**Verification:**
```bash
python -m pytest tests/test_papers.py -v
```

**Priority:** P0

---

### US-003: Find and Download YouTube Video Audio
**As a** user **I want to** find the matching Paper Club video on YouTube and download its audio **so that** NotebookLM can use the discussion as context.

**Acceptance Criteria:**
- [ ] Two-step discovery: (1) list videos from @LatentSpaceTV channel using `yt-dlp --dump-json` (without `--flat-playlist`, to get full metadata), (2) match against event
- [ ] Matching algorithm: video title contains "Paper Club" (case-insensitive) AND `upload_date` (for regular uploads) or `release_date` (for livestreams) is within `config.youtube.match_window_days` (default 7) days after the Luma event date. If multiple match, selects closest by date. Dates compared as timezone-aware UTC.
- [ ] Downloads audio-only using `yt-dlp -x --audio-format mp3 --audio-quality 128K`
- [ ] Saves to `tmp/` with filename format: `{video_id}.mp3`
- [ ] Returns `pathlib.Path` of downloaded audio, or `None` if no matching video found
- [ ] Logs at WARNING level via `logging.getLogger("lspc.youtube")` if no match found

**Verification:**
```bash
python -m pytest tests/test_youtube.py -v
```

**Priority:** P0

---

### US-004: Download Supplementary Content
**As a** user **I want to** grab any blog posts or threads linked in the event description **so that** NotebookLM has additional context.

**Acceptance Criteria:**
- [ ] Given a list of non-paper URLs from the event description, fetches text content
- [ ] Uses `trafilatura` to extract main article text from blog posts (strips navigation, ads, boilerplate)
- [ ] Twitter/X URLs: best-effort via Nitter instances or ThreadReaderApp URLs. If scraping fails, logs WARNING and skips (not a hard failure).
- [ ] Saves each as `{url_hash[:12]}.txt` in `tmp/`
- [ ] Returns `list[pathlib.Path]` of successfully fetched files
- [ ] URLs that return HTTP errors or time out (>15s) are logged at WARNING and skipped

**Verification:**
```bash
python -m pytest tests/test_supplementary.py -v
```

**Priority:** P1

---

### US-005: Generate Podcast via NotebookLM
**As a** user **I want to** feed the paper(s), video audio, and supplementary content into NotebookLM **so that** it generates a deep-dive podcast episode.

**Acceptance Criteria:**
- [ ] Creates a NotebookLM notebook titled `"LSPC: {event_title} ({date})"`
- [ ] Uploads all paper PDFs as sources (supports multiple papers per event)
- [ ] Uploads video audio file as a source
- [ ] Uploads supplementary text files as sources (if any exist)
- [ ] Calls `generate_audio()` with `format=config.notebooklm.format` (default: `"deep-dive"`) and `length=config.notebooklm.length` (default: `"standard"`)
- [ ] Sets episode focus to the prompt string from `config.yaml` `notebooklm.prompt`
- [ ] Episode slug = `{date_YYYYMMDD}-{hashlib.sha256(event_url).hexdigest()[:8]}` (stable, derived from event URL, not title). A human-readable title is stored in RSS metadata and `processed.json`, not in the filename.
- [ ] Downloads generated MP3 to `docs/episodes/{episode_slug}.mp3`
- [ ] Returns `pathlib.Path` of the downloaded MP3
- [ ] Raises `PodcastGenerationError` if NotebookLM fails after the configured retry count

**Testing strategy:**
- Unit tests use mocked `NotebookLMClient` with golden request/response payloads
- Integration tests gated behind `RUN_INTEGRATION=1` env var (requires real Google auth)

**Verification:**
```bash
python -m pytest tests/test_podcast_gen.py -v
```

**Priority:** P0

---

### US-006: LLM+TTS Podcast Fallback
**As a** user **I want to** fall back to an LLM+TTS podcast generation pipeline **so that** I still get episodes if notebooklm-py breaks.

**Acceptance Criteria:**
- [ ] Extracts transcript from YouTube auto-captions via `yt-dlp --write-auto-sub --sub-lang en --skip-download`; if unavailable, uses `whisper` (base model) for transcription
- [ ] Feeds paper text + transcript into an LLM via `litellm` (configurable model in `config.yaml` `fallback.llm_model`, default: `gpt-4o-mini`) to generate a two-speaker dialogue script
- [ ] Dialogue prompt instructs the LLM to deeply explain the paper (same focus as the NotebookLM prompt)
- [ ] Converts dialogue to audio via `openai` TTS API (`tts-1` model, configurable in `config.yaml` `fallback.tts_model`)
- [ ] Required env vars: `OPENAI_API_KEY` (or model-specific key via litellm). If missing, raises `FallbackConfigError` listing the required env vars.
- [ ] Saves MP3 to `docs/episodes/{episode_slug}.mp3` (same slug derivation as US-005: `{date_YYYYMMDD}-{hash[:8]}`)
- [ ] Returns `pathlib.Path` of the MP3
- [ ] Is triggered automatically when US-005 raises `PodcastGenerationError`
- [ ] Logs at INFO level that fallback pipeline was used

**Note:** This fallback uses paid APIs (OpenAI). "LLM+TTS" is the accurate description — it is not open-source.

**Verification:**
```bash
python -m pytest tests/test_fallback.py -v
```

**Priority:** P1

---

### US-007: Generate and Update RSS Feed
**As a** user **I want to** generate a valid podcast RSS feed from the episodes directory **so that** Apple Podcasts can sync new episodes.

**Acceptance Criteria:**
- [ ] Generates RSS 2.0 XML with `xmlns:itunes` and `xmlns:atom` namespaces
- [ ] Each episode `<item>` has:
  - `<title>`: event title + date
  - `<description>`: For arXiv papers, abstract fetched via `arxiv` Python API (`arxiv.Search(id_list=[...])` → `result.summary`). For non-arXiv PDFs, extract text via `pymupdf` (first page), take first 500 characters. If extraction fails or yields <50 chars, use `"No abstract available."` Append links to paper(s) and YouTube video. All text HTML-escaped.
  - `<pubDate>`: Luma event date in RFC 2822 format
  - `<guid isPermaLink="false">`: episode slug
  - `<enclosure>`: MP3 URL (constructed from `config.rss.base_url` + `/episodes/{slug}.mp3`), `type="audio/mpeg"`, `length` = file size in bytes
  - `<itunes:duration>`: HH:MM:SS computed via `mutagen.mp3.MP3.info.length`
- [ ] Feed `<channel>` includes: title, description, author, language, `<itunes:image>`, `<atom:link rel="self" href="{base_url}/feed.xml">`
- [ ] Feed validates using `feedparser.parse()` with `bozo == 0` in test
- [ ] Episodes ordered by pubDate descending (newest first)
- [ ] Writes feed to `docs/feed.xml`
- [ ] Existing episodes are preserved when adding new ones

**Verification:**
```bash
python -m pytest tests/test_rss.py -v
```

**Priority:** P0

---

### US-008: Deduplication and State Tracking
**As a** user **I want to** track which episodes have been processed **so that** the same Paper Club session isn't processed twice.

**Acceptance Criteria:**
- [ ] Maintains `processed.json` in repo root: a JSON object mapping Luma event URLs (canonicalized) to:
  ```json
  {
    "title": "event title",
    "date": "2026-03-05T19:00:00-08:00",
    "paper_urls": ["https://arxiv.org/abs/2501.12345"],
    "episode_slug": "20260305-a1b2c3d4",
    "episode_file": "docs/episodes/20260305-a1b2c3d4.mp3",
    "processed_at": "2026-03-06T08:15:00-07:00"
  }
  ```
  All timestamps are full ISO 8601 with timezone offset.
- [ ] An event is marked processed **only after the episode is successfully pushed** (US-010 step 5). The `processed.json` update is a separate commit after the episode push, ensuring the working tree is always clean on failure via `git reset --hard HEAD~1` (see US-010 publish sequence).
- [ ] Before processing, checks if the event URL is a key in `processed.json`:
  - Paper URLs are canonicalized (arXiv normalized to `abs/` form, fragments/query stripped) before comparison using set equality
  - If key exists AND the canonicalized live-scraped `paper_urls` set equals the stored set → skip
  - If key exists BUT the live-scraped set contains URLs not in the stored set → re-process (updated event description)
  - If key does not exist → process normally
- [ ] If an event has `paper_urls=[]` (paper TBD), the pipeline does NOT add it to `processed.json` — it exits with code 2 (retriable)

**Verification:**
```bash
python -m pytest tests/test_state.py -v
```

**Priority:** P0

---

### US-009: Pipeline Orchestration
**As a** user **I want to** run the full pipeline with a single command **so that** everything happens end-to-end.

**Acceptance Criteria:**
- [ ] `python -m src.pipeline` runs the full pipeline
- [ ] `python -m src.pipeline --paper-url URL [--paper-url URL2 ...] --video-url URL` accepts manual overrides (skips Luma scraping and YouTube matching). `--paper-url` is repeatable for multi-paper events.
- [ ] `python -m src.pipeline --force` re-processes even if event is in `processed.json`
- [ ] Pipeline steps execute in order: scrape → download papers → download video → download supplementary → generate podcast → update RSS → publish
- [ ] Skips events per US-008 deduplication logic
- [ ] On failure at any step, retries up to `config.errors.max_retries` (default 3) times with exponential backoff (base = `config.errors.backoff_base`, default 60s)
- [ ] If video not found on first run, exits with code 2 (retriable) — cron retries on subsequent days
- [ ] Logs all actions to `logs/run_{YYYYMMDD_HHMMSS}.log` at INFO level, errors at ERROR level, via `logging.getLogger("lspc")`
- [ ] No secrets appear in log output (credential paths, API keys are never logged)
- [ ] Acquires a file lock (`tmp/pipeline.lock`) to prevent overlapping runs; exits with code 3 if lock held
- [ ] Cleans up `tmp/` directory after successful completion (deletes downloaded PDFs, audio files)

**Verification:**
```bash
python -m pytest tests/test_pipeline.py -v
```

**Priority:** P0

---

### US-010: GitHub Pages Publishing
**As a** user **I want to** automatically push new episodes to GitHub Pages **so that** the RSS feed updates without manual intervention.

**Acceptance Criteria:**
- [ ] **Deployment model**: GitHub Pages serves from `main` branch `/docs` directory
- [ ] **Publish sequence** (order matters):
  1. Write MP3 to `docs/episodes/{slug}.mp3` and generate `docs/feed.xml`
  2. Stage episode files: `git add docs/episodes/{slug}.mp3 docs/feed.xml`
  3. Commit: `"Add episode: {event_title} ({date})"`
  4. Push to `origin main`
  5. **Only on successful push**: update `processed.json` with the event entry, `git add processed.json`, commit `"Mark processed: {event_title}"`, push again
- [ ] If push fails at step 4: run `git reset --hard HEAD~1` to fully revert the commit and all working tree changes, log ERROR, exit with code 4 (retriable). Next run starts clean.
- [ ] If push fails at step 5 (state update): run `git reset --hard HEAD~1` to drop the local processed.json commit (the episode commit is already pushed and safe). Next run will see the event is not in `processed.json`, detect the episode file already exists in `docs/episodes/`, and skip directly to step 5 (idempotent retry).
- [ ] Before commit, checks MP3 file size < 95MB; if larger, re-encodes to 96kbps and re-checks
- [ ] Credential files, `tmp/`, and `.env` are in `.gitignore`

**Testing strategy:**
- Unit tests use a temp git repo with a local bare remote
- Manual smoke test documented for real push

**Verification:**
```bash
python -m pytest tests/test_publish.py -v
```

**Priority:** P0

---

### US-011: Cron Job Setup
**As a** user **I want to** set up a scheduled cron job on WSL **so that** the pipeline runs automatically.

**Acceptance Criteria:**
- [ ] `cron_setup.sh` prints the exact crontab line(s) to stdout and prompts user to confirm before adding
- [ ] Schedule: cron runs **daily** at a configurable time (default 08:00 local time). The pipeline itself checks `config.schedule.run_days` (default: `["thursday", "friday", "saturday"]`) and exits immediately with code 0 if today is not a scheduled day.
- [ ] Cron entry uses absolute paths to Python interpreter and project directory
- [ ] Script detects if entry already exists and skips duplicate installation
- [ ] Outputs the installed cron line for verification

**Verification:**
```bash
bash cron_setup.sh --dry-run
```

**Priority:** P1

---

### US-012: Configuration File
**As a** user **I want to** configure the pipeline via a YAML file **so that** I can adjust settings without editing code.

**Acceptance Criteria:**
- [ ] `config.yaml` contains all settings:
  - `luma.calendar_url`, `luma.event_filter`
  - `youtube.channel_url`, `youtube.match_window_days`
  - `notebooklm.prompt`, `notebooklm.format`, `notebooklm.length`
  - `fallback.llm_model`, `fallback.tts_model`
  - `rss.title`, `rss.description`, `rss.author`, `rss.base_url`
  - `errors.max_retries`, `errors.backoff_base`
  - `schedule.run_days`, `schedule.time`
- [ ] Pipeline loads config at startup via `src/config.py` which returns a typed dataclass
- [ ] Missing `config.yaml` raises `FileNotFoundError` with message pointing to `config.example.yaml`
- [ ] A `config.example.yaml` is committed to the repo with all fields documented
- [ ] Invalid YAML or missing required fields raises `ConfigError` with the field name and expected type

**Verification:**
```bash
python -m pytest tests/test_config.py -v
```

**Priority:** P1

---

## Functional Requirements

1. **Content Discovery**: Automatically find Paper Club events and extract paper/video references (US-001)
2. **Content Download**: Download paper PDFs, video audio, and supplementary materials (US-002, US-003, US-004)
3. **Podcast Generation**: Create deep-dive podcast episodes via NotebookLM with LLM+TTS fallback (US-005, US-006)
4. **Distribution**: Publish to RSS feed on GitHub Pages from `main` branch `/docs` (US-007, US-010)
5. **Automation**: Run daily via cron with internal day-gating and retry logic (US-009, US-011)
6. **State Management**: Track processed episodes; only mark done after successful publish (US-008)
7. **Manual Override**: Accept explicit paper/video URLs for manual runs, supporting multiple papers (US-009)

## Non-Functional Requirements

- **Reliability**: Pipeline handles transient failures via retry with exponential backoff; file lock prevents overlapping runs
- **Maintainability**: Modular code structure — each pipeline stage is independently testable with mocked dependencies
- **Storage**: MP3s compressed to fit under GitHub's 100MB/file limit; repo growth acknowledged — migrate to R2/S3 if repo exceeds 1GB
- **Logging**: Every run produces a timestamped log file; loggers follow `lspc.*` hierarchy; no secrets in log output
- **Security**: Credentials stored by notebooklm-py's credential system, never committed; `tmp/`, credential paths, and `.env` in `.gitignore`; log output scanned for accidental secret leakage (regex test for API key patterns)
- **Privacy**: Feed is publicly accessible on GitHub Pages but not submitted to directories; generated audio is for personal educational use
- **Timezone handling**: All datetimes are timezone-aware throughout the pipeline; event times parsed with original timezone, compared in UTC

## Success Metrics

- Pipeline produces a new podcast episode within 48 hours of each Paper Club session
- Episodes appear in Apple Podcasts (via RSS URL subscription) without manual intervention
- Fallback pipeline activates automatically if NotebookLM is unavailable

## Scope

### In Scope
- Luma scraping for Paper Club events
- Paper PDF download (arXiv + direct URLs), supporting multiple papers per event
- YouTube video audio download with two-step metadata fetch
- Blog post / supplementary content download (best-effort)
- NotebookLM podcast generation + LLM+TTS fallback
- RSS feed generation and GitHub Pages hosting (main branch /docs)
- Daily cron scheduling on WSL with internal day-gating
- Retry logic, logging, deduplication
- Manual override mode (repeatable `--paper-url`, `--video-url`)

### Out of Scope
- Discord integration
- Luma paid API
- Multi-user support
- Web UI or dashboard
- Podcast directory submission (Spotify, Apple directory)
- Episode editing or post-processing
- Mobile app
- Private/authenticated RSS feed

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| notebooklm-py breaks (unofficial API) | High — no podcast generation | LLM+TTS fallback pipeline (US-006) with specified models + env var requirements |
| Luma page structure changes | Medium — can't discover papers | Log error + manual override mode (`--paper-url`) |
| YouTube video delayed | Low — missing one episode | Retry over multiple days via daily cron schedule |
| GitHub Pages 100MB/file limit | Low — large MP3 | Pre-commit size check + re-encode to 96kbps if needed |
| GitHub repo growth over time | Medium — eventual size issues | Monitor size; migrate episodes to R2/S3 when repo > 1GB |
| Google auth expires | Medium — notebooklm-py fails | Re-login documented; fallback pipeline activates |
| Paper not on arXiv | Low — can't auto-download | Direct PDF URL support from event description |
| Twitter/X scraping fails | Low — missing supplementary context | Best-effort only; pipeline continues without it |
| WSL cron unreliable (machine off) | Medium — missed episodes | Daily cron + manual catch-up via CLI; consider Windows Task Scheduler |
| Legal/copyright concerns | Low — personal use | Feed not submitted to directories; "personal educational use" documented |
| Fallback API keys missing | Medium — fallback also fails | `FallbackConfigError` with clear message listing required env vars |

## Open Questions

1. GitHub username for Pages URL?
2. Notification preference beyond log files? (desktop notification, email, etc.)
3. Daily cron time preference + timezone? (currently 08:00 local)
