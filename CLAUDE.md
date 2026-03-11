# Project: lspc-to-nblm

Automated podcast generation from Latent Space Paper Club events using NotebookLM.

## Key Paths

- `src/` - Core library modules
- `test_scripts/` - Runnable scripts (e2e test, podcast generation, batch generation)
- `docs/` - GitHub Pages root (episodes, feed.xml, cover.png)
- `config.yaml` - Runtime config (gitignored, has API keys and email)
- `config.example.yaml` - Template config (safe to commit)

## NotebookLM Authentication

NotebookLM CLI (`notebooklm-py`) uses Playwright browser storage state for auth. The auth expires quickly (~10 min) for the CLI's headless context.

### How to refresh auth:

1. Navigate MCP Playwright browser to `https://notebooklm.google.com`
2. Confirm logged in as Mason (check account button in snapshot)
3. Save storage state:
```js
// Run via mcp playwright browser_run_code
async (page) => {
  const context = page.context();
  await context.storageState({
    path: '/home/mgrim/.claude-profiles/personal/.notebooklm/storage_state.json'
  });
  return 'saved';
}
```
4. Must refresh before EACH NotebookLM CLI operation (auth expires fast)
5. During batch generation, refresh every 5-8 minutes while waiting

### NotebookLM CLI patterns:
- Create notebook: `notebooklm create "Title" --json`
- Notebook ID is at `result["notebook"]["id"]` (not top-level)
- Set active: `notebooklm use NOTEBOOK_ID`
- Add source: `notebooklm source add URL -n NOTEBOOK_ID`
- Generate: `notebooklm generate audio PROMPT -n ID --format deep-dive --length default --no-wait --json`
- Wait: `notebooklm artifact wait TASK_ID -n ID --timeout 600 --json`
- Download: `notebooklm download audio PATH -n ID --latest --force`
- Delete: `notebooklm delete -n NOTEBOOK_ID -y`

### NotebookLM outputs M4A, not MP3
Despite file extension, NotebookLM downloads MPEG-4/M4A audio. Must convert with ffmpeg:
```bash
ffmpeg -y -i input.mp3 -map 0:a -map_metadata -1 \
  -codec:a libmp3lame -ar 44100 -b:a 128k -id3v2_version 3 output.mp3
```

## Spotify/RSS Podcast Requirements

- MP3 must be **MPEG-1 Layer 3** (not MPEG-2) — requires 44.1kHz sample rate
- Bitrate: 96-320 kbps CBR
- edge-tts outputs 24kHz (MPEG-2) — must resample to 44.1kHz
- Strip non-MP3 metadata (M4A container tags cause rejection)
- Cover art: 1400x1400 to 3000x3000 px, JPG or PNG

## YouTube Video Matching

Video-event matching uses Gemini Flash to verify correctness:
- `_list_channel_videos()` fetches Paper Club videos from YouTube (two-phase: flat-playlist then individual metadata)
- Title similarity alone is unreliable (many videos uploaded same day, similar generic titles)
- LLM verification prompt asks Gemini to match event title + paper URLs against candidate video titles
- Returns "NONE" when no match exists (paper-only episode)

## Luma Calendar API

- Calendar URL `lu.ma/ls` resolves to API ID via `/url?url=ls` endpoint
- API ID can change over time (was `cal-cVMwMTiMj4lYFBL`, now `cal-mc9ZW5C3TzHDv6L`)
- The scraper resolves it dynamically each run
- `pagination_limit` controls how many events returned (default 20)
- Many events lack paper URLs in the Luma description

## Running Tests

```bash
pytest tests/ -v  # 298 unit tests, all mocked
python test_scripts/e2e_test.py  # Real API calls
```

## Generating Episodes

### Single episode (most recent match):
```bash
python test_scripts/generate_podcast_nblm.py
```

### Batch (all available):
```bash
python test_scripts/batch_generate_nblm.py
```

### After generating, update RSS and push:
Episodes go to `docs/episodes/SLUG.mp3`. RSS feed at `docs/feed.xml`.
GitHub Pages serves from `docs/` on main branch.

## Environment Variables

- `GEMINI_API_KEY` - Required for LLM video matching and fallback dialogue generation
- No `OPENAI_API_KEY` needed (NotebookLM handles generation)
