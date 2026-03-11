"""Tests for src/state.py — deduplication and state tracking."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.scraper import PaperClubEvent
from src.state import (
    is_processed,
    load_state,
    mark_processed,
    save_state,
    should_reprocess,
)


def _make_event(
    title: str = "Paper Club: Test Paper",
    event_url: str = "https://lu.ma/test-event",
    paper_urls: list[str] | None = None,
    date: datetime | None = None,
) -> PaperClubEvent:
    """Helper to build a PaperClubEvent with sensible defaults."""
    return PaperClubEvent(
        title=title,
        date=date or datetime(2025, 6, 15, 18, 0, tzinfo=timezone.utc),
        event_url=event_url,
        paper_urls=paper_urls or [],
    )


# ── load_state ──────────────────────────────────────────────────────────


class TestLoadState:
    def test_existing_file(self, tmp_path: Path):
        state_file = tmp_path / "processed.json"
        data = {"https://lu.ma/abc": {"title": "Test"}}
        state_file.write_text(json.dumps(data))

        result = load_state(state_file)
        assert result == data

    def test_missing_file_returns_empty_dict(self, tmp_path: Path):
        state_file = tmp_path / "processed.json"
        result = load_state(state_file)
        assert result == {}


# ── save_state ──────────────────────────────────────────────────────────


class TestSaveState:
    def test_writes_valid_json(self, tmp_path: Path):
        state_file = tmp_path / "processed.json"
        data = {
            "https://lu.ma/abc": {
                "title": "Test Event",
                "paper_urls": ["https://arxiv.org/abs/2301.00001"],
            }
        }
        save_state(data, state_file)

        assert state_file.exists()
        loaded = json.loads(state_file.read_text())
        assert loaded == data

    def test_atomic_write_no_leftover_tmp(self, tmp_path: Path):
        state_file = tmp_path / "processed.json"
        save_state({"key": "value"}, state_file)

        tmp_file = tmp_path / "processed.json.tmp"
        assert not tmp_file.exists(), "Temp file should be renamed away"


# ── is_processed ────────────────────────────────────────────────────────


class TestIsProcessed:
    def test_processed_event(self):
        event = _make_event(event_url="https://lu.ma/test-event")
        state = {"https://lu.ma/test-event": {"title": "Test"}}
        assert is_processed(event, state) is True

    def test_unprocessed_event(self):
        event = _make_event(event_url="https://lu.ma/new-event")
        state = {"https://lu.ma/test-event": {"title": "Test"}}
        assert is_processed(event, state) is False

    def test_canonicalization_strips_trailing_slash(self):
        event = _make_event(event_url="https://lu.ma/test-event/")
        state = {"https://lu.ma/test-event": {"title": "Test"}}
        assert is_processed(event, state) is True

    def test_canonicalization_strips_query_and_fragment(self):
        event = _make_event(event_url="https://lu.ma/test-event?ref=abc#section")
        state = {"https://lu.ma/test-event": {"title": "Test"}}
        assert is_processed(event, state) is True


# ── should_reprocess ────────────────────────────────────────────────────


class TestShouldReprocess:
    def test_paper_urls_changed(self):
        event = _make_event(
            event_url="https://lu.ma/test-event",
            paper_urls=["https://arxiv.org/abs/2301.00001", "https://arxiv.org/abs/2301.00002"],
        )
        state = {
            "https://lu.ma/test-event": {
                "paper_urls": ["https://arxiv.org/abs/2301.00001"],
            }
        }
        assert should_reprocess(event, state) is True

    def test_paper_urls_same_no_reprocess(self):
        event = _make_event(
            event_url="https://lu.ma/test-event",
            paper_urls=["https://arxiv.org/abs/2301.00001"],
        )
        state = {
            "https://lu.ma/test-event": {
                "paper_urls": ["https://arxiv.org/abs/2301.00001"],
            }
        }
        assert should_reprocess(event, state) is False

    def test_same_urls_different_order_no_reprocess(self):
        event = _make_event(
            event_url="https://lu.ma/test-event",
            paper_urls=[
                "https://arxiv.org/abs/2301.00002",
                "https://arxiv.org/abs/2301.00001",
            ],
        )
        state = {
            "https://lu.ma/test-event": {
                "paper_urls": [
                    "https://arxiv.org/abs/2301.00001",
                    "https://arxiv.org/abs/2301.00002",
                ],
            }
        }
        assert should_reprocess(event, state) is False

    def test_event_not_in_state_returns_false(self):
        event = _make_event(event_url="https://lu.ma/unknown")
        assert should_reprocess(event, {}) is False

    def test_arxiv_canonicalization_pdf_vs_abs(self):
        """pdf/ URL in live event should match abs/ URL in stored state."""
        event = _make_event(
            event_url="https://lu.ma/test-event",
            paper_urls=["https://arxiv.org/pdf/2301.00001"],
        )
        state = {
            "https://lu.ma/test-event": {
                "paper_urls": ["https://arxiv.org/abs/2301.00001"],
            }
        }
        assert should_reprocess(event, state) is False


# ── mark_processed ──────────────────────────────────────────────────────


class TestMarkProcessed:
    def test_adds_correct_metadata(self):
        event = _make_event(
            title="Paper Club: Attention Is All You Need",
            event_url="https://lu.ma/test-event",
            paper_urls=["https://arxiv.org/abs/2301.00001"],
        )
        state = {}
        result = mark_processed(event, "20250615-abcd1234", state)

        key = "https://lu.ma/test-event"
        assert key in result
        entry = result[key]
        assert entry["event_url"] == "https://lu.ma/test-event"
        assert entry["title"] == "Paper Club: Attention Is All You Need"
        assert entry["episode_slug"] == "20250615-abcd1234"
        assert entry["episode_file"] == "docs/episodes/20250615-abcd1234.mp3"
        assert entry["paper_urls"] == ["https://arxiv.org/abs/2301.00001"]
        assert "processed_at" in entry
        assert entry["date"] == "2025-06-15T18:00:00+00:00"

    def test_returns_updated_state(self):
        event = _make_event(event_url="https://lu.ma/new-event")
        state = {"https://lu.ma/old-event": {"title": "Old"}}
        result = mark_processed(event, "20250615-abcd1234", state)

        assert "https://lu.ma/old-event" in result
        assert "https://lu.ma/new-event" in result

    def test_paper_urls_are_canonicalized_and_sorted(self):
        event = _make_event(
            event_url="https://lu.ma/test-event",
            paper_urls=[
                "https://arxiv.org/pdf/2301.00002v1",
                "https://arxiv.org/abs/2301.00001",
            ],
        )
        result = mark_processed(event, "slug", {})
        entry = result["https://lu.ma/test-event"]
        assert entry["paper_urls"] == [
            "https://arxiv.org/abs/2301.00001",
            "https://arxiv.org/abs/2301.00002",
        ]


# ── empty paper_urls handling ───────────────────────────────────────────


class TestEmptyPaperUrls:
    def test_mark_processed_with_empty_paper_urls(self):
        event = _make_event(event_url="https://lu.ma/test-event", paper_urls=[])
        result = mark_processed(event, "slug", {})
        entry = result["https://lu.ma/test-event"]
        assert entry["paper_urls"] == []

    def test_should_reprocess_empty_stored_vs_empty_live(self):
        event = _make_event(
            event_url="https://lu.ma/test-event",
            paper_urls=[],
        )
        state = {"https://lu.ma/test-event": {"paper_urls": []}}
        assert should_reprocess(event, state) is False

    def test_should_reprocess_empty_stored_vs_new_papers(self):
        event = _make_event(
            event_url="https://lu.ma/test-event",
            paper_urls=["https://arxiv.org/abs/2301.00001"],
        )
        state = {"https://lu.ma/test-event": {"paper_urls": []}}
        assert should_reprocess(event, state) is True
