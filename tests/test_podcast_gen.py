"""Tests for src/podcast.py — NotebookLM podcast generation."""

from __future__ import annotations

import concurrent.futures
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.errors import PodcastGenerationError
from src.podcast import (
    ContentBundle,
    extract_text_from_pdfs,
    generate_episode_slug,
    generate_podcast,
)
from src.scraper import PaperClubEvent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_event() -> PaperClubEvent:
    """A minimal PaperClubEvent for testing."""
    return PaperClubEvent(
        title="Test Paper Club",
        date=datetime(2025, 3, 15, 12, 0, 0, tzinfo=timezone.utc),
        event_url="https://lu.ma/test-event",
        paper_urls=["https://arxiv.org/abs/2501.00001"],
    )


@pytest.fixture()
def sample_bundle(tmp_path: Path) -> ContentBundle:
    """A ContentBundle with real temp files."""
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake audio")
    supp = tmp_path / "supp.txt"
    supp.write_text("Supplementary content here")
    return ContentBundle(
        paper_paths=[pdf],
        audio_path=audio,
        supplementary_paths=[supp],
    )


@pytest.fixture()
def nblm_config():
    """NotebookLMConfig with default settings."""
    from src.config import NotebookLMConfig
    return NotebookLMConfig()


@pytest.fixture()
def mock_notebooklm():
    """Install a fake notebooklm module in sys.modules and return the mock client class."""
    mock_module = MagicMock()
    mock_client_cls = MagicMock()
    mock_module.NotebookLMClient = mock_client_cls
    with patch.dict(sys.modules, {"notebooklm": mock_module}):
        yield mock_client_cls


@pytest.fixture()
def mock_pymupdf():
    """Install a fake pymupdf module in sys.modules and return the mock."""
    mock_module = MagicMock()
    with patch.dict(sys.modules, {"pymupdf": mock_module}):
        yield mock_module


# ---------------------------------------------------------------------------
# ContentBundle dataclass
# ---------------------------------------------------------------------------


class TestContentBundle:
    def test_default_fields(self):
        bundle = ContentBundle()
        assert bundle.paper_paths == []
        assert bundle.audio_path is None
        assert bundle.supplementary_paths == []

    def test_custom_fields(self, tmp_path: Path):
        pdf = tmp_path / "a.pdf"
        pdf.touch()
        audio = tmp_path / "b.mp3"
        audio.touch()
        supp = tmp_path / "c.txt"
        supp.touch()
        bundle = ContentBundle(
            paper_paths=[pdf],
            audio_path=audio,
            supplementary_paths=[supp],
        )
        assert bundle.paper_paths == [pdf]
        assert bundle.audio_path == audio
        assert bundle.supplementary_paths == [supp]


# ---------------------------------------------------------------------------
# generate_episode_slug
# ---------------------------------------------------------------------------


class TestGenerateEpisodeSlug:
    def test_slug_format(self, sample_event: PaperClubEvent):
        slug = generate_episode_slug(sample_event)
        # Format: YYYYMMDD-{8 hex chars}
        parts = slug.split("-", 1)
        assert parts[0] == "20250315"
        assert len(parts[1]) == 8
        assert all(c in "0123456789abcdef" for c in parts[1])

    def test_slug_deterministic(self, sample_event: PaperClubEvent):
        slug1 = generate_episode_slug(sample_event)
        slug2 = generate_episode_slug(sample_event)
        assert slug1 == slug2

    def test_different_urls_different_slugs(self):
        event_a = PaperClubEvent(
            title="A",
            date=datetime(2025, 3, 15, tzinfo=timezone.utc),
            event_url="https://lu.ma/event-a",
        )
        event_b = PaperClubEvent(
            title="B",
            date=datetime(2025, 3, 15, tzinfo=timezone.utc),
            event_url="https://lu.ma/event-b",
        )
        assert generate_episode_slug(event_a) != generate_episode_slug(event_b)

    def test_different_dates_different_slugs(self):
        event_a = PaperClubEvent(
            title="Same",
            date=datetime(2025, 3, 15, tzinfo=timezone.utc),
            event_url="https://lu.ma/same-event",
        )
        event_b = PaperClubEvent(
            title="Same",
            date=datetime(2025, 4, 15, tzinfo=timezone.utc),
            event_url="https://lu.ma/same-event",
        )
        slug_a = generate_episode_slug(event_a)
        slug_b = generate_episode_slug(event_b)
        # Same URL hash, different date prefix
        assert slug_a[:8] != slug_b[:8]
        assert slug_a[9:] == slug_b[9:]


