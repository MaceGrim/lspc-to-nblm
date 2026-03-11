"""Tests for src/fallback.py — LLM+TTS fallback podcast pipeline."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.config import FallbackConfig, NotebookLMConfig, PipelineConfig
from src.errors import FallbackConfigError
from src.fallback import (
    chunk_text,
    generate_dialogue,
    generate_fallback_podcast,
    get_transcript,
    parse_speaker_chunks,
    strip_vtt_timestamps,
    synthesize_audio,
)
from src.podcast import ContentBundle
from src.scraper import PaperClubEvent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_event() -> PaperClubEvent:
    return PaperClubEvent(
        title="Test Paper Club",
        date=datetime(2025, 3, 15, 12, 0, 0, tzinfo=timezone.utc),
        event_url="https://lu.ma/test-event",
        paper_urls=["https://arxiv.org/abs/2501.00001"],
    )


@pytest.fixture()
def fallback_config() -> FallbackConfig:
    return FallbackConfig()


@pytest.fixture()
def pipeline_config() -> PipelineConfig:
    """Minimal PipelineConfig with defaults for fallback testing."""
    from src.config import (
        ErrorConfig,
        LumaConfig,
        RSSConfig,
        ScheduleConfig,
        SecurityConfig,
        YouTubeConfig,
    )

    return PipelineConfig(
        luma=LumaConfig(),
        youtube=YouTubeConfig(),
        notebooklm=NotebookLMConfig(),
        fallback=FallbackConfig(),
        rss=RSSConfig(base_url="https://example.com", owner_email="test@test.com"),
        errors=ErrorConfig(),
        schedule=ScheduleConfig(),
        security=SecurityConfig(),
    )


@pytest.fixture()
def sample_bundle(tmp_path: Path) -> ContentBundle:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake audio data")
    return ContentBundle(
        paper_paths=[pdf],
        audio_path=audio,
        supplementary_paths=[],
    )


@pytest.fixture()
def mock_pymupdf():
    """Install a fake pymupdf module and configure it to return sample text."""
    mock_module = MagicMock()
    mock_page = MagicMock()
    mock_page.get_text.return_value = "This is the paper text content."
    mock_doc = MagicMock()
    mock_doc.__iter__ = MagicMock(return_value=iter([mock_page]))
    mock_module.open.return_value = mock_doc
    with patch.dict(sys.modules, {"pymupdf": mock_module}):
        yield mock_module


# ---------------------------------------------------------------------------
# chunk_text
# ---------------------------------------------------------------------------


class TestChunkText:
    def test_short_text_single_chunk(self):
        result = chunk_text("Hello world.", max_chars=100)
        assert result == ["Hello world."]

    def test_splits_at_sentence_boundaries(self):
        text = "First sentence. Second sentence. Third sentence."
        result = chunk_text(text, max_chars=35)
        # Should split into multiple chunks
        assert len(result) >= 2
        assert all(len(c) <= 35 for c in result)

    def test_long_sentence_splits_at_word_boundaries(self):
        # A single sentence with many words that exceeds max_chars
        words = ["word"] * 50
        text = " ".join(words) + "."
        result = chunk_text(text, max_chars=30)
        assert len(result) > 1
        assert all(len(c) <= 30 for c in result)

    def test_very_long_word_hard_splits(self):
        text = "A" * 100
        result = chunk_text(text, max_chars=30)
        assert len(result) > 1
        assert all(len(c) <= 30 for c in result)
        assert "".join(result) == "A" * 100

    def test_empty_text(self):
        result = chunk_text("", max_chars=100)
        assert result == []

    def test_respects_max_chars(self):
        text = "One. Two. Three. Four. Five. Six. Seven. Eight. Nine. Ten."
        result = chunk_text(text, max_chars=20)
        for chunk in result:
            assert len(chunk) <= 20

    def test_default_max_chars(self):
        short_text = "Short text."
        result = chunk_text(short_text)
        assert result == ["Short text."]


# ---------------------------------------------------------------------------
# parse_speaker_chunks
# ---------------------------------------------------------------------------


class TestParseSpeakerChunks:
    def test_basic_two_speaker_parsing(self):
        dialogue = "HOST: Hello everyone.\nEXPERT: Thanks for having me."
        result = parse_speaker_chunks(dialogue, max_chars=4096)
        assert len(result) == 2
        assert result[0] == ("HOST", "Hello everyone.")
        assert result[1] == ("EXPERT", "Thanks for having me.")

    def test_markdown_bold_speaker_tags(self):
        dialogue = "**HOST**: Welcome.\n**EXPERT**: Hi there."
        result = parse_speaker_chunks(dialogue, max_chars=4096)
        assert len(result) == 2
        assert result[0][0] == "HOST"
        assert result[1][0] == "EXPERT"

    def test_case_insensitive_speaker_tags(self):
        dialogue = "host: Hello.\nexpert: Hi."
        result = parse_speaker_chunks(dialogue, max_chars=4096)
        assert result[0][0] == "HOST"
        assert result[1][0] == "EXPERT"

    def test_multiline_turn(self):
        dialogue = "HOST: First line.\nSecond line of host.\nEXPERT: Expert line."
        result = parse_speaker_chunks(dialogue, max_chars=4096)
        assert len(result) == 2
        assert "First line." in result[0][1]
        assert "Second line" in result[0][1]
        assert result[1][1] == "Expert line."

    def test_long_turn_gets_sub_chunked(self):
        long_text = "HOST: " + "This is a sentence. " * 50
        result = parse_speaker_chunks(long_text, max_chars=100)
        assert len(result) > 1
        assert all(speaker == "HOST" for speaker, _ in result)
        assert all(len(text) <= 100 for _, text in result)

    def test_empty_dialogue(self):
        result = parse_speaker_chunks("", max_chars=4096)
        assert result == []

    def test_blank_lines_ignored(self):
        dialogue = "HOST: Hello.\n\n\nEXPERT: World."
        result = parse_speaker_chunks(dialogue, max_chars=4096)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# strip_vtt_timestamps
# ---------------------------------------------------------------------------


class TestStripVttTimestamps:
    def test_strips_timestamps_and_headers(self):
        vtt = (
            "WEBVTT\n\n"
            "1\n"
            "00:00:00.000 --> 00:00:02.000\n"
            "Hello world\n\n"
            "2\n"
            "00:00:02.000 --> 00:00:04.000\n"
            "More text\n"
        )
        result = strip_vtt_timestamps(vtt)
        assert "Hello world" in result
        assert "More text" in result
        assert "-->" not in result
        assert "WEBVTT" not in result

    def test_strips_html_tags(self):
        vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n<b>Bold text</b>\n"
        result = strip_vtt_timestamps(vtt)
        assert result == "Bold text"

    def test_deduplicates_adjacent_lines(self):
        vtt = (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:01.000\n"
            "Same line\n"
            "00:00:01.000 --> 00:00:02.000\n"
            "Same line\n"
        )
        result = strip_vtt_timestamps(vtt)
        assert result.count("Same line") == 1

    def test_empty_vtt(self):
        result = strip_vtt_timestamps("")
        assert result == ""


# ---------------------------------------------------------------------------
# get_transcript
# ---------------------------------------------------------------------------


class TestGetTranscript:
    @patch("src.fallback._extract_youtube_captions")
    def test_youtube_captions_success(self, mock_captions, fallback_config):
        """When YouTube captions are available, they are returned."""
        mock_captions.return_value = "Hello from captions"

        result = get_transcript(
            "https://youtube.com/watch?v=test123", None, fallback_config
        )

        assert result == "Hello from captions"
        mock_captions.assert_called_once_with("https://youtube.com/watch?v=test123")

    @patch("src.fallback._extract_youtube_captions", return_value="")
    @patch("src.fallback._transcribe_with_whisper", return_value="Whisper text")
    def test_falls_back_to_whisper(
        self, mock_whisper, mock_captions, tmp_path, fallback_config
    ):
        """When captions are empty, falls back to Whisper."""
        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"fake audio")

        result = get_transcript(
            "https://youtube.com/watch?v=test", audio, fallback_config
        )
        assert result == "Whisper text"
        mock_whisper.assert_called_once_with(audio)

    @patch("src.fallback._extract_youtube_captions", return_value="Caption text")
    def test_prefers_captions_over_whisper(
        self, mock_captions, tmp_path, fallback_config
    ):
        """Captions are preferred; Whisper is not called."""
        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"fake audio")

        result = get_transcript(
            "https://youtube.com/watch?v=test", audio, fallback_config
        )
        assert result == "Caption text"

    def test_no_video_no_audio_returns_empty(self, fallback_config):
        result = get_transcript(None, None, fallback_config)
        assert result == ""

    @patch("src.fallback._extract_youtube_captions", return_value="")
    def test_no_audio_path_returns_empty(self, mock_captions, fallback_config):
        result = get_transcript(
            "https://youtube.com/watch?v=test", None, fallback_config
        )
        assert result == ""


# ---------------------------------------------------------------------------
# generate_dialogue
# ---------------------------------------------------------------------------


class TestGenerateDialogue:
    def test_generates_dialogue_with_mock_litellm(
        self, sample_event, pipeline_config
    ):
        """LLM returns HOST/EXPERT dialogue, parsed into speaker turns."""
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(
                message=MagicMock(
                    content="HOST: Welcome to the show.\nEXPERT: Thanks for having me."
                )
            )
        ]

        mock_litellm = MagicMock()
        mock_litellm.completion.return_value = mock_response

        with patch.dict(sys.modules, {"litellm": mock_litellm}):
            result = generate_dialogue(
                "transcript text", "paper text", sample_event, pipeline_config
            )

        assert len(result) == 2
        assert result[0] == {"speaker": "A", "text": "Welcome to the show."}
        assert result[1] == {"speaker": "B", "text": "Thanks for having me."}

        # Verify litellm was called with correct model
        mock_litellm.completion.assert_called_once()
        call_kwargs = mock_litellm.completion.call_args
        assert call_kwargs.kwargs["model"] == "gpt-4o-mini"

    def test_dialogue_with_custom_model(self, sample_event, pipeline_config):
        """Respects configured LLM model."""
        pipeline_config.fallback.llm_model = "claude-3-haiku-20240307"

        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content="HOST: Hi.\nEXPERT: Hello."))
        ]

        mock_litellm = MagicMock()
        mock_litellm.completion.return_value = mock_response

        with patch.dict(sys.modules, {"litellm": mock_litellm}):
            generate_dialogue(
                "transcript", "paper", sample_event, pipeline_config
            )

        call_kwargs = mock_litellm.completion.call_args
        assert call_kwargs.kwargs["model"] == "claude-3-haiku-20240307"

    def test_truncates_long_input(self, sample_event, pipeline_config):
        """Paper text is truncated to 50k chars, transcript to 30k."""
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content="HOST: Summary."))
        ]

        mock_litellm = MagicMock()
        mock_litellm.completion.return_value = mock_response

        long_paper = "P" * 100_000
        long_transcript = "T" * 60_000

        with patch.dict(sys.modules, {"litellm": mock_litellm}):
            generate_dialogue(
                long_transcript, long_paper, sample_event, pipeline_config
            )

        user_content = mock_litellm.completion.call_args.kwargs["messages"][1]["content"]
        # Paper text is truncated via [:50000] slice. The format string also
        # includes "Paper:\n" prefix which adds one extra "P", hence 50001.
        assert user_content.count("P") == 50_001  # 50000 from paper + 1 from "Paper:"
        assert user_content.count("T") == 30_000


# ---------------------------------------------------------------------------
# synthesize_audio
# ---------------------------------------------------------------------------


class TestSynthesizeAudio:
    def _setup_mocks(self):
        """Create mock openai and pydub modules."""
        mock_openai_module = MagicMock()
        mock_client = MagicMock()
        mock_openai_module.OpenAI.return_value = mock_client
        mock_response = MagicMock()
        mock_client.audio.speech.create.return_value = mock_response

        mock_pydub_module = MagicMock()
        mock_combined = MagicMock()
        mock_pydub_module.AudioSegment.from_mp3.return_value = mock_combined
        mock_combined.__iadd__ = MagicMock(return_value=mock_combined)

        return mock_openai_module, mock_pydub_module, mock_client, mock_combined

    def test_creates_audio_with_two_voices(self, fallback_config):
        """Two speakers get different TTS voices."""
        mock_openai, mock_pydub, mock_client, mock_combined = self._setup_mocks()

        dialogue = [
            {"speaker": "A", "text": "Hello from host."},
            {"speaker": "B", "text": "Hello from expert."},
        ]

        with patch.dict(sys.modules, {"openai": mock_openai, "pydub": mock_pydub}):
            result = synthesize_audio(dialogue, "20250315-abc12345", fallback_config)

        # Verify two TTS calls with different voices
        tts_calls = mock_client.audio.speech.create.call_args_list
        assert len(tts_calls) == 2
        assert tts_calls[0].kwargs["voice"] == "alloy"  # Host voice
        assert tts_calls[1].kwargs["voice"] == "echo"  # Expert voice
        assert tts_calls[0].kwargs["model"] == "tts-1"

        # Verify output path
        assert "docs/episodes/20250315-abc12345.mp3" in str(result)

    def test_chunks_long_text(self, fallback_config):
        """Long text is chunked before TTS calls."""
        mock_openai, mock_pydub, mock_client, mock_combined = self._setup_mocks()

        # Create text that will need chunking (> 100 chars with chunk_max_chars=100)
        long_text = "This is a sentence. " * 30  # ~600 chars
        dialogue = [{"speaker": "A", "text": long_text}]

        fallback_config.chunk_max_chars = 100

        with patch.dict(sys.modules, {"openai": mock_openai, "pydub": mock_pydub}):
            synthesize_audio(dialogue, "test-slug", fallback_config)

        # Should have made multiple TTS calls
        assert mock_client.audio.speech.create.call_count > 1

    def test_empty_dialogue(self, fallback_config):
        """Empty dialogue produces an empty file."""
        mock_openai, mock_pydub, mock_client, _ = self._setup_mocks()

        with patch.dict(sys.modules, {"openai": mock_openai, "pydub": mock_pydub}):
            result = synthesize_audio([], "test-slug", fallback_config)

        assert result.name == "test-slug.mp3"
        mock_client.audio.speech.create.assert_not_called()

    def test_single_voice_fallback(self, fallback_config):
        """When only one voice configured, both speakers use it."""
        fallback_config.tts_voices = ["nova"]
        mock_openai, mock_pydub, mock_client, mock_combined = self._setup_mocks()

        dialogue = [
            {"speaker": "A", "text": "Host."},
            {"speaker": "B", "text": "Expert."},
        ]

        with patch.dict(sys.modules, {"openai": mock_openai, "pydub": mock_pydub}):
            synthesize_audio(dialogue, "test-slug", fallback_config)

        tts_calls = mock_client.audio.speech.create.call_args_list
        assert all(c.kwargs["voice"] == "nova" for c in tts_calls)


# ---------------------------------------------------------------------------
# FallbackConfigError
# ---------------------------------------------------------------------------


class TestFallbackConfigError:
    def test_missing_openai_key_raises_error(
        self, sample_bundle, sample_event, pipeline_config
    ):
        """FallbackConfigError raised when OPENAI_API_KEY missing."""
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(FallbackConfigError, match="OPENAI_API_KEY"):
                generate_fallback_podcast(
                    sample_bundle, sample_event, "test-slug", pipeline_config
                )

    def test_error_contains_missing_vars(self):
        err = FallbackConfigError(["OPENAI_API_KEY"])
        assert "OPENAI_API_KEY" in str(err)
        assert err.missing_vars == ["OPENAI_API_KEY"]

    def test_empty_voices_raises_error(
        self, sample_bundle, sample_event, pipeline_config
    ):
        """FallbackConfigError raised when tts_voices is empty."""
        pipeline_config.fallback.tts_voices = []

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            with pytest.raises(FallbackConfigError, match="tts_voices"):
                generate_fallback_podcast(
                    sample_bundle, sample_event, "test-slug", pipeline_config
                )


# ---------------------------------------------------------------------------
# Full pipeline integration (all mocked)
# ---------------------------------------------------------------------------


class TestGenerateFallbackPodcast:
    @patch("src.fallback.synthesize_audio")
    @patch("src.fallback.generate_dialogue")
    @patch("src.fallback.get_transcript")
    @patch("src.fallback.extract_text_from_pdfs")
    def test_full_pipeline_orchestration(
        self,
        mock_extract,
        mock_transcript,
        mock_dialogue,
        mock_synthesize,
        sample_bundle,
        sample_event,
        pipeline_config,
    ):
        """Full pipeline: transcript -> dialogue -> audio."""
        mock_transcript.return_value = "Transcript text"
        mock_extract.return_value = "Paper text"
        mock_dialogue.return_value = [
            {"speaker": "A", "text": "Hello."},
            {"speaker": "B", "text": "World."},
        ]
        mock_synthesize.return_value = Path("docs/episodes/test-slug.mp3")

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            result = generate_fallback_podcast(
                sample_bundle, sample_event, "test-slug", pipeline_config
            )

        assert result == Path("docs/episodes/test-slug.mp3")
        mock_transcript.assert_called_once()
        mock_dialogue.assert_called_once()
        mock_synthesize.assert_called_once()

    @patch("src.fallback.synthesize_audio")
    @patch("src.fallback.generate_dialogue")
    @patch("src.fallback.get_transcript")
    @patch("src.fallback.extract_text_from_pdfs")
    def test_pipeline_without_audio(
        self,
        mock_extract,
        mock_transcript,
        mock_dialogue,
        mock_synthesize,
        sample_event,
        pipeline_config,
        tmp_path,
    ):
        """Pipeline works when bundle has no audio_path."""
        bundle = ContentBundle(
            paper_paths=[tmp_path / "paper.pdf"],
            audio_path=None,
        )
        (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4 fake")

        mock_transcript.return_value = ""
        mock_extract.return_value = "Paper only text"
        mock_dialogue.return_value = [{"speaker": "A", "text": "Summary."}]
        mock_synthesize.return_value = Path("docs/episodes/test-slug.mp3")

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            result = generate_fallback_podcast(
                bundle, sample_event, "test-slug", pipeline_config
            )

        assert result == Path("docs/episodes/test-slug.mp3")
        # get_transcript called with None video_url, None audio_path
        mock_transcript.assert_called_once_with(None, None, pipeline_config.fallback)

    @patch("src.fallback.synthesize_audio")
    @patch("src.fallback.generate_dialogue")
    @patch("src.fallback.get_transcript")
    @patch("src.fallback.extract_text_from_pdfs")
    def test_pipeline_passes_correct_config(
        self,
        mock_extract,
        mock_transcript,
        mock_dialogue,
        mock_synthesize,
        sample_bundle,
        sample_event,
        pipeline_config,
    ):
        """Pipeline passes fallback config to synthesize_audio."""
        mock_transcript.return_value = ""
        mock_extract.return_value = ""
        mock_dialogue.return_value = []
        mock_synthesize.return_value = Path("docs/episodes/slug.mp3")

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            generate_fallback_podcast(
                sample_bundle, sample_event, "slug", pipeline_config
            )

        # Verify synthesize_audio receives the fallback config
        synth_call = mock_synthesize.call_args
        assert synth_call.args[1] == "slug"
        assert synth_call.args[2] is pipeline_config.fallback
