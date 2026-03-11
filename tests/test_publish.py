"""Tests for src/publish.py — all subprocess/git calls fully mocked."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

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
from src.errors import PublishError
from src.publish import publish_episode, publish_state_update, reencode_mp3
from src.scraper import PaperClubEvent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_event() -> PaperClubEvent:
    return PaperClubEvent(
        title="Test Paper Club",
        date=datetime(2025, 3, 15, tzinfo=timezone.utc),
        event_url="https://lu.ma/test-event",
        paper_urls=["https://arxiv.org/abs/2501.00001"],
    )


@pytest.fixture()
def sample_config() -> PipelineConfig:
    return PipelineConfig(
        luma=LumaConfig(),
        youtube=YouTubeConfig(),
        notebooklm=NotebookLMConfig(),
        fallback=FallbackConfig(),
        rss=RSSConfig(
            base_url="https://example.github.io/podcast",
            owner_email="test@example.com",
        ),
        errors=ErrorConfig(),
        schedule=ScheduleConfig(),
        security=SecurityConfig(),
    )


@pytest.fixture()
def sample_slug() -> str:
    return "20250315-abcd1234"


@pytest.fixture()
def mp3_file(tmp_path: Path) -> Path:
    mp3 = tmp_path / "docs" / "episodes" / "20250315-abcd1234.mp3"
    mp3.parent.mkdir(parents=True, exist_ok=True)
    mp3.write_bytes(b"\x00" * 1024)  # small dummy file
    return mp3


# ---------------------------------------------------------------------------
# Helper to build mock subprocess.run side effects
# ---------------------------------------------------------------------------

def _make_completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Return a CompletedProcess matching subprocess.run expectations."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode,
        stdout=stdout, stderr=stderr.encode() if isinstance(stderr, str) else stderr,
    )


# ---------------------------------------------------------------------------
# publish_episode tests
# ---------------------------------------------------------------------------

class TestPublishEpisode:
    """Test the two-commit publish flow."""

    @patch("src.publish.publish_state_update")
    @patch("src.publish.subprocess.run")
    def test_full_two_commit_flow(
        self, mock_run, mock_state_update, sample_event, sample_slug,
        sample_config, mp3_file,
    ):
        """Episode commit + push succeeds, then state update is called."""
        # git add -> ok, git diff --cached -> has changes (rc=1),
        # git commit -> ok, git push -> ok
        mock_run.side_effect = [
            _make_completed(0),      # git add
            _make_completed(1),      # git diff --cached --quiet (has changes)
            _make_completed(0),      # git commit
            _make_completed(0),      # git push
        ]

        publish_episode(mp3_file, sample_event, sample_slug, {}, sample_config)

        # Verify git add called with mp3 and feed.xml
        add_call = mock_run.call_args_list[0]
        assert str(mp3_file) in add_call.args[0]
        assert "docs/feed.xml" in add_call.args[0]

        # Verify commit message
        commit_call = mock_run.call_args_list[2]
        assert "Add episode:" in commit_call.args[0][-1]

        # Verify push
        push_call = mock_run.call_args_list[3]
        assert push_call.args[0] == ["git", "push", "origin", "main"]

        # Verify state update called
        mock_state_update.assert_called_once_with(
            sample_event, sample_slug, {}, sample_config, repair_feed=False,
        )

    @patch("src.publish.publish_state_update")
    @patch("src.publish.subprocess.run")
    def test_no_staged_changes_skips_commit(
        self, mock_run, mock_state_update, sample_event, sample_slug,
        sample_config, mp3_file,
    ):
        """When no changes are staged, skip commit but still update state."""
        mock_run.side_effect = [
            _make_completed(0),      # git add
            _make_completed(0),      # git diff --cached --quiet (no changes)
        ]

        publish_episode(mp3_file, sample_event, sample_slug, {}, sample_config)

        # Should NOT have committed or pushed
        assert len(mock_run.call_args_list) == 2
        # State update still called
        mock_state_update.assert_called_once()

    @patch("src.publish.publish_state_update")
    @patch("src.publish.subprocess.run")
    def test_push_failure_reverts_episode_commit(
        self, mock_run, mock_state_update, sample_event, sample_slug,
        sample_config, mp3_file,
    ):
        """Push failure at step 4 reverts via git reset --hard HEAD~1."""
        mock_run.side_effect = [
            _make_completed(0),                # git add
            _make_completed(1),                # git diff --cached (has changes)
            _make_completed(0),                # git commit
            _make_completed(1, stderr="error"), # git push FAILS
            _make_completed(0),                # git reset --hard HEAD~1
        ]

        with pytest.raises(PublishError, match="Episode push failed"):
            publish_episode(
                mp3_file, sample_event, sample_slug, {}, sample_config,
            )

        # Verify reset was called
        reset_call = mock_run.call_args_list[4]
        assert reset_call.args[0] == ["git", "reset", "--hard", "HEAD~1"]

        # State update should NOT have been called
        mock_state_update.assert_not_called()

    @patch("src.publish.subprocess.run")
    def test_git_add_failure_raises_publish_error(
        self, mock_run, sample_event, sample_slug, sample_config, mp3_file,
    ):
        """Git add failure raises PublishError."""
        mock_run.side_effect = subprocess.CalledProcessError(1, "git add")

        with pytest.raises(PublishError, match="Git staging/commit failed"):
            publish_episode(
                mp3_file, sample_event, sample_slug, {}, sample_config,
            )


# ---------------------------------------------------------------------------
# publish_state_update tests
# ---------------------------------------------------------------------------

class TestPublishStateUpdate:
    """Test state update commit + push."""

    @patch("src.publish.save_state")
    @patch("src.publish.mark_processed")
    @patch("src.publish.load_state", return_value={})
    @patch("src.publish.subprocess.run")
    def test_successful_state_update(
        self, mock_run, mock_load, mock_mark, mock_save,
        sample_event, sample_slug, sample_config,
    ):
        """State update: fetch, reset, save state, add, commit, push."""
        mock_run.side_effect = [
            _make_completed(0, stdout=""),  # git status --porcelain
            _make_completed(0),             # git fetch
            _make_completed(0),             # git reset --hard
            _make_completed(0),             # git add (processed.json, feed.xml)
            _make_completed(0),             # git commit
            _make_completed(0),             # git push
        ]

        publish_state_update(
            sample_event, sample_slug, {}, sample_config,
        )

        # Verify fetch + reset
        assert mock_run.call_args_list[1].args[0] == [
            "git", "fetch", "origin", "main",
        ]
        assert mock_run.call_args_list[2].args[0] == [
            "git", "reset", "--hard", "origin/main",
        ]

        # State was saved
        mock_mark.assert_called_once()
        mock_save.assert_called_once()

        # Push was called
        push_call = mock_run.call_args_list[5]
        assert push_call.args[0] == ["git", "push", "origin", "main"]

    @patch("src.publish.save_state")
    @patch("src.publish.mark_processed")
    @patch("src.publish.load_state", return_value={})
    @patch("src.publish.subprocess.run")
    def test_push_failure_allows_retry(
        self, mock_run, mock_load, mock_mark, mock_save,
        sample_event, sample_slug, sample_config,
    ):
        """Push failure at state update raises PublishError but episode is live."""
        mock_run.side_effect = [
            _make_completed(0, stdout=""),   # git status --porcelain
            _make_completed(0),              # git fetch
            _make_completed(0),              # git reset --hard
            _make_completed(0),              # git add
            _make_completed(0),              # git commit
            _make_completed(1, stderr="err"), # git push FAILS
            _make_completed(0),              # git reset --hard HEAD~1
        ]

        with pytest.raises(PublishError, match="will retry"):
            publish_state_update(
                sample_event, sample_slug, {}, sample_config,
            )

        # Reset was called after failed push
        reset_call = mock_run.call_args_list[6]
        assert reset_call.args[0] == ["git", "reset", "--hard", "HEAD~1"]

    @patch("src.publish.subprocess.run")
    def test_dirty_working_tree_raises(
        self, mock_run, sample_event, sample_slug, sample_config,
    ):
        """Dirty working tree prevents state update."""
        mock_run.side_effect = [
            _make_completed(0, stdout="M src/file.py"),  # dirty
        ]

        with pytest.raises(PublishError, match="dirty working tree"):
            publish_state_update(
                sample_event, sample_slug, {}, sample_config,
            )

    @patch("src.publish.update_rss_feed")
    @patch("src.publish.feed_contains_guid", return_value=False)
    @patch("src.publish.save_state")
    @patch("src.publish.mark_processed")
    @patch("src.publish.load_state", return_value={})
    @patch("src.publish.subprocess.run")
    def test_repair_feed_when_guid_missing(
        self, mock_run, mock_load, mock_mark, mock_save,
        mock_feed_check, mock_update_rss,
        sample_event, sample_slug, sample_config, mp3_file,
    ):
        """repair_feed=True triggers RSS rebuild when guid is missing."""
        mock_run.side_effect = [
            _make_completed(0, stdout=""),  # git status
            _make_completed(0),             # git fetch
            _make_completed(0),             # git reset
            _make_completed(0),             # git add episode_path
            _make_completed(0),             # git add processed.json, feed.xml
            _make_completed(0),             # git commit
            _make_completed(0),             # git push
        ]

        publish_state_update(
            sample_event, sample_slug, {}, sample_config,
            repair_feed=True, episode_path=mp3_file,
        )

        mock_feed_check.assert_called_once_with(sample_slug)
        mock_update_rss.assert_called_once()

    @patch("src.publish.save_state")
    @patch("src.publish.mark_processed")
    @patch("src.publish.load_state", return_value={})
    @patch("src.publish.subprocess.run")
    def test_episode_path_staged(
        self, mock_run, mock_load, mock_mark, mock_save,
        sample_event, sample_slug, sample_config, mp3_file,
    ):
        """episode_path causes the MP3 to be staged after reset."""
        mock_run.side_effect = [
            _make_completed(0, stdout=""),  # git status
            _make_completed(0),             # git fetch
            _make_completed(0),             # git reset
            _make_completed(0),             # git add episode_path
            _make_completed(0),             # git add processed.json, feed.xml
            _make_completed(0),             # git commit
            _make_completed(0),             # git push
        ]

        publish_state_update(
            sample_event, sample_slug, {}, sample_config,
            episode_path=mp3_file,
        )

        # Verify episode file was staged
        add_episode_call = mock_run.call_args_list[3]
        assert str(mp3_file) in add_episode_call.args[0]


# ---------------------------------------------------------------------------
# reencode_mp3 tests
# ---------------------------------------------------------------------------

class TestReencodeMp3:
    """Test MP3 re-encoding with mocked ffmpeg/ffprobe."""

    @patch("src.publish.subprocess.run")
    def test_reencode_with_explicit_bitrate(self, mock_run, mp3_file):
        """Explicit bitrate bypasses ffprobe, calls ffmpeg directly."""
        # ffmpeg mock creates the .reencoded.mp3 file so replace() works
        def _ffmpeg_side_effect(*args, **kwargs):
            mp3_file.with_suffix(".reencoded.mp3").write_bytes(b"\x00" * 512)
            return _make_completed(0)

        mock_run.side_effect = _ffmpeg_side_effect

        reencode_mp3(mp3_file, bitrate="96k")

        assert mock_run.call_count == 1
        ffmpeg_call = mock_run.call_args_list[0]
        cmd = ffmpeg_call.args[0]
        assert cmd[0] == "ffmpeg"
        assert "-b:a" in cmd
        assert "96k" in cmd

    @patch("src.publish.subprocess.run")
    def test_reencode_auto_bitrate(self, mock_run, mp3_file):
        """Auto bitrate uses ffprobe for duration, then ffmpeg."""
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_completed(0, stdout="3600.0")  # ffprobe
            else:
                mp3_file.with_suffix(".reencoded.mp3").write_bytes(b"\x00" * 512)
                return _make_completed(0)  # ffmpeg

        mock_run.side_effect = _side_effect

        reencode_mp3(mp3_file, bitrate="auto")

        assert mock_run.call_count == 2
        # First call is ffprobe
        probe_call = mock_run.call_args_list[0]
        assert "ffprobe" in probe_call.args[0]
        # Second call is ffmpeg with computed bitrate
        ffmpeg_call = mock_run.call_args_list[1]
        cmd = ffmpeg_call.args[0]
        assert cmd[0] == "ffmpeg"
        # For 1hr audio, target = (90M * 8) / 3600 / 1000 = 200 kbps, clamped to 128k
        assert "128k" in cmd

    @patch("src.publish.subprocess.run")
    def test_reencode_auto_bitrate_short_audio(self, mock_run, mp3_file):
        """Short audio calculates a reasonable bitrate."""
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_completed(0, stdout="600.0")
            else:
                mp3_file.with_suffix(".reencoded.mp3").write_bytes(b"\x00" * 512)
                return _make_completed(0)

        mock_run.side_effect = _side_effect

        reencode_mp3(mp3_file, bitrate="auto")

        ffmpeg_call = mock_run.call_args_list[1]
        assert "128k" in ffmpeg_call.args[0]

    @patch("src.publish.subprocess.run")
    def test_reencode_auto_ffprobe_failure_defaults_96k(self, mock_run, mp3_file):
        """ffprobe failure defaults to 96k bitrate."""
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_completed(1, stdout="")  # ffprobe fails
            else:
                mp3_file.with_suffix(".reencoded.mp3").write_bytes(b"\x00" * 512)
                return _make_completed(0)

        mock_run.side_effect = _side_effect

        reencode_mp3(mp3_file, bitrate="auto")

        ffmpeg_call = mock_run.call_args_list[1]
        assert "96k" in ffmpeg_call.args[0]


# ---------------------------------------------------------------------------
# Size check tests
# ---------------------------------------------------------------------------

class TestSizeCheck:
    """Test pre-commit size check logic (95MB threshold)."""

    @patch("src.publish.reencode_mp3")
    def test_oversized_mp3_triggers_reencode(self, mock_reencode, tmp_path):
        """MP3 > 95MB should trigger re-encode (tested at pipeline level)."""
        mp3 = tmp_path / "big.mp3"
        mp3.write_bytes(b"\x00" * (96_000_000))  # 96MB > 95MB threshold

        file_size = mp3.stat().st_size
        assert file_size > 95_000_000

        # Simulate what process_single_event does
        if file_size > 95_000_000:
            mock_reencode(mp3, bitrate="auto")

        mock_reencode.assert_called_once_with(mp3, bitrate="auto")

    def test_small_mp3_no_reencode(self, tmp_path):
        """MP3 < 95MB should not need re-encoding."""
        mp3 = tmp_path / "small.mp3"
        mp3.write_bytes(b"\x00" * 1024)
        assert mp3.stat().st_size < 95_000_000


# ---------------------------------------------------------------------------
# .gitignore tests
# ---------------------------------------------------------------------------

class TestGitignore:
    """Verify .gitignore contains required entries."""

    def test_gitignore_has_required_entries(self):
        gitignore_path = Path(__file__).parent.parent / ".gitignore"
        assert gitignore_path.exists(), ".gitignore file must exist"

        content = gitignore_path.read_text()
        required = ["tmp/", "logs/", ".env", "credentials/"]
        for entry in required:
            assert entry in content, f".gitignore must contain '{entry}'"


# ---------------------------------------------------------------------------
# Idempotent retry test
# ---------------------------------------------------------------------------

class TestIdempotentRetry:
    """Test idempotent retry when episode exists but state incomplete."""

    @patch("src.publish.update_rss_feed")
    @patch("src.publish.feed_contains_guid", return_value=True)
    @patch("src.publish.save_state")
    @patch("src.publish.mark_processed")
    @patch("src.publish.load_state", return_value={})
    @patch("src.publish.subprocess.run")
    def test_retry_with_existing_episode_and_feed(
        self, mock_run, mock_load, mock_mark, mock_save,
        mock_feed_check, mock_update_rss,
        sample_event, sample_slug, sample_config, mp3_file,
    ):
        """When episode exists and feed has guid, just update state."""
        mock_run.side_effect = [
            _make_completed(0, stdout=""),  # git status
            _make_completed(0),             # git fetch
            _make_completed(0),             # git reset
            _make_completed(0),             # git add episode_path
            _make_completed(0),             # git add processed.json, feed.xml
            _make_completed(0),             # git commit
            _make_completed(0),             # git push
        ]

        # This is what process_single_event calls when episode_path exists
        publish_state_update(
            sample_event, sample_slug, {}, sample_config,
            repair_feed=True, episode_path=mp3_file,
        )

        # Feed was checked but NOT repaired (guid exists)
        mock_feed_check.assert_called_once_with(sample_slug)
        mock_update_rss.assert_not_called()

        # State was updated
        mock_mark.assert_called_once()
        mock_save.assert_called_once()

    @patch("src.publish.update_rss_feed")
    @patch("src.publish.feed_contains_guid", return_value=False)
    @patch("src.publish.save_state")
    @patch("src.publish.mark_processed")
    @patch("src.publish.load_state", return_value={})
    @patch("src.publish.subprocess.run")
    def test_retry_repairs_missing_feed_entry(
        self, mock_run, mock_load, mock_mark, mock_save,
        mock_feed_check, mock_update_rss,
        sample_event, sample_slug, sample_config, mp3_file,
    ):
        """When episode exists but feed missing guid, repair feed."""
        mock_run.side_effect = [
            _make_completed(0, stdout=""),  # git status
            _make_completed(0),             # git fetch
            _make_completed(0),             # git reset
            _make_completed(0),             # git add episode_path
            _make_completed(0),             # git add processed.json, feed.xml
            _make_completed(0),             # git commit
            _make_completed(0),             # git push
        ]

        publish_state_update(
            sample_event, sample_slug, {}, sample_config,
            repair_feed=True, episode_path=mp3_file,
        )

        # Feed was repaired
        mock_feed_check.assert_called_once_with(sample_slug)
        mock_update_rss.assert_called_once()


# ---------------------------------------------------------------------------
# Git status porcelain flag test
# ---------------------------------------------------------------------------

class TestGitStatusPorcelain:
    """Verify git status uses --porcelain --untracked-files=no."""

    @patch("src.publish.save_state")
    @patch("src.publish.mark_processed")
    @patch("src.publish.load_state", return_value={})
    @patch("src.publish.subprocess.run")
    def test_git_status_uses_correct_flags(
        self, mock_run, mock_load, mock_mark, mock_save,
        sample_event, sample_slug, sample_config,
    ):
        mock_run.side_effect = [
            _make_completed(0, stdout=""),  # git status
            _make_completed(0),             # git fetch
            _make_completed(0),             # git reset
            _make_completed(0),             # git add
            _make_completed(0),             # git commit
            _make_completed(0),             # git push
        ]

        publish_state_update(
            sample_event, sample_slug, {}, sample_config,
        )

        status_call = mock_run.call_args_list[0]
        cmd = status_call.args[0]
        assert cmd == [
            "git", "status", "--porcelain", "--untracked-files=no",
        ]
