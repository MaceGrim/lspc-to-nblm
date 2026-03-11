# SPEC.md — Latent Space Paper Club → NotebookLM Podcast Pipeline

## Project Overview

Automated pipeline that monitors Latent Space's weekly Paper Club sessions, downloads the discussed paper and video, generates a personalized deep-dive podcast via NotebookLM, and publishes it to a personal RSS feed consumable by Apple Podcasts.

## Pipeline Summary

```
Luma Events (weekly scrape)
  → Extract paper URL(s) + event metadata
  → Find matching YouTube video on @LatentSpaceTV
  → Download: paper PDF, video audio, any linked blog posts
  → Feed into NotebookLM (notebooklm-py) with deep-explanation prompt
  → Download generated podcast MP3
  → Update RSS feed XML
  → Push to GitHub Pages
  → Apple Podcasts auto-syncs
```

## Data Sources

### Luma Event Pages
- **Calendar**: https://lu.ma/ls (Latent Space events)
- **Paper Club format**: Events with "Paper Club" in title, Wednesdays 12pm PT
- **Paper links**: Embedded as URLs in event description text (arXiv links, blog posts, etc.)
- **Scraping approach**: Parse public event pages (no Luma API key — that requires paid Luma Plus)
- **Fallback**: iCal feed subscription may also contain description text with links

