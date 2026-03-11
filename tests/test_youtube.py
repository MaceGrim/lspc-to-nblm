"""Tests for src.youtube — video matching, date parsing, and audio download.

All yt-dlp subprocess calls are mocked. No real YouTube access occurs.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.config import YouTubeConfig
from src.errors import VideoNotFoundError, YouTubeDiscoveryError
from src.scraper import PaperClubEvent
from src.youtube import (
    VideoMetadata,
    download_audio,
    find_and_download_video,
    find_paper_club_video,
    parse_yt_date,
    title_similarity,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_event(
    title: str = "Paper Club: Attention Is All You Need",
    date: datetime | None = None,
) -> PaperClubEvent:
    if date is None:
        date = datetime(2024, 3, 15, 18, 0, tzinfo=timezone.utc)
    return PaperClubEvent(
        title=title,
        date=date,
        event_url="https://lu.ma/test-event",
    )


def _make_video(
    video_id: str = "abc123",
    title: str = "Paper Club: Attention Is All You Need",
    upload_date: str = "20240315",
    webpage_url: str = "https://www.youtube.com/watch?v=abc123",
) -> dict:
    return {
        "id": video_id,
        "title": title,
        "upload_date": upload_date,
        "webpage_url": webpage_url,
    }


@pytest.fixture()
def default_config() -> YouTubeConfig:
    return YouTubeConfig()


# ---------------------------------------------------------------------------
# parse_yt_date
# ---------------------------------------------------------------------------

class TestParseYtDate:
    def test_valid_date(self):
        assert parse_yt_date("20240315") == datetime(2024, 3, 15).date()

    def test_none_input(self):
        assert parse_yt_date(None) is None

    def test_empty_string(self):
        assert parse_yt_date("") is None

    def test_wrong_length(self):
        assert parse_yt_date("2024031") is None
        assert parse_yt_date("202403150") is None

    def test_invalid_date(self):
        assert parse_yt_date("20241345") is None


# ---------------------------------------------------------------------------
# title_similarity
# ---------------------------------------------------------------------------

class TestTitleSimilarity:
    def test_identical_titles(self):
        score = title_similarity("attention transformers", "attention transformers")
        assert score == 1.0

    def test_no_overlap(self):
        score = title_similarity("attention transformers", "biology genetics")
        assert score == 0.0

    def test_partial_overlap(self):
        score = title_similarity(
            "attention is all you need review",
            "paper club attention need discussion",
        )
        assert 0.0 < score < 1.0

    def test_stop_words_excluded(self):
        # "the", "and", "for", "with", "paper", "club" are stop words
        score = title_similarity("the paper club", "the paper club")
        assert score == 0.0  # all words are stop words or < 3 chars

    def test_empty_returns_zero(self):
        assert title_similarity("", "something") == 0.0
        assert title_similarity("something", "") == 0.0


# ---------------------------------------------------------------------------
# find_paper_club_video
# ---------------------------------------------------------------------------

class TestFindPaperClubVideo:
    """Test video matching logic with mocked yt-dlp channel listing."""

    @patch("src.youtube._list_channel_videos")
    def test_exact_match(self, mock_list, default_config):
        mock_list.return_value = [
            _make_video(
                video_id="match1",
                title="Paper Club: Attention Is All You Need",
                upload_date="20240315",
            ),
        ]
        event = _make_event()
        result = find_paper_club_video(event, default_config)
        assert result == "match1"

    @patch("src.youtube._list_channel_videos")
    def test_case_insensitive_title_match(self, mock_list, default_config):
        """'paper club' in title should match case-insensitively."""
        mock_list.return_value = [
            _make_video(
                video_id="ci1",
                title="PAPER CLUB: Some Topic",
                upload_date="20240315",
            ),
        ]
        event = _make_event()
        result = find_paper_club_video(event, default_config)
        assert result == "ci1"

    @patch("src.youtube._list_channel_videos")
    def test_date_within_window(self, mock_list, default_config):
        """Video uploaded within match_window_days should match."""
        mock_list.return_value = [
            _make_video(
                video_id="dw1",
                title="Paper Club: Attention Is All You Need",
                upload_date="20240318",  # 3 days after event
            ),
        ]
        event = _make_event()  # date = 2024-03-15
        result = find_paper_club_video(event, default_config)
        assert result == "dw1"

    @patch("src.youtube._list_channel_videos")
    def test_date_outside_window_returns_none(self, mock_list, default_config, caplog):
        """Video too far from event date should not match."""
        mock_list.return_value = [
            _make_video(
                video_id="far1",
                title="Paper Club: Attention Is All You Need",
                upload_date="20240401",  # 17 days after event
            ),
        ]
        event = _make_event()
        with caplog.at_level(logging.WARNING, logger="lspc.youtube"):
            result = find_paper_club_video(event, default_config)
        assert result is None
        assert "No matching video found" in caplog.text

    @patch("src.youtube._list_channel_videos")
    def test_no_paper_club_in_title_returns_none(self, mock_list, default_config, caplog):
        """Videos without 'Paper Club' in title should not match."""
        mock_list.return_value = [
            _make_video(
                video_id="np1",
                title="Latent Space Podcast #42",
                upload_date="20240315",
            ),
        ]
        event = _make_event()
        with caplog.at_level(logging.WARNING, logger="lspc.youtube"):
            result = find_paper_club_video(event, default_config)
        assert result is None

    @patch("src.youtube._list_channel_videos")
    def test_no_videos_raises_discovery_error(self, mock_list, default_config):
        """Empty video list should raise YouTubeDiscoveryError."""
        mock_list.return_value = []
        event = _make_event()
        with pytest.raises(YouTubeDiscoveryError):
            find_paper_club_video(event, default_config)

    @patch("src.youtube._list_channel_videos")
    def test_best_match_by_date_proximity(self, mock_list, default_config):
        """Closer upload date should win when titles are similar."""
        mock_list.return_value = [
            _make_video(
                video_id="far",
                title="Paper Club: Attention Is All You Need",
                upload_date="20240320",  # 5 days away
            ),
            _make_video(
                video_id="close",
                title="Paper Club: Attention Is All You Need",
                upload_date="20240316",  # 1 day away
            ),
        ]
        event = _make_event()
        result = find_paper_club_video(event, default_config)
        assert result == "close"

    @patch("src.youtube._list_channel_videos")
    def test_release_date_fallback(self, mock_list, default_config):
        """Should fall back to release_date when upload_date is absent."""
        mock_list.return_value = [
            {
                "id": "rd1",
                "title": "Paper Club: Test Topic",
                "release_date": "20240315",
                "webpage_url": "https://www.youtube.com/watch?v=rd1",
            },
        ]
        event = _make_event(title="Paper Club: Test Topic")
        result = find_paper_club_video(event, default_config)
        assert result == "rd1"

    @patch("src.youtube._list_channel_videos")
    def test_symmetric_window(self, mock_list, default_config):
        """Video uploaded BEFORE event should also match within window."""
        mock_list.return_value = [
            _make_video(
                video_id="before1",
                title="Paper Club: Attention Is All You Need",
                upload_date="20240310",  # 5 days BEFORE event
            ),
        ]
        event = _make_event()  # 2024-03-15
        result = find_paper_club_video(event, default_config)
        assert result == "before1"


# ---------------------------------------------------------------------------
# download_audio
# ---------------------------------------------------------------------------

class TestDownloadAudio:
    """Test audio download with mocked yt-dlp subprocess call."""

    @patch("src.youtube.subprocess.run")
    def test_downloads_mp3(self, mock_run, tmp_path):
        """download_audio should call yt-dlp and return the MP3 path."""
        video_id = "test_vid_123"
        expected_path = tmp_path / f"{video_id}.mp3"

        # Simulate yt-dlp creating the file
        def side_effect(*args, **kwargs):
            expected_path.write_bytes(b"fake mp3 data")
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        result = download_audio(video_id, tmp_path)

        assert result == expected_path
        assert result.exists()

        # Verify yt-dlp was called with correct arguments
        call_args = mock_run.call_args[0][0]
        assert "yt-dlp" in call_args
        assert "-x" in call_args
        assert "--audio-format" in call_args
        assert "mp3" in call_args
        assert "--audio-quality" in call_args
        assert "128K" in call_args

    @patch("src.youtube.subprocess.run")
    def test_video_id_in_filename(self, mock_run, tmp_path):
        """The output filename must contain the video ID."""
        video_id = "mySpecialId99"
        expected_path = tmp_path / f"{video_id}.mp3"

        def side_effect(*args, **kwargs):
            expected_path.write_bytes(b"fake mp3 data")
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        result = download_audio(video_id, tmp_path)

        assert video_id in result.name

    @patch("src.youtube.subprocess.run")
    def test_creates_tmp_dir(self, mock_run, tmp_path):
        """download_audio should create the tmp directory if missing."""
        nested = tmp_path / "sub" / "dir"
        video_id = "dirtest"
        expected_path = nested / f"{video_id}.mp3"

        def side_effect(*args, **kwargs):
            expected_path.write_bytes(b"data")
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        result = download_audio(video_id, nested)
        assert nested.is_dir()
        assert result == expected_path

    @patch("src.youtube.subprocess.run")
    def test_fallback_extension(self, mock_run, tmp_path):
        """If .mp3 doesn't exist, find any file with the video ID."""
        video_id = "fallback1"
        alt_path = tmp_path / f"{video_id}.m4a"

        def side_effect(*args, **kwargs):
            alt_path.write_bytes(b"m4a data")
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        result = download_audio(video_id, tmp_path)
        assert result == alt_path

    @patch("src.youtube.subprocess.run")
    def test_missing_file_raises_error(self, mock_run, tmp_path):
        """If no file is produced, raise VideoNotFoundError."""
        mock_run.return_value = MagicMock(returncode=0)
        with pytest.raises(VideoNotFoundError):
            download_audio("ghost_vid", tmp_path)

    @patch("src.youtube.subprocess.run")
    def test_subprocess_failure_propagates(self, mock_run, tmp_path):
        """If yt-dlp fails (check=True), CalledProcessError should propagate."""
        mock_run.side_effect = subprocess.CalledProcessError(1, "yt-dlp")
        with pytest.raises(subprocess.CalledProcessError):
            download_audio("fail_vid", tmp_path)

    @patch("src.youtube.subprocess.run")
    def test_output_path_uses_tmp_dir(self, mock_run, tmp_path):
        """The -o template should target the provided tmp_dir."""
        video_id = "pathtest"
        expected_path = tmp_path / f"{video_id}.mp3"

        def side_effect(*args, **kwargs):
            expected_path.write_bytes(b"data")
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        download_audio(video_id, tmp_path)

        call_args = mock_run.call_args[0][0]
        # The -o template should contain the tmp_path
        o_idx = call_args.index("-o")
        template = call_args[o_idx + 1]
        assert str(tmp_path) in template