# ---------------------------------------------------------------------------
# generate_podcast — happy path
# ---------------------------------------------------------------------------


class TestGeneratePodcast:
    def test_creates_notebook_and_uploads_sources(
        self,
        mock_notebooklm,
        sample_event,
        sample_bundle,
        nblm_config,
    ):
        """Full happy path: client init, notebook creation, source upload,
        audio generation, download, and notebook cleanup."""
        mock_client = MagicMock()
        mock_notebooklm.from_storage.return_value = mock_client
        mock_nb = MagicMock()
        mock_nb.id = "nb-123"
        mock_client.notebooks.create.return_value = mock_nb
        mock_audio = MagicMock()
        mock_nb.artifacts.generate_audio.return_value = mock_audio

        def fake_download(path_str):
            Path(path_str).parent.mkdir(parents=True, exist_ok=True)
            Path(path_str).write_bytes(b"fake mp3 data")

        mock_audio.download_audio.side_effect = fake_download

        result = generate_podcast(sample_bundle, sample_event, nblm_config)

        # Verify notebook created with correct title
        expected_title = "LSPC: Test Paper Club (2025-03-15)"
        mock_client.notebooks.create.assert_called_once_with(expected_title)

        # Verify sources uploaded
        mock_nb.sources.add_file.assert_any_call(str(sample_bundle.paper_paths[0]))
        mock_nb.sources.add_file.assert_any_call(str(sample_bundle.audio_path))
        mock_nb.sources.add_text.assert_called_once_with("Supplementary content here")

        # Verify audio generation called with config
        mock_nb.artifacts.generate_audio.assert_called_once_with(
            format=nblm_config.format,
            length=nblm_config.length,
            focus=nblm_config.prompt,
        )

        # Verify download
        mock_audio.download_audio.assert_called_once()
        assert result.name.endswith(".mp3")
        assert "docs/episodes" in str(result)

        # Verify notebook cleanup
        mock_client.notebooks.delete.assert_called_once_with("nb-123")

    def test_no_audio_path(
        self, mock_notebooklm, sample_event, nblm_config, tmp_path
    ):
        """Bundle without audio_path skips audio upload."""
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        bundle = ContentBundle(paper_paths=[pdf])

        mock_client = MagicMock()
        mock_notebooklm.from_storage.return_value = mock_client
        mock_nb = MagicMock()
        mock_nb.id = "nb-456"
        mock_client.notebooks.create.return_value = mock_nb
        mock_audio = MagicMock()
        mock_nb.artifacts.generate_audio.return_value = mock_audio

        def fake_download(path_str):
            Path(path_str).parent.mkdir(parents=True, exist_ok=True)
            Path(path_str).write_bytes(b"fake mp3")

        mock_audio.download_audio.side_effect = fake_download

        generate_podcast(bundle, sample_event, nblm_config)

        # Only one add_file call (paper), no audio
        assert mock_nb.sources.add_file.call_count == 1
        mock_nb.sources.add_text.assert_not_called()


# ---------------------------------------------------------------------------
# generate_podcast — error handling
# ---------------------------------------------------------------------------