### YouTube Videos
- **Channel**: @LatentSpaceTV (https://www.youtube.com/@LatentSpaceTV)
- **Video naming pattern**: `{Paper Title} — Paper Club {Date}` or similar
- **Discovery method**: Use `yt-dlp --flat-playlist` to list channel videos, match by title keywords and date proximity to Luma events
- **Download**: `yt-dlp` for full audio extraction

### Supplementary Content
- Paper's arXiv abstract page
- Any blog posts or threads linked in the Luma event description
- Author blog posts if easily discoverable

## NotebookLM Podcast Generation

### Primary: notebooklm-py (unofficial API)
- **Package**: `pip install "notebooklm-py[browser]"`
- **Auth**: One-time browser login via `notebooklm login`, stores credentials
- **Sources fed to NotebookLM**:
  1. Paper PDF (uploaded directly)
  2. Video audio (full ~1hr audio uploaded as source)
  3. Blog posts / supplementary text (if available)
- **Podcast format**: `deep-dive`, standard length
- **Output**: MP3 download

### Fallback: Open-source pipeline (open-notebooklm)
- **Trigger**: If notebooklm-py fails (API changes, auth issues, etc.)
- **Approach**: LLM generates dialogue script from paper + transcript, TTS generates audio
- **Quality**: Lower than NotebookLM but functional
- **Dependencies**: Together AI or similar LLM API + TTS service

### Personalization Prompt (Fixed)
Baked-in prompt for all episodes, something like:

> "Deeply explain this paper's core ideas, methodology, and key results. Focus on building intuition for WHY the approach works, not just WHAT it does. Explain mathematical concepts in accessible terms. Discuss practical implications and how this connects to the broader field. The listener is a data scientist with a strong technical background who wants to truly understand the paper."

This prompt is stored in a config file and used for every episode.

## Output & Distribution

### RSS Feed
- **Format**: Standard podcast RSS 2.0 with iTunes namespace
- **Hosting**: GitHub Pages (free, this repo)
- **Feed URL**: `https://{username}.github.io/lspc-to-nblm/feed.xml`
- **Episodes directory**: `docs/episodes/` (GitHub Pages serves from `docs/`)
- **Cover art**: Simple branded image for the feed

### Podcast Metadata per Episode
- **Title**: Paper title + date (e.g., "Recursive Language Models — Paper Club Jan 28, 2026")
- **Description**: Paper abstract + links to original paper and YouTube video
- **Publication date**: Date the podcast was generated
- **Duration**: Whatever NotebookLM produces (~10-20 min typical)

### Consumer
- Apple Podcasts via custom RSS URL

## Scheduling & Automation

### Cron Job
- **Runs on**: Mason's WSL machine
- **Schedule**: Weekly, day after Paper Club (Thursday) to allow YouTube upload time
- **What it does**:
  1. Check Luma for most recent Paper Club event
  2. Check if already processed (track in `processed.json`)
  3. Download paper + video + supplementary content
  4. Generate podcast via notebooklm-py
  5. Generate/update RSS feed XML
  6. Git commit + push to GitHub Pages
  7. Clean up temp files

### State Tracking
- `processed.json`: List of processed event IDs/dates to avoid duplicates
- Stored in repo root (committed)

## Error Handling

### Strategy: Retry with backoff, then notify
- **Retries**: Up to 3 attempts with exponential backoff
- **Notification on failure**: Write to a log file + optionally send a notification (email, desktop notification, or simple flag file)
- **Partial failures**:
  - If video not yet uploaded: Retry next day (Paper Club videos sometimes take 1-2 days to appear)
  - If paper URL not in Luma description: Log warning, skip episode
  - If notebooklm-py fails: Try fallback open-source pipeline
  - If fallback also fails: Log error, notify, move on

### Logging
- All runs logged to `logs/` directory with timestamps
- Errors include full stack traces

## Project Structure

```
lspc-to-nblm/
├── SPEC.md
├── README.md
├── requirements.txt
├── config.yaml              # Prompt, URLs, schedule settings
├── processed.json           # Track which episodes are done
├── src/
│   ├── __init__.py
│   ├── scraper.py           # Luma event scraping
│   ├── youtube.py           # YouTube video discovery + download
│   ├── papers.py            # Paper PDF download (arxiv, etc.)
│   ├── podcast_gen.py       # NotebookLM integration + fallback
│   ├── rss.py               # RSS feed generation
│   └── pipeline.py          # Orchestrates the full pipeline
├── docs/                    # GitHub Pages root
│   ├── feed.xml             # RSS feed
│   ├── cover.jpg            # Podcast cover art
│   └── episodes/            # MP3 files
├── logs/                    # Run logs
├── test_scripts/            # One-off test scripts
└── cron_setup.sh            # Cron installation helper
```

## Configuration (config.yaml)

```yaml
luma:
  calendar_url: "https://lu.ma/ls"
  event_filter: "Paper Club"

youtube:
  channel: "@LatentSpaceTV"
  channel_url: "https://www.youtube.com/@LatentSpaceTV"

notebooklm:
  prompt: |
    Deeply explain this paper's core ideas, methodology, and key results.
    Focus on building intuition for WHY the approach works, not just WHAT it does.
    Explain mathematical concepts in accessible terms.
    Discuss practical implications and how this connects to the broader field.
    The listener is a data scientist with a strong technical background
    who wants to truly understand the paper.
  format: "deep-dive"
  length: "standard"

rss:
  title: "Latent Space Paper Club Deep Dives"
  description: "AI-generated deep-dive podcasts on papers discussed at the Latent Space Paper Club"
  base_url: "https://{username}.github.io/lspc-to-nblm"
  author: "Mason Grimshaw"

schedule:
  day: "thursday"
  time: "08:00"
  retry_days: [1, 2]  # Retry Friday, Saturday if Thursday fails

errors:
  max_retries: 3
  backoff_base: 60  # seconds
  notify: "log"  # "log", "email", or "desktop"
```

## Dependencies

- **Python 3.10+**
- `notebooklm-py[browser]` — NotebookLM unofficial API
- `yt-dlp` — YouTube video/audio download
- `arxiv` — arXiv paper search/download
- `beautifulsoup4` + `requests` — Luma page scraping
- `mutagen` — MP3 metadata for RSS generation
- `pyyaml` — Config file parsing
- `playwright` — Required by notebooklm-py for browser auth

## Scope

### In Scope (MVP)
- Scrape latest Paper Club event from Luma
- Download paper PDF from arXiv (or direct URL)
- Download YouTube video audio
- Grab linked blog posts as text
- Generate podcast via notebooklm-py
- Publish to GitHub Pages RSS feed
- Weekly cron on WSL
- Retry logic with logging
- Deduplication via processed.json

### Out of Scope
- Discord bot or Discord API integration
- Luma paid API
- Multi-user support
- Web UI or dashboard
- Spotify/Apple directory submission (just custom RSS URL)
- Episode editing or post-processing
- Automatic paper summarization without NotebookLM
- Mobile app

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| notebooklm-py breaks (Google changes internals) | No podcast generation | Fallback to open-source pipeline |
| Luma page structure changes | Can't find paper URLs | Alert + manual fallback mode |
| YouTube video not uploaded in time | Missing video source | Retry over multiple days |
| GitHub Pages file size limits (100MB/file) | Can't host large MP3s | Compress MP3s to 128kbps (keeps files under 20MB for ~20min episodes) |
| Google account auth expires | notebooklm-py fails | Re-login flow, retry |
| Paper not on arXiv | Can't auto-download PDF | Support direct URL download from event description |

## Open Questions

- What GitHub username to use for the Pages URL?
- Should notification be just log files, or also a desktop notification/email?
- Exact cron time preference (currently spec'd as Thursday 8am)?
