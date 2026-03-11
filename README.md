# lspc-to-nblm

Automated podcast generation from [Latent Space Paper Club](https://lu.ma/ls) events. Scrapes paper club events, downloads papers and YouTube discussion videos, then generates deep-dive podcast episodes via Google NotebookLM.

## How it works

1. **Scrape** Luma calendar API for Paper Club events
2. **Download** papers (arXiv, GitHub) and match YouTube discussion videos
3. **Verify** video-event matches using Gemini Flash (avoids wrong context)
4. **Generate** podcast audio via NotebookLM (paper + video + supplementary sources)
5. **Convert** to Spotify-compatible MP3 (MPEG-1, 44.1kHz, 128kbps CBR)
6. **Publish** RSS feed to GitHub Pages for automatic distribution

## Listen

**RSS Feed:** [https://macegrim.github.io/lspc-to-nblm/feed.xml](https://macegrim.github.io/lspc-to-nblm/feed.xml)

Add to your podcast app of choice.

## Setup

```bash
pip install -r requirements.txt

# Required
export GEMINI_API_KEY="..."

# Copy and edit config
cp config.example.yaml config.yaml
```

NotebookLM requires browser authentication:
```bash
notebooklm login
```

## Usage

### End-to-end test
```bash
python test_scripts/e2e_test.py
```

### Generate single episode (NotebookLM)
```bash
python test_scripts/generate_podcast_nblm.py
```

### Batch generate all available episodes
```bash
python test_scripts/batch_generate_nblm.py
```

### Generate episode (Gemini + edge-tts fallback)
```bash
python test_scripts/generate_podcast.py
```

## Architecture

```
src/
  scraper.py        # Luma API calendar scraping
  youtube.py        # yt-dlp video discovery + audio download
  papers.py         # Paper PDF download with security validation
  podcast.py        # Episode slug generation, content bundling
  fallback.py       # Gemini dialogue generation + TTS
  rss.py            # RSS/iTunes podcast feed generation
  config.py         # YAML config loading
  state.py          # Processed event tracking
  pipeline.py       # Full pipeline orchestration
```

## Tests

```bash
pytest tests/ -v
```
