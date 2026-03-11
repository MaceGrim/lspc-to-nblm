"""Tests for src/pipeline.py — pipeline orchestration."""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from filelock import Timeout

from src.config import (
    ErrorConfig,
    FallbackConfig,
    LumaConfig,
    NotebookLMConfig,
    PipelineConfig,
    RSSConfig,
    ScheduleConfig,
    SecurityConfig,
    YouTubeConfig,
)
from src.errors import PodcastGenerationError, PublishError, VideoNotFoundError
from src.pipeline import (
    SecretRedactFormatter,
    build_manual_event,
    cleanup_tmp,
    is_scheduled_day,
    parse_args,
    process_single_event,
    run_pipeline,
    setup_logging,
)
from src.scraper import PaperClubEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> PipelineConfig:
    """Build a PipelineConfig with sensible defaults for testing."""
    return PipelineConfig(
        luma=overrides.get("luma", LumaConfig()),
        youtube=overrides.get("youtube", YouTubeConfig()),
        notebooklm=overrides.get("notebooklm", NotebookLMConfig()),
        fallback=overrides.get("fallback", FallbackConfig(enabled=False)),
        rss=overrides.get(
            "rss",
            RSSConfig(
                base_url="https://example.github.io/podcast",
                owner_email="test@example.com",
            ),
        ),
        errors=overrides.get("errors", ErrorConfig(max_retries=1, backoff_base=0)),
        schedule=overrides.get("schedule", ScheduleConfig()),
        security=overrides.get("security", SecurityConfig()),
    )


def _make_event(**overrides) -> PaperClubEvent:
    """Build a PaperClubEvent with sensible defaults."""
    return PaperClubEvent(
        title=overrides.get("title", "Test Paper Club Event"),
        date=overrides.get("date", datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc)),
        event_url=overrides.get("event_url", "https://lu.ma/test-event"),
        paper_urls=overrides.get("paper_urls", ["https://arxiv.org/abs/2401.00001"]),
        supplementary_urls=overrides.get("supplementary_urls", []),
    )


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


class TestParseArgs:
    """Test CLI argument parsing."""

    def test_defaults(self):
        args = parse_args([])
        assert args.paper_urls is None
        assert args.video_url is None
        assert args.force is False
        assert args.backfill == 1
        assert args.config == "config.yaml"

    def test_paper_url_repeatable(self):
        args = parse_args([
            "--paper-url", "https://arxiv.org/abs/2401.00001",
            "--paper-url", "https://arxiv.org/abs/2401.00002",
        ])
        assert len(args.paper_urls) == 2

    def test_video_url(self):
        args = parse_args(["--video-url", "https://youtube.com/watch?v=abc123"])
        assert args.video_url == "https://youtube.com/watch?v=abc123"

    def test_force_flag(self):
        args = parse_args(["--force"])
        assert args.force is True

    def test_backfill_value(self):
        args = parse_args(["--backfill", "5"])
        assert args.backfill == 5

    def test_config_path(self):
        args = parse_args(["--config", "/tmp/custom.yaml"])
        assert args.config == "/tmp/custom.yaml"


# ---------------------------------------------------------------------------
# --video-url requires --paper-url validation
# ---------------------------------------------------------------------------


class TestVideoUrlRequiresPaperUrl:
    """--video-url without --paper-url should fail."""

    @patch("src.pipeline.subprocess")
    @patch("src.pipeline.scrape_events")
    @patch("src.pipeline.load_state", return_value={})
    def test_video_url_without_paper_url_returns_1(
        self, mock_state, mock_scrape, mock_subprocess, tmp_path
    ):
        config = _make_config()
        args = parse_args(["--video-url", "https://youtube.com/watch?v=abc"])
        assert args.video_url is not None
        assert args.paper_urls is None

        mock_subprocess.run.return_value = MagicMock(returncode=0)

        with patch("src.pipeline.FileLock") as mock_lock_cls:
            mock_lock = MagicMock()
            mock_lock_cls.return_value = mock_lock
            code = run_pipeline(config, args)

        assert code == 1


# ---------------------------------------------------------------------------
# SecretRedactFormatter
# ---------------------------------------------------------------------------


