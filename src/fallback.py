"""LLM+TTS fallback pipeline for podcast generation.

Used when NotebookLM (primary) fails with PodcastGenerationError.
Generates a two-speaker dialogue via LLM, then synthesizes audio
via OpenAI TTS with two voices.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

from src.config import FallbackConfig, PipelineConfig
from src.errors import FallbackConfigError
from src.podcast import ContentBundle, extract_text_from_pdfs
from src.scraper import PaperClubEvent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Text chunking
# ---------------------------------------------------------------------------


def chunk_text(text: str, max_chars: int = 4096) -> list[str]:
    """Split text into chunks at sentence boundaries.

    Long sentences are split at word boundaries.
    Very long words are hard-split as a last resort.
    """
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    current = ""

    for sentence in sentences:
        # Force-split sentences that exceed max_chars on their own
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
                            chunks.append(word[i : i + max_chars])
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


# ---------------------------------------------------------------------------
# Speaker dialogue parsing
# ---------------------------------------------------------------------------


def parse_speaker_chunks(
    dialogue: str, max_chars: int
) -> list[tuple[str, str]]:
    """Parse two-speaker dialogue into (speaker, text) chunks.

    Expected LLM output format:
    HOST: Hello, welcome to...
    EXPERT: Thanks for having me...

    Each chunk is <= max_chars. Long turns are split at sentence boundaries.
    """
    segments: list[tuple[str, str]] = []
    current_speaker = "HOST"
    current_text = ""

    # Regex allows variations: "HOST:", "Host:", "**HOST**:", whitespace
    speaker_re = re.compile(
        r"^\s*\*{0,2}(HOST|EXPERT)\*{0,2}\s*:\s*", re.IGNORECASE
    )

    for line in dialogue.split("\n"):
        line = line.strip()
        if not line:
            continue
        match = speaker_re.match(line)
        if match:
            if current_text:
                segments.append((current_speaker, current_text.strip()))
            current_speaker = match.group(1).upper()
            current_text = line[match.end() :].strip()
        else:
            current_text += " " + line

    if current_text.strip():
        segments.append((current_speaker, current_text.strip()))

    # Warn if LLM output didn't produce meaningful speaker variation
    unique_speakers = {s for s, _ in segments}
    if len(segments) > 0 and len(unique_speakers) < 2:
        logger.warning(
            "LLM output has only %d speaker(s); expected HOST + EXPERT",
            len(unique_speakers),
        )

    # Sub-chunk long segments at sentence boundaries
    result: list[tuple[str, str]] = []
    for speaker, text in segments:
        if len(text) <= max_chars:
            result.append((speaker, text))
        else:
            for sub in chunk_text(text, max_chars):
                result.append((speaker, sub))

    return result


# ---------------------------------------------------------------------------
# VTT caption processing
# ---------------------------------------------------------------------------


def strip_vtt_timestamps(vtt_text: str) -> str:
    """Strip VTT timestamps and markup, returning plain text."""
    lines: list[str] = []
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


# ---------------------------------------------------------------------------
# Transcript extraction
# ---------------------------------------------------------------------------


def get_transcript(video_url: str | None, audio_path: Path | None, config: FallbackConfig) -> str:
    """Get transcript from YouTube captions or Whisper transcription.

    Tries YouTube captions first (yt-dlp --write-sub, then --write-auto-sub).
    Falls back to Whisper transcription if captions are unavailable.

    Parameters
    ----------
    video_url : str | None
        YouTube video URL to extract captions from.
    audio_path : Path | None
        Path to audio file for Whisper transcription fallback.
    config : FallbackConfig
        Fallback configuration settings.

    Returns
    -------
    str
        Transcript text, or empty string if no transcript available.
    """
    if video_url:
        transcript = _extract_youtube_captions(video_url)
        if transcript:
            return transcript

    # Fall back to Whisper if we have audio
    if audio_path and audio_path.exists():
        return _transcribe_with_whisper(audio_path)

    return ""


def _extract_youtube_captions(video_url: str) -> str:
    """Extract auto-captions from a YouTube video URL.

    Prefers human-uploaded captions over auto-generated.
    Strips VTT timestamps and markup to return plain text.
    """
    # Clean up any stale caption files from previous runs
    for stale in Path("tmp").glob("captions*.vtt"):
        stale.unlink(missing_ok=True)

    Path("tmp").mkdir(exist_ok=True)

    # Try human captions first, then auto-generated
    for sub_flag in ["--write-sub", "--write-auto-sub"]:
        result = subprocess.run(
            [
                "yt-dlp",
                sub_flag,
                "--sub-lang",
                "en",
                "--skip-download",
                "--sub-format",
                "vtt",
                "-o",
                "tmp/captions",
                video_url,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        # Look for any .vtt file produced
        vtt_files = list(Path("tmp").glob("captions*.vtt"))
        if vtt_files:
            raw_vtt = vtt_files[0].read_text()
            return strip_vtt_timestamps(raw_vtt)
    return ""


def _transcribe_with_whisper(audio_path: Path) -> str:
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


# ---------------------------------------------------------------------------
# LLM dialogue generation
# ---------------------------------------------------------------------------


def generate_dialogue(
    transcript: str,
    paper_text: str,
    event: PaperClubEvent,
    config: PipelineConfig,
) -> list[dict]:
    """Generate two-speaker podcast dialogue via LLM.

    Uses litellm with the model specified in config.fallback.llm_model.

    Parameters
    ----------
    transcript : str
        Video transcript text.
    paper_text : str
        Extracted paper text.
    event : PaperClubEvent
        The event being processed.
    config : PipelineConfig
        Full pipeline config (uses fallback and notebooklm settings).

    Returns
    -------
    list[dict]
        List of {"speaker": "A"|"B", "text": "..."} dialogue turns.
    """
    from litellm import completion

    response = completion(
        model=config.fallback.llm_model,
        messages=[
            {
                "role": "system",
                "content": (
                    "Create a two-speaker deep-dive podcast script between HOST and EXPERT. "
                    "Format each line as 'HOST: ...' or 'EXPERT: ...'. "
                    f"{config.notebooklm.prompt}"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Paper:\n{paper_text[:50000]}\n\n"
                    f"Discussion transcript:\n{transcript[:30000]}"
                ),
            },
        ],
    )
    raw_dialogue = response.choices[0].message.content

    # Parse into structured speaker turns
    speaker_chunks = parse_speaker_chunks(
        raw_dialogue, config.fallback.chunk_max_chars
    )

    result: list[dict] = []
    for speaker, text in speaker_chunks:
        result.append(
            {
                "speaker": "A" if speaker == "HOST" else "B",
                "text": text,
            }
        )

    return result


# ---------------------------------------------------------------------------
# Audio synthesis
# ---------------------------------------------------------------------------


def synthesize_audio(
    dialogue: list[dict], slug: str, config: FallbackConfig
) -> Path:
    """Convert dialogue to audio via OpenAI TTS with two voices.

    Chunks text at word boundaries (max 4096 chars per TTS call).
    Combines segments with pydub. Saves to docs/episodes/{slug}.mp3.

    Parameters
    ----------
    dialogue : list[dict]
        List of {"speaker": "A"|"B", "text": "..."}.
    slug : str
        Episode slug for filename.
    config : FallbackConfig
        Fallback config with TTS settings.

    Returns
    -------
    Path
        Path to the saved MP3.
    """
    from openai import OpenAI
    from pydub import AudioSegment

    client = OpenAI()
    output_path = Path(f"docs/episodes/{slug}.mp3")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    Path("tmp").mkdir(exist_ok=True)

    voices = config.tts_voices  # ["alloy", "echo"] -- Host, Expert

    audio_segments: list[Path] = []
    segment_idx = 0

    for turn in dialogue:
        speaker = turn["speaker"]
        text = turn["text"]
        voice = voices[0] if speaker == "A" else (voices[1] if len(voices) > 1 else voices[0])

        # Chunk text if it exceeds max chars
        chunks = chunk_text(text, config.chunk_max_chars)

        for chunk in chunks:
            segment_path = Path(f"tmp/tts_segment_{segment_idx}.mp3")
            response = client.audio.speech.create(
                model=config.tts_model,
                voice=voice,
                input=chunk,
            )
            response.stream_to_file(str(segment_path))
            audio_segments.append(segment_path)
            segment_idx += 1

    # Combine segments with pydub
    if not audio_segments:
        # Write empty/minimal mp3
        output_path.write_bytes(b"")
        return output_path

    combined = AudioSegment.from_mp3(str(audio_segments[0]))
    for seg_path in audio_segments[1:]:
        combined += AudioSegment.from_mp3(str(seg_path))

    combined.export(str(output_path), format="mp3")

    logger.info(
        "Synthesized audio via TTS (%d segments) -> %s",
        len(audio_segments),
        output_path,
    )

    return output_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def generate_fallback_podcast(
    bundle: ContentBundle,
    event: PaperClubEvent,
    slug: str,
    config: PipelineConfig,
) -> Path:
    """Generate a podcast episode using LLM+TTS fallback.

    Orchestrates: transcript extraction -> dialogue generation -> audio synthesis.

    Parameters
    ----------
    bundle : ContentBundle
        Paper PDFs, audio file, and supplementary text files.
    event : PaperClubEvent
        The event this episode covers.
    slug : str
        Episode slug for filename.
    config : PipelineConfig
        Full pipeline config.

    Returns
    -------
    Path
        Path to the generated MP3 at docs/episodes/{slug}.mp3.

    Raises
    ------
    FallbackConfigError
        If OPENAI_API_KEY is missing from environment.
    """
    # 0. Check required env vars
    required_vars = ["OPENAI_API_KEY"]
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        raise FallbackConfigError(missing)

    # 0.1 Validate TTS voices
    if not config.fallback.tts_voices:
        raise FallbackConfigError(["tts_voices must contain at least one voice"])

    # 1. Get transcript
    video_url = None
    audio_path = None
    if bundle.audio_path:
        audio_path = bundle.audio_path
    # Try to infer video URL from audio path name (video_id.mp3 pattern)
    # For now, we accept video_url being None; captions won't be extracted
    transcript = get_transcript(video_url, audio_path, config.fallback)

    # 2. Extract paper text
    paper_text = extract_text_from_pdfs(bundle.paper_paths)

    # 3. Generate dialogue via LLM
    dialogue = generate_dialogue(transcript, paper_text, event, config)

    # 4. Synthesize audio
    mp3_path = synthesize_audio(dialogue, slug, config.fallback)

    logger.info(
        "Generated episode via LLM+TTS fallback (%d turns)", len(dialogue)
    )

    return mp3_path
