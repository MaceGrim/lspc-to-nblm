"""Tests for src/supplementary.py — blog/thread text extraction."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.supplementary import _is_paper_url, download_supplementary


# ---------------------------------------------------------------------------
# _is_paper_url
# ---------------------------------------------------------------------------

class TestIsPaperUrl:
    def test_arxiv_url(self):
        assert _is_paper_url("https://arxiv.org/abs/2301.00001") is True

    def test_arxiv_subdomain(self):
        assert _is_paper_url("https://export.arxiv.org/pdf/2301.00001") is True

    def test_pdf_extension(self):
        assert _is_paper_url("https://example.com/paper.pdf") is True

    def test_normal_blog_url(self):
        assert _is_paper_url("https://openai.com/blog/some-post") is False

    def test_github_url(self):
        assert _is_paper_url("https://github.com/some/repo") is False


# ---------------------------------------------------------------------------
# URL hashing
# ---------------------------------------------------------------------------

class TestUrlHashing:
    def test_hash_deterministic(self):
        url = "https://example.com/blog/post"
        expected = hashlib.sha256(url.encode()).hexdigest()[:12]
        assert len(expected) == 12
        # Run twice to confirm determinism
        assert hashlib.sha256(url.encode()).hexdigest()[:12] == expected

    def test_different_urls_different_hashes(self):
        h1 = hashlib.sha256(b"https://a.com").hexdigest()[:12]
        h2 = hashlib.sha256(b"https://b.com").hexdigest()[:12]
        assert h1 != h2


# ---------------------------------------------------------------------------
# download_supplementary — successful extraction
# ---------------------------------------------------------------------------

class TestDownloadSupplementarySuccess:
    @patch("src.supplementary.trafilatura")
    @patch("src.supplementary.requests")
    def test_successful_extraction(self, mock_requests, mock_trafilatura, tmp_path):
        url = "https://openai.com/blog/some-post"
        html_content = b"<html><body><p>Great blog post content here.</p></body></html>"

        # Mock HEAD (succeeds)
        mock_requests.head.return_value = MagicMock(status_code=200)
        mock_requests.RequestException = Exception

        # Mock GET with streaming
        mock_resp = MagicMock()
        mock_resp.iter_content.return_value = [html_content]
        mock_resp.raise_for_status.return_value = None
        mock_requests.get.return_value = mock_resp

        # Mock trafilatura extraction
        mock_trafilatura.extract.return_value = "Great blog post content here."

        result = download_supplementary([url], tmp_path)

        assert len(result) == 1
        assert result[0].exists()
        assert result[0].read_text() == "Great blog post content here."

        # Check filename uses sha256 hash
        expected_hash = hashlib.sha256(url.encode()).hexdigest()[:12]
        assert result[0].name == f"{expected_hash}.txt"

    @patch("src.supplementary.trafilatura")
    @patch("src.supplementary.requests")
    def test_multiple_urls(self, mock_requests, mock_trafilatura, tmp_path):
        urls = [
            "https://openai.com/blog/post-1",
            "https://blog.google/ai/post-2",
        ]
        html_content = b"<html><body><p>Content</p></body></html>"

        mock_requests.head.return_value = MagicMock(status_code=200)
        mock_requests.RequestException = Exception

        mock_resp = MagicMock()
        mock_resp.iter_content.return_value = [html_content]
        mock_resp.raise_for_status.return_value = None
        mock_requests.get.return_value = mock_resp

        mock_trafilatura.extract.return_value = "Extracted text."

        result = download_supplementary(urls, tmp_path)

        assert len(result) == 2
        assert all(p.exists() for p in result)

    @patch("src.supplementary.trafilatura")
    @patch("src.supplementary.requests")
    def test_returns_list_of_paths(self, mock_requests, mock_trafilatura, tmp_path):
        url = "https://openai.com/blog/post"
        mock_requests.head.return_value = MagicMock(status_code=200)
        mock_requests.RequestException = Exception

        mock_resp = MagicMock()
        mock_resp.iter_content.return_value = [b"<html>content</html>"]
        mock_resp.raise_for_status.return_value = None
        mock_requests.get.return_value = mock_resp

        mock_trafilatura.extract.return_value = "Text"

        result = download_supplementary([url], tmp_path)

        assert isinstance(result, list)
        assert all(isinstance(p, Path) for p in result)


# ---------------------------------------------------------------------------
# download_supplementary — paper URLs skipped
# ---------------------------------------------------------------------------

class TestPaperUrlsSkipped:
    @patch("src.supplementary.trafilatura")
    @patch("src.supplementary.requests")
    def test_arxiv_urls_skipped(self, mock_requests, mock_trafilatura, tmp_path):
        urls = ["https://arxiv.org/abs/2301.00001"]

        result = download_supplementary(urls, tmp_path)

        assert result == []
        mock_requests.get.assert_not_called()

    @patch("src.supplementary.trafilatura")
    @patch("src.supplementary.requests")
    def test_pdf_urls_skipped(self, mock_requests, mock_trafilatura, tmp_path):
        urls = ["https://example.com/paper.pdf"]

        result = download_supplementary(urls, tmp_path)

        assert result == []
        mock_requests.get.assert_not_called()

    @patch("src.supplementary.trafilatura")
    @patch("src.supplementary.requests")
    def test_mixed_urls_only_non_paper_fetched(
        self, mock_requests, mock_trafilatura, tmp_path
    ):
        urls = [
            "https://arxiv.org/abs/2301.00001",
            "https://openai.com/blog/post",
            "https://example.com/doc.pdf",
        ]
        mock_requests.head.return_value = MagicMock(status_code=200)
        mock_requests.RequestException = Exception

        mock_resp = MagicMock()
        mock_resp.iter_content.return_value = [b"<html>content</html>"]
        mock_resp.raise_for_status.return_value = None
        mock_requests.get.return_value = mock_resp

        mock_trafilatura.extract.return_value = "Blog text"

        result = download_supplementary(urls, tmp_path)

        # Only the blog URL should be fetched
        assert len(result) == 1
        assert mock_requests.get.call_count == 1


# ---------------------------------------------------------------------------
# download_supplementary — failure handling
# ---------------------------------------------------------------------------

class TestFailureHandling:
    @patch("src.supplementary.trafilatura")
    @patch("src.supplementary.requests")
    def test_failed_url_logs_warning_and_skips(
        self, mock_requests, mock_trafilatura, tmp_path, caplog
    ):
        url = "https://openai.com/blog/broken"
        mock_requests.head.return_value = MagicMock(status_code=200)
        mock_requests.RequestException = Exception

        # GET raises an exception
        mock_requests.get.side_effect = Exception("Connection refused")

        with caplog.at_level(logging.WARNING, logger="lspc.supplementary"):
            result = download_supplementary([url], tmp_path)

        assert result == []
        assert any("Failed to fetch supplementary URL" in r.message for r in caplog.records)

    @patch("src.supplementary.trafilatura")
    @patch("src.supplementary.requests")
    def test_trafilatura_returns_none_skips(
        self, mock_requests, mock_trafilatura, tmp_path, caplog
    ):
        url = "https://openai.com/blog/empty"
        mock_requests.head.return_value = MagicMock(status_code=200)
        mock_requests.RequestException = Exception

        mock_resp = MagicMock()
        mock_resp.iter_content.return_value = [b"<html>no useful text</html>"]
        mock_resp.raise_for_status.return_value = None
        mock_requests.get.return_value = mock_resp

        mock_trafilatura.extract.return_value = None

        with caplog.at_level(logging.WARNING, logger="lspc.supplementary"):
            result = download_supplementary([url], tmp_path)

        assert result == []
        assert any("No extractable text" in r.message for r in caplog.records)

    @patch("src.supplementary.trafilatura")
    @patch("src.supplementary.requests")
    def test_head_failure_continues_to_get(
        self, mock_requests, mock_trafilatura, tmp_path
    ):
        url = "https://openai.com/blog/head-fails"

        # HEAD raises an exception
        mock_requests.head.side_effect = Exception("HEAD failed")
        mock_requests.RequestException = Exception

        # GET succeeds
        mock_resp = MagicMock()
        mock_resp.iter_content.return_value = [b"<html>content</html>"]
        mock_resp.raise_for_status.return_value = None
        mock_requests.get.return_value = mock_resp

        mock_trafilatura.extract.return_value = "Extracted content"

        result = download_supplementary([url], tmp_path)

        assert len(result) == 1
        mock_requests.get.assert_called_once()

    @patch("src.supplementary.trafilatura")
    @patch("src.supplementary.requests")
    def test_empty_url_list(self, mock_requests, mock_trafilatura, tmp_path):
        result = download_supplementary([], tmp_path)

        assert result == []
        mock_requests.get.assert_not_called()

    @patch("src.supplementary.trafilatura")
    @patch("src.supplementary.requests")
    def test_tmp_dir_created_if_missing(self, mock_requests, mock_trafilatura, tmp_path):
        nested = tmp_path / "sub" / "dir"
        assert not nested.exists()

        download_supplementary([], nested)

        assert nested.exists()