class TestSecretRedactFormatter:
    """Test that API keys are redacted in log output."""

    def test_redacts_openai_key(self):
        formatter = SecretRedactFormatter("%(message)s")
        record = logging.LogRecord(
            "test", logging.INFO, "", 0,
            "Key: sk-proj-abcdefghijklmnopqrstuvwxyz1234567890", None, None,
        )
        output = formatter.format(record)
        assert "sk-proj-" not in output
        assert "[REDACTED]" in output

    def test_redacts_google_key(self):
        formatter = SecretRedactFormatter("%(message)s")
        record = logging.LogRecord(
            "test", logging.INFO, "", 0,
            "Key: AIzaSyA012345678901234567890123456789AB", None, None,
        )
        output = formatter.format(record)
        assert "AIza" not in output
        assert "[REDACTED]" in output

    def test_redacts_github_pat(self):
        formatter = SecretRedactFormatter("%(message)s")
        record = logging.LogRecord(
            "test", logging.INFO, "", 0,
            "Token: ghp_012345678901234567890123456789ABCDEF", None, None,
        )
        output = formatter.format(record)
        assert "ghp_" not in output
        assert "[REDACTED]" in output

    def test_preserves_normal_text(self):
        formatter = SecretRedactFormatter("%(message)s")
        msg = "Processing event: Paper Club #42"
        record = logging.LogRecord("test", logging.INFO, "", 0, msg, None, None)
        output = formatter.format(record)
        assert output == msg


# ---------------------------------------------------------------------------
# process_single_event exit codes
# ---------------------------------------------------------------------------


class TestProcessSingleEvent:
    """Test exit codes from process_single_event."""

    def test_no_papers_returns_5(self):
        config = _make_config()
        event = _make_event(paper_urls=[])
        state = {}
        code = process_single_event(event, state, config)
        assert code == 5

    @patch("src.pipeline.download_all_papers", return_value=[Path("/tmp/paper.pdf")])
    @patch("src.pipeline.find_and_download_video", return_value=None)
    @patch("src.pipeline.download_supplementary", return_value=[])
    def test_video_not_found_returns_2(
        self, mock_supp, mock_video, mock_papers
    ):
        config = _make_config()
        event = _make_event()
        code = process_single_event(event, {}, config)
        assert code == 2

    @patch("src.pipeline.download_all_papers", return_value=[Path("/tmp/paper.pdf")])
    @patch("src.pipeline.find_and_download_video")
    @patch("src.pipeline.download_supplementary", return_value=[])
    @patch("src.pipeline.generate_podcast")
    @patch("src.pipeline.update_rss_feed")
    @patch("src.pipeline.publish_episode", side_effect=PublishError("push failed"))
    def test_publish_error_returns_4(
        self, mock_pub, mock_rss, mock_podcast, mock_video, mock_papers, mock_supp,
        tmp_path,
    ):
        config = _make_config()
        event = _make_event()

        mock_video_meta = MagicMock()
        mock_video_meta.audio_path = Path("/tmp/audio.mp3")
        mock_video.return_value = mock_video_meta

        mp3 = tmp_path / "episode.mp3"
        mp3.write_bytes(b"\x00" * 100)
        mock_podcast.return_value = mp3

        code = process_single_event(event, {}, config)
        assert code == 4

    @patch("src.pipeline.publish_state_update")
    def test_existing_episode_returns_0(self, mock_pub, tmp_path):
        config = _make_config()
        event = _make_event()

        # Create the expected episode file
        from src.podcast import generate_episode_slug
        slug = generate_episode_slug(event)
        ep_dir = Path("docs/episodes")
        ep_dir.mkdir(parents=True, exist_ok=True)
        ep_path = ep_dir / f"{slug}.mp3"
        ep_path.write_bytes(b"\x00" * 100)

        try:
            code = process_single_event(event, {}, config)
            assert code == 0
            mock_pub.assert_called_once()
        finally:
            ep_path.unlink(missing_ok=True)
            # Clean up directories if empty
            try:
                ep_dir.rmdir()
                Path("docs").rmdir()
            except OSError:
                pass

    @patch("src.pipeline.download_all_papers", return_value=[Path("/tmp/paper.pdf")])
    @patch("src.pipeline.find_and_download_video")
    @patch("src.pipeline.download_supplementary", return_value=[])
    @patch("src.pipeline.generate_podcast")
    @patch("src.pipeline.update_rss_feed")
    @patch("src.pipeline.publish_episode")
    def test_success_returns_0(
        self, mock_pub, mock_rss, mock_podcast, mock_video, mock_papers, mock_supp,
        tmp_path,
    ):
        config = _make_config()
        event = _make_event()

        mock_video_meta = MagicMock()
        mock_video_meta.audio_path = Path("/tmp/audio.mp3")
        mock_video.return_value = mock_video_meta

        mp3 = tmp_path / "episode.mp3"
        mp3.write_bytes(b"\x00" * 100)
        mock_podcast.return_value = mp3

        code = process_single_event(event, {}, config)
        assert code == 0


# ---------------------------------------------------------------------------
# Exit code severity tracking
# ---------------------------------------------------------------------------