class TestGeneratePodcastErrors:
    def test_client_init_failure_raises_podcast_error(
        self, mock_notebooklm, sample_event, sample_bundle, nblm_config
    ):
        mock_notebooklm.from_storage.side_effect = RuntimeError("connection failed")

        with pytest.raises(PodcastGenerationError, match="client init failed"):
            generate_podcast(sample_bundle, sample_event, nblm_config)

    def test_auth_error_creates_flag_file(
        self, mock_notebooklm, sample_event, sample_bundle, nblm_config
    ):
        """Auth-related errors create the flag file."""
        mock_notebooklm.from_storage.side_effect = RuntimeError(
            "authentication credentials expired"
        )
        flag_path = Path("tmp/notebooklm_auth_expired")
        flag_path.unlink(missing_ok=True)

        with pytest.raises(PodcastGenerationError):
            generate_podcast(sample_bundle, sample_event, nblm_config)

        assert flag_path.exists()
        # Cleanup
        flag_path.unlink(missing_ok=True)

    def test_non_auth_error_no_flag_file(
        self, mock_notebooklm, sample_event, sample_bundle, nblm_config
    ):
        """Non-auth errors should NOT create the flag file."""
        mock_notebooklm.from_storage.side_effect = RuntimeError("network timeout")
        flag_path = Path("tmp/notebooklm_auth_expired")
        flag_path.unlink(missing_ok=True)

        with pytest.raises(PodcastGenerationError):
            generate_podcast(sample_bundle, sample_event, nblm_config)

        assert not flag_path.exists()

    def test_generation_error_still_cleans_up_notebook(
        self, mock_notebooklm, sample_event, sample_bundle, nblm_config
    ):
        """Notebook is deleted even when generation fails."""
        mock_client = MagicMock()
        mock_notebooklm.from_storage.return_value = mock_client
        mock_nb = MagicMock()
        mock_nb.id = "nb-fail"
        mock_client.notebooks.create.return_value = mock_nb
        mock_nb.artifacts.generate_audio.side_effect = RuntimeError("generation boom")

        with pytest.raises(RuntimeError, match="generation boom"):
            generate_podcast(sample_bundle, sample_event, nblm_config)

        mock_client.notebooks.delete.assert_called_once_with("nb-fail")


# ---------------------------------------------------------------------------
# Timeout handling
# ---------------------------------------------------------------------------


class TestGeneratePodcastTimeout:
    def test_timeout_raises_podcast_generation_error(
        self, mock_notebooklm, sample_event, sample_bundle, nblm_config
    ):
        mock_client = MagicMock()
        mock_notebooklm.from_storage.return_value = mock_client
        mock_nb = MagicMock()
        mock_nb.id = "nb-timeout"
        mock_client.notebooks.create.return_value = mock_nb

        mock_executor = MagicMock()
        mock_future = MagicMock()
        mock_future.result.side_effect = concurrent.futures.TimeoutError()
        mock_executor.submit.return_value = mock_future

        with patch(
            "src.podcast.concurrent.futures.ThreadPoolExecutor",
            return_value=mock_executor,
        ):
            with pytest.raises(
                PodcastGenerationError,
                match="timed out after 30 minutes",
            ):
                generate_podcast(sample_bundle, sample_event, nblm_config)

        # Verify executor shutdown was called without waiting
        mock_executor.shutdown.assert_called_once_with(wait=False)
        # Notebook cleanup still happens
        mock_client.notebooks.delete.assert_called_once_with("nb-timeout")


# ---------------------------------------------------------------------------
# Auth flag file management
# ---------------------------------------------------------------------------


class TestAuthFlagManagement:
    def test_successful_generation_clears_auth_flag(
        self, mock_notebooklm, sample_event, sample_bundle, nblm_config
    ):
        """Successful generation removes any stale auth-expired flag."""
        flag_path = Path("tmp/notebooklm_auth_expired")
        Path("tmp").mkdir(exist_ok=True)
        flag_path.touch()
        assert flag_path.exists()

        mock_client = MagicMock()
        mock_notebooklm.from_storage.return_value = mock_client
        mock_nb = MagicMock()
        mock_nb.id = "nb-ok"
        mock_client.notebooks.create.return_value = mock_nb
        mock_audio = MagicMock()
        mock_nb.artifacts.generate_audio.return_value = mock_audio

        def fake_download(path_str):
            Path(path_str).parent.mkdir(parents=True, exist_ok=True)
            Path(path_str).write_bytes(b"mp3")

        mock_audio.download_audio.side_effect = fake_download

        generate_podcast(sample_bundle, sample_event, nblm_config)

        assert not flag_path.exists()


# ---------------------------------------------------------------------------
# extract_text_from_pdfs
# ---------------------------------------------------------------------------