# ---------------------------------------------------------------------------
# find_and_download_video (integration of find + download)
# ---------------------------------------------------------------------------

class TestFindAndDownloadVideo:
    """Test the convenience wrapper that combines find + download."""

    @patch("src.youtube.download_audio")
    @patch("src.youtube._list_channel_videos")
    def test_returns_video_metadata(self, mock_list, mock_dl, default_config):
        mock_list.return_value = [
            _make_video(
                video_id="full1",
                title="Paper Club: Attention Is All You Need",
                upload_date="20240315",
                webpage_url="https://www.youtube.com/watch?v=full1",
            ),
        ]
        mock_dl.return_value = Path("tmp/full1.mp3")

        event = _make_event()
        result = find_and_download_video(event, default_config)

        assert isinstance(result, VideoMetadata)
        assert result.video_id == "full1"
        assert result.video_url == "https://www.youtube.com/watch?v=full1"
        assert result.audio_path == Path("tmp/full1.mp3")

    @patch("src.youtube._list_channel_videos")
    def test_no_match_returns_none(self, mock_list, default_config, caplog):
        mock_list.return_value = [
            _make_video(
                video_id="nomatch",
                title="Regular Podcast Episode",
                upload_date="20240315",
            ),
        ]
        event = _make_event()
        with caplog.at_level(logging.WARNING, logger="lspc.youtube"):
            result = find_and_download_video(event, default_config)
        assert result is None