class TestExitCodeSeverity:
    """Test that run_pipeline returns the worst exit code by severity."""

    @patch("src.pipeline.subprocess")
    @patch("src.pipeline.scrape_events")
    @patch("src.pipeline.load_state", return_value={})
    @patch("src.pipeline.is_processed", return_value=False)
    @patch("src.pipeline.process_single_event")
    def test_worst_code_wins(
        self, mock_process, mock_is_proc, mock_state, mock_scrape, mock_sub,
    ):
        """When processing multiple events, worst severity code is returned."""
        config = _make_config()
        args = parse_args(["--backfill", "3"])

        events = [_make_event(event_url=f"https://lu.ma/evt{i}") for i in range(3)]
        mock_scrape.return_value = events
        mock_sub.run.return_value = MagicMock(returncode=0)

        # Event 0 -> code 2, Event 1 -> code 5, Event 2 -> code 0
        # Severity: {1:4, 4:3, 5:2, 2:1} => 5 has higher severity than 2
        mock_process.side_effect = [2, 5, 0]

        with patch("src.pipeline.FileLock") as mock_lock_cls:
            mock_lock = MagicMock()
            mock_lock_cls.return_value = mock_lock
            code = run_pipeline(config, args)

        assert code == 5

    @patch("src.pipeline.subprocess")
    @patch("src.pipeline.scrape_events")
    @patch("src.pipeline.load_state", return_value={})
    @patch("src.pipeline.is_processed", return_value=False)
    @patch("src.pipeline.process_single_event")
    def test_fatal_stops_immediately(
        self, mock_process, mock_is_proc, mock_state, mock_scrape, mock_sub,
    ):
        """Exit code 1 should stop processing immediately."""
        config = _make_config()
        args = parse_args(["--backfill", "3"])

        events = [_make_event(event_url=f"https://lu.ma/evt{i}") for i in range(3)]
        mock_scrape.return_value = events
        mock_sub.run.return_value = MagicMock(returncode=0)

        # First event returns 1 (fatal) - should stop immediately
        mock_process.side_effect = [1, 0, 0]

        with patch("src.pipeline.FileLock") as mock_lock_cls:
            mock_lock = MagicMock()
            mock_lock_cls.return_value = mock_lock
            code = run_pipeline(config, args)

        assert code == 1
        # Only the first event was processed
        assert mock_process.call_count == 1


# ---------------------------------------------------------------------------
# File lock prevents overlapping runs
# ---------------------------------------------------------------------------


class TestFileLock:
    """Test that file lock prevents concurrent pipeline runs."""

    def test_lock_conflict_returns_3(self):
        """If lock is already held, run_pipeline returns exit code 3."""
        config = _make_config()
        args = parse_args([])

        with patch("src.pipeline.FileLock") as mock_lock_cls:
            mock_lock = MagicMock()
            mock_lock.acquire.side_effect = Timeout("tmp/pipeline.lock")
            mock_lock_cls.return_value = mock_lock
            code = run_pipeline(config, args)

        assert code == 3


# ---------------------------------------------------------------------------
# Logging to file
# ---------------------------------------------------------------------------


class TestLogging:
    """Test log file creation and secret redaction in output."""

    def test_setup_logging_creates_log_file(self, tmp_path, monkeypatch):
        """setup_logging should create a log file in logs/."""
        monkeypatch.chdir(tmp_path)
        config = _make_config()

        # Clear existing handlers
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        root.handlers = []

        try:
            log = setup_logging(config)
            log.info("Test message")

            log_files = list(Path("logs").glob("run_*.log"))
            assert len(log_files) >= 1

            content = log_files[0].read_text()
            assert "Test message" in content
        finally:
            # Restore original handlers
            for h in root.handlers[:]:
                h.close()
                root.removeHandler(h)
            root.handlers = original_handlers

    def test_secrets_not_in_log_file(self, tmp_path, monkeypatch):
        """API keys should be redacted in the log file."""
        monkeypatch.chdir(tmp_path)
        config = _make_config()

        root = logging.getLogger()
        original_handlers = root.handlers[:]
        root.handlers = []

        try:
            log = setup_logging(config)
            log.info("API key: sk-proj-abcdefghijklmnopqrstuvwxyz1234567890")

            # Flush handlers
            for h in logging.getLogger().handlers:
                h.flush()

            log_files = list(Path("logs").glob("run_*.log"))
            content = log_files[0].read_text()
            assert "sk-proj-" not in content
            assert "[REDACTED]" in content
        finally:
            for h in root.handlers[:]:
                h.close()
                root.removeHandler(h)
            root.handlers = original_handlers


# ---------------------------------------------------------------------------
# --force flag skips dedup
# ---------------------------------------------------------------------------