class TestExtractTextFromPdfs:
    def test_extracts_text_from_single_pdf(self, mock_pymupdf, tmp_path: Path):
        """Mock pymupdf to verify text extraction."""
        mock_page = MagicMock()
        mock_page.get_text.return_value = "Page one text."

        mock_doc = MagicMock()
        mock_doc.__iter__ = MagicMock(return_value=iter([mock_page]))

        pdf_path = tmp_path / "test.pdf"
        pdf_path.touch()

        mock_pymupdf.open.return_value = mock_doc
        result = extract_text_from_pdfs([pdf_path])

        assert result == "Page one text."
        mock_doc.close.assert_called_once()

    def test_multiple_papers_separated_by_divider(self, mock_pymupdf, tmp_path: Path):
        """Multiple PDFs are separated by --- divider."""
        page_a = MagicMock()
        page_a.get_text.return_value = "Paper A text."
        doc_a = MagicMock()
        doc_a.__iter__ = MagicMock(return_value=iter([page_a]))

        page_b = MagicMock()
        page_b.get_text.return_value = "Paper B text."
        doc_b = MagicMock()
        doc_b.__iter__ = MagicMock(return_value=iter([page_b]))

        pdf_a = tmp_path / "a.pdf"
        pdf_a.touch()
        pdf_b = tmp_path / "b.pdf"
        pdf_b.touch()

        mock_pymupdf.open.side_effect = [doc_a, doc_b]
        result = extract_text_from_pdfs([pdf_a, pdf_b])

        assert "Paper A text." in result
        assert "Paper B text." in result
        assert "\n\n---\n\n" in result
        doc_a.close.assert_called_once()
        doc_b.close.assert_called_once()

    def test_per_paper_budget_distribution(self, mock_pymupdf, tmp_path: Path):
        """Each paper gets max_chars // len(papers) budget."""
        # 2 papers, 20 char budget => 10 chars each
        page1 = MagicMock()
        page1.get_text.return_value = "A" * 15  # exceeds per-paper budget
        doc1 = MagicMock()
        doc1.__iter__ = MagicMock(return_value=iter([page1]))

        page2 = MagicMock()
        page2.get_text.return_value = "B" * 5
        doc2 = MagicMock()
        doc2.__iter__ = MagicMock(return_value=iter([page2]))

        pdf1 = tmp_path / "p1.pdf"
        pdf1.touch()
        pdf2 = tmp_path / "p2.pdf"
        pdf2.touch()

        mock_pymupdf.open.side_effect = [doc1, doc2]
        result = extract_text_from_pdfs([pdf1, pdf2], max_chars=20)

        # First paper's page (15 chars) was read because extraction happens
        # per page (reads the page, THEN checks the budget for the next page).
        # Second paper's page (5 chars) also read.
        assert "A" * 15 in result
        assert "B" * 5 in result

    def test_respects_global_max_chars(self, mock_pymupdf, tmp_path: Path):
        """Stops extracting once total max_chars is reached."""
        page1 = MagicMock()
        page1.get_text.return_value = "X" * 100
        doc1 = MagicMock()
        doc1.__iter__ = MagicMock(return_value=iter([page1]))

        page2 = MagicMock()
        page2.get_text.return_value = "Y" * 100
        doc2 = MagicMock()
        doc2.__iter__ = MagicMock(return_value=iter([page2]))

        pdf1 = tmp_path / "big1.pdf"
        pdf1.touch()
        pdf2 = tmp_path / "big2.pdf"
        pdf2.touch()

        mock_pymupdf.open.side_effect = [doc1, doc2]
        result = extract_text_from_pdfs([pdf1, pdf2], max_chars=50)

        # First paper reads one page (100 chars > budget of 25), so it reads
        # but then hits per_paper_budget. Second paper is skipped because
        # total_chars (100) >= max_chars (50).
        assert "X" * 100 in result
        assert "Y" not in result

    def test_empty_paths_returns_empty_string(self, mock_pymupdf):
        result = extract_text_from_pdfs([])
        assert result == ""

    def test_doc_close_called_on_exception(self, mock_pymupdf, tmp_path: Path):
        """doc.close() is called even if get_text raises."""
        mock_page = MagicMock()
        mock_page.get_text.side_effect = RuntimeError("corrupt PDF")
        mock_doc = MagicMock()
        mock_doc.__iter__ = MagicMock(return_value=iter([mock_page]))

        pdf = tmp_path / "bad.pdf"
        pdf.touch()

        mock_pymupdf.open.return_value = mock_doc
        with pytest.raises(RuntimeError, match="corrupt PDF"):
            extract_text_from_pdfs([pdf])

        mock_doc.close.assert_called_once()