class TestForceFlag:
    """Test that --force skips the dedup check."""

    @patch("src.pipeline.subprocess")
    @patch("src.pipeline.scrape_events")
    @patch("src.pipeline.load_state")
    @patch("src.pipeline.is_processed", return_value=True)
    @patch("src.pipeline.process_single_event", return_value=0)
    def test_force_processes_already_processed_event(
        self, mock_process, mock_is_proc, mock_state, mock_scrape, mock_sub,
    ):
        config = _make_config()
        args = parse_args(["--force"])

        event = _make_event()
        mock_scrape.return_value = [event]
        mock_state.return_value = {"https://lu.ma/test-event": {"some": "data"}}
        mock_sub.run.return_value = MagicMock(returncode=0)

        with patch("src.pipeline.FileLock") as mock_lock_cls:
            mock_lock = MagicMock()
            mock_lock_cls.return_value = mock_lock
            code = run_pipeline(config, args)

        assert code == 0
        # process_single_event WAS called (force skips dedup)
        mock_process.assert_called_once()

    @patch("src.pipeline.subprocess")
    @patch("src.pipeline.scrape_events")
    @patch("src.pipeline.load_state")
    @patch("src.pipeline.is_processed", return_value=True)
    @patch("src.pipeline.process_single_event", return_value=0)
    def test_without_force_skips_processed_event(
        self, mock_process, mock_is_proc, mock_state, mock_scrape, mock_sub,
    ):
        config = _make_config()
        args = parse_args([])

        event = _make_event()
        mock_scrape.return_value = [event]
        mock_state.return_value = {}
        mock_sub.run.return_value = MagicMock(returncode=0)

        with patch("src.pipeline.FileLock") as mock_lock_cls:
            mock_lock = MagicMock()
            mock_lock_cls.return_value = mock_lock
            code = run_pipeline(config, args)

        # No events left after filtering -> returns 0 with no processing
        assert code == 0
        mock_process.assert_not_called()


# ---------------------------------------------------------------------------
# --backfill processes all events
# ---------------------------------------------------------------------------


class TestBackfill:
    """Test that --backfill passes the limit to scrape_events."""

    @patch("src.pipeline.subprocess")
    @patch("src.pipeline.scrape_events")
    @patch("src.pipeline.load_state", return_value={})
    @patch("src.pipeline.is_processed", return_value=False)
    @patch("src.pipeline.process_single_event", return_value=0)
    def test_backfill_passes_limit(
        self, mock_process, mock_is_proc, mock_state, mock_scrape, mock_sub,
    ):
        config = _make_config()
        args = parse_args(["--backfill", "10"])

        events = [_make_event(event_url=f"https://lu.ma/evt{i}") for i in range(10)]
        mock_scrape.return_value = events
        mock_sub.run.return_value = MagicMock(returncode=0)

        with patch("src.pipeline.FileLock") as mock_lock_cls:
            mock_lock = MagicMock()
            mock_lock_cls.return_value = mock_lock
            code = run_pipeline(config, args)

        # scrape_events called with limit=10
        mock_scrape.assert_called_once_with(config.luma, limit=10)
        assert mock_process.call_count == 10


# ---------------------------------------------------------------------------
# build_manual_event
# ---------------------------------------------------------------------------


class TestBuildManualEvent:
    """Test manual event construction from CLI args."""

    def test_basic_manual_event(self):
        event = build_manual_event(["https://arxiv.org/abs/2401.00001"])
        assert event.title == "Manual Paper Club Entry"
        assert len(event.paper_urls) == 1
        assert event.event_url.startswith("https://manual.local/")

    def test_manual_event_with_video_url(self):
        event = build_manual_event(
            ["https://arxiv.org/abs/2401.00001"],
            "https://youtube.com/watch?v=abc123",
        )
        assert hasattr(event, "_manual_video_url")
        assert event._manual_video_url == "https://youtube.com/watch?v=abc123"

    def test_manual_event_without_video_url(self):
        event = build_manual_event(["https://arxiv.org/abs/2401.00001"])
        assert not hasattr(event, "_manual_video_url")


# ---------------------------------------------------------------------------
# is_scheduled_day
# ---------------------------------------------------------------------------


class TestIsScheduledDay:
    """Test day-gating logic."""

    def test_every_day_schedule(self):
        schedule = ScheduleConfig()
        assert is_scheduled_day(schedule) is True

    def test_empty_schedule(self):
        schedule = ScheduleConfig(run_days=[])
        assert is_scheduled_day(schedule) is False


# ---------------------------------------------------------------------------
# cleanup_tmp
# ---------------------------------------------------------------------------


class TestCleanupTmp:
    """Test temporary file cleanup."""

    def test_cleanup_preserves_lock_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        tmp_dir = tmp_path / "tmp"
        tmp_dir.mkdir()
        (tmp_dir / "pipeline.lock").write_text("lock")
        (tmp_dir / "some_file.pdf").write_text("data")

        cleanup_tmp()

        assert (tmp_dir / "pipeline.lock").exists()
        assert not (tmp_dir / "some_file.pdf").exists()
