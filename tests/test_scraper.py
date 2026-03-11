"""Tests for src/scraper.py — all HTTP calls mocked."""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pytest
import responses

from src.config import LumaConfig
from src.errors import ConfigError, NoEventsFoundError
from src.scraper import (
    PaperClubEvent,
    canonicalize_event_url,
    canonicalize_paper_url,
    extract_urls_from_description,
    get_latest_paper_club_event,
    scrape_events,
    extract_events_from_json,
    extract_event_cards,
    _parse_iso_datetime,
)


# =========================================================================
# canonicalize_paper_url
# =========================================================================

class TestCanonicalizePaperUrl:
    """Test arXiv and non-arXiv URL normalization."""

    def test_arxiv_abs_unchanged(self):
        url = "https://arxiv.org/abs/2301.07041"
        assert canonicalize_paper_url(url) == "https://arxiv.org/abs/2301.07041"

    def test_arxiv_pdf_to_abs(self):
        url = "https://arxiv.org/pdf/2301.07041"
        assert canonicalize_paper_url(url) == "https://arxiv.org/abs/2301.07041"

    def test_arxiv_html_to_abs(self):
        url = "https://arxiv.org/html/2301.07041"
        assert canonicalize_paper_url(url) == "https://arxiv.org/abs/2301.07041"

    def test_arxiv_strips_version(self):
        url = "https://arxiv.org/abs/2301.07041v3"
        assert canonicalize_paper_url(url) == "https://arxiv.org/abs/2301.07041"

    def test_arxiv_pdf_with_version(self):
        url = "https://arxiv.org/pdf/2301.07041v2"
        assert canonicalize_paper_url(url) == "https://arxiv.org/abs/2301.07041"

    def test_arxiv_subdomain(self):
        url = "https://browse.arxiv.org/abs/2301.07041v1"
        assert canonicalize_paper_url(url) == "https://arxiv.org/abs/2301.07041"

    def test_arxiv_http_upgraded(self):
        url = "http://arxiv.org/abs/2301.07041"
        assert canonicalize_paper_url(url) == "https://arxiv.org/abs/2301.07041"

    def test_arxiv_five_digit_id(self):
        url = "https://arxiv.org/pdf/2301.12345v1"
        assert canonicalize_paper_url(url) == "https://arxiv.org/abs/2301.12345"

    def test_non_arxiv_preserves_query(self):
        url = "https://example.com/paper.pdf?token=abc"
        result = canonicalize_paper_url(url)
        assert "token=abc" in result

    def test_non_arxiv_strips_fragment(self):
        url = "https://example.com/paper.pdf#page=3"
        result = canonicalize_paper_url(url)
        assert "#" not in result

    def test_non_arxiv_http_upgrade(self):
        url = "http://example.com/paper.pdf"
        result = canonicalize_paper_url(url)
        assert result.startswith("https://")

    def test_scheme_less_url(self):
        url = "arxiv.org/abs/2301.07041"
        assert canonicalize_paper_url(url) == "https://arxiv.org/abs/2301.07041"

    def test_mailto_passthrough(self):
        url = "mailto:test@example.com"
        assert canonicalize_paper_url(url) == url

    def test_javascript_passthrough(self):
        url = "javascript:void(0)"
        assert canonicalize_paper_url(url) == url

    def test_ftp_passthrough(self):
        url = "ftp://files.example.com/paper.pdf"
        assert canonicalize_paper_url(url) == url


# =========================================================================
# canonicalize_event_url
# =========================================================================

class TestCanonicalizeEventUrl:
    """Test event URL normalization."""

    def test_basic_luma_url(self):
        url = "https://lu.ma/some-event"
        assert canonicalize_event_url(url) == "https://lu.ma/some-event"

    def test_strips_trailing_slash(self):
        url = "https://lu.ma/some-event/"
        assert canonicalize_event_url(url) == "https://lu.ma/some-event"

    def test_strips_query_params(self):
        url = "https://lu.ma/some-event?ref=twitter"
        assert canonicalize_event_url(url) == "https://lu.ma/some-event"

    def test_strips_fragment(self):
        url = "https://lu.ma/some-event#details"
        assert canonicalize_event_url(url) == "https://lu.ma/some-event"

    def test_removes_www_prefix(self):
        url = "https://www.lu.ma/some-event"
        assert canonicalize_event_url(url) == "https://lu.ma/some-event"

    def test_lowercases_host(self):
        url = "https://LU.MA/some-event"
        assert canonicalize_event_url(url) == "https://lu.ma/some-event"

    def test_scheme_less_url(self):
        url = "lu.ma/some-event"
        assert canonicalize_event_url(url) == "https://lu.ma/some-event"

    def test_http_to_https(self):
        # canonicalize_event_url always produces https://
        url = "http://lu.ma/some-event"
        assert canonicalize_event_url(url) == "https://lu.ma/some-event"

    def test_no_hostname_raises(self):
        with pytest.raises(ConfigError):
            canonicalize_event_url("")


# =========================================================================
# extract_urls_from_description
# =========================================================================

class TestExtractUrlsFromDescription:
    """Test URL extraction from event page HTML."""

    def test_arxiv_classified_as_paper(self):
        html = '<a href="https://arxiv.org/abs/2301.07041">Paper</a>'
        papers, suppl = extract_urls_from_description(html)
        assert len(papers) == 1
        assert "arxiv.org" in papers[0]
        assert len(suppl) == 0

    def test_pdf_classified_as_paper(self):
        html = '<a href="https://example.com/research.pdf">PDF</a>'
        papers, suppl = extract_urls_from_description(html)
        assert len(papers) == 1
        assert papers[0].endswith(".pdf")

    def test_blog_classified_as_supplementary(self):
        html = '<a href="https://blog.google/ai-research">Blog</a>'
        papers, suppl = extract_urls_from_description(html)
        assert len(papers) == 0
        assert len(suppl) == 1

    def test_mixed_urls(self):
        html = """
        <div>
            <a href="https://arxiv.org/abs/2301.07041">Paper</a>
            <a href="https://blog.google/research">Blog</a>
            <a href="https://example.com/paper.pdf">PDF</a>
        </div>
        """
        papers, suppl = extract_urls_from_description(html)
        assert len(papers) == 2
        assert len(suppl) == 1

    def test_mailto_ignored(self):
        html = '<a href="mailto:test@example.com">Email</a>'
        papers, suppl = extract_urls_from_description(html)
        assert len(papers) == 0
        assert len(suppl) == 0

    def test_javascript_ignored(self):
        html = '<a href="javascript:void(0)">Click</a>'
        papers, suppl = extract_urls_from_description(html)
        assert len(papers) == 0
        assert len(suppl) == 0

    def test_relative_path_ignored(self):
        html = '<a href="/some/path">Link</a>'
        papers, suppl = extract_urls_from_description(html)
        assert len(papers) == 0
        assert len(suppl) == 0

    def test_fragment_only_ignored(self):
        html = '<a href="#section">Jump</a>'
        papers, suppl = extract_urls_from_description(html)
        assert len(papers) == 0
        assert len(suppl) == 0

    def test_deduplication(self):
        html = """
        <a href="https://arxiv.org/abs/2301.07041">Paper1</a>
        <a href="https://arxiv.org/abs/2301.07041">Paper2</a>
        """
        papers, suppl = extract_urls_from_description(html)
        assert len(papers) == 1

    def test_scheme_less_url_gets_https(self):
        html = '<a href="arxiv.org/abs/2301.07041">Paper</a>'
        papers, suppl = extract_urls_from_description(html)
        assert len(papers) == 1
        assert papers[0].startswith("https://")

    def test_arxiv_subdomain_classified_as_paper(self):
        html = '<a href="https://browse.arxiv.org/abs/2301.07041">Paper</a>'
        papers, suppl = extract_urls_from_description(html)
        assert len(papers) == 1

    def test_www_arxiv_classified_as_paper(self):
        html = '<a href="https://www.arxiv.org/abs/2301.07041">Paper</a>'
        papers, suppl = extract_urls_from_description(html)
        assert len(papers) == 1

    def test_empty_html(self):
        papers, suppl = extract_urls_from_description("")
        assert papers == []
        assert suppl == []


# =========================================================================
# Timezone-aware datetime parsing
# =========================================================================

class TestParseDatetime:
    """Test ISO datetime parsing with timezone handling."""

    def test_utc_z_suffix(self):
        dt = _parse_iso_datetime("2025-01-15T18:00:00Z")
        assert dt.tzinfo is not None
        assert dt.utcoffset() == timedelta(0)

    def test_explicit_offset(self):
        dt = _parse_iso_datetime("2025-01-15T10:00:00-08:00")
        assert dt.tzinfo is not None
        assert dt.utcoffset() == timedelta(hours=-8)

    def test_naive_gets_default_tz(self):
        dt = _parse_iso_datetime("2025-01-15T18:00:00")
        assert dt.tzinfo is not None
        # Should be America/Los_Angeles
        assert dt.tzinfo == ZoneInfo("America/Los_Angeles")

    def test_invalid_raises(self):
        with pytest.raises(Exception):
            _parse_iso_datetime("not-a-date")


# =========================================================================
# Event filtering (Paper Club title match)
# =========================================================================

class TestEventFiltering:
    """Test that events are filtered by 'Paper Club' in title."""

    def test_json_filter_case_insensitive(self):
        html = """
        <script type="application/json">
        [
            {"name": "PAPER CLUB: Attention Is All You Need",
             "start_at": "2025-01-10T18:00:00Z",
             "url": "https://lu.ma/pc-attention"},
            {"name": "Social Meetup",
             "start_at": "2025-01-11T18:00:00Z",
             "url": "https://lu.ma/meetup"}
        ]
        </script>
        """
        events = extract_events_from_json(html, "Paper Club")
        assert len(events) == 1
        assert "Attention" in events[0].title

    def test_json_filter_partial_match(self):
        html = """
        <script type="application/json">
        [
            {"name": "Latent Space Paper Club #42",
             "start_at": "2025-01-10T18:00:00Z",
             "url": "https://lu.ma/pc-42"}
        ]
        </script>
        """
        events = extract_events_from_json(html, "Paper Club")
        assert len(events) == 1

    def test_json_no_match(self):
        html = """
        <script type="application/json">
        [
            {"name": "Random Event",
             "start_at": "2025-01-10T18:00:00Z",
             "url": "https://lu.ma/random"}
        ]
        </script>
        """
        events = extract_events_from_json(html, "Paper Club")
        assert len(events) == 0


# =========================================================================
# Timezone-aware comparison (past event selection)
# =========================================================================

class TestTimezoneAwareComparison:
    """Test that timezone-aware comparison correctly selects past events."""

    def test_past_event_selected(self):
        """Events in the past should be selected."""
        past_dt = datetime(2020, 1, 1, tzinfo=timezone.utc)
        future_dt = datetime(2099, 1, 1, tzinfo=timezone.utc)

        events = [
            PaperClubEvent(
                title="Past Paper Club",
                date=past_dt,
                event_url="https://lu.ma/past",
            ),
            PaperClubEvent(
                title="Future Paper Club",
                date=future_dt,
                event_url="https://lu.ma/future",
            ),
        ]

        now = datetime.now(timezone.utc)
        past_events = [e for e in events if e.date < now]
        assert len(past_events) == 1
        assert past_events[0].title == "Past Paper Club"

    def test_different_tz_comparison(self):
        """Events in different timezones should compare correctly."""
        # Same instant: UTC midnight = PST 4pm previous day
        utc_time = datetime(2025, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
        pst_time = datetime(
            2025, 1, 14, 16, 0, 0,
            tzinfo=ZoneInfo("America/Los_Angeles"),
        )
        # These represent the same instant
        assert utc_time == pst_time

    def test_most_recent_past_selected(self):
        """When multiple past events exist, the most recent should come last."""
        events = [
            PaperClubEvent(
                title="Old",
                date=datetime(2024, 6, 1, tzinfo=timezone.utc),
                event_url="https://lu.ma/old",
            ),
            PaperClubEvent(
                title="Newer",
                date=datetime(2024, 12, 1, tzinfo=timezone.utc),
                event_url="https://lu.ma/newer",
            ),
            PaperClubEvent(
                title="Newest",
                date=datetime(2025, 1, 1, tzinfo=timezone.utc),
                event_url="https://lu.ma/newest",
            ),
        ]
        sorted_events = sorted(events, key=lambda e: e.date)
        most_recent = sorted_events[-1]
        assert most_recent.title == "Newest"


# =========================================================================
# No paper URLs logs WARNING
# =========================================================================

class TestNoPaperUrlsWarning:
    """Test that missing paper URLs triggers a WARNING log."""

    @responses.activate
    def test_no_paper_urls_logs_warning(self, caplog):
        """When event page has no paper URLs, a WARNING should be logged."""
        config = LumaConfig(
            calendar_url="https://lu.ma/ls",
            event_filter="Paper Club",
        )

        # Calendar page with one Paper Club event (past date)
        calendar_html = """
        <html>
        <script type="application/json">
        [
            {"name": "Paper Club: No Links",
             "start_at": "2020-01-10T18:00:00Z",
             "url": "https://lu.ma/no-links"}
        ]
        </script>
        </html>
        """

        # Event page with no paper links at all
        event_html = """
        <html><body>
        <p>This event has no paper links.</p>
        </body></html>
        """

        responses.add(
            responses.GET,
            "https://lu.ma/ls",
            body=calendar_html,
            status=200,
        )
        responses.add(
            responses.GET,
            "https://lu.ma/no-links",
            body=event_html,
            status=200,
        )

        with caplog.at_level(logging.WARNING, logger="src.scraper"):
            events = scrape_events(config, limit=1)

        assert len(events) == 1
        assert events[0].paper_urls == []
        assert any(
            "No paper URLs" in record.message
            for record in caplog.records
        )


# =========================================================================
# scrape_events with mocked HTTP
# =========================================================================

class TestScrapeEvents:
    """Integration-level tests for scrape_events with mocked HTTP."""

    @responses.activate
    def test_rejects_non_luma_calendar_url(self):
        config = LumaConfig(
            calendar_url="https://evil.com/events",
            event_filter="Paper Club",
        )
        with pytest.raises(ConfigError, match="lu.ma"):
            scrape_events(config)

    @responses.activate
    def test_full_scrape_flow(self):
        """End-to-end: calendar -> event page -> extract URLs."""
        config = LumaConfig(
            calendar_url="https://lu.ma/ls",
            event_filter="Paper Club",
        )

        calendar_html = """
        <html>
        <script type="application/json">
        [
            {"name": "Paper Club: Transformers",
             "start_at": "2020-06-15T18:00:00Z",
             "url": "https://lu.ma/pc-transformers"}
        ]
        </script>
        </html>
        """

        event_html = """
        <html><body>
        <a href="https://arxiv.org/abs/1706.03762">Attention paper</a>
        <a href="https://blog.google/ai-transformers">Blog post</a>
        </body></html>
        """

        responses.add(
            responses.GET,
            "https://lu.ma/ls",
            body=calendar_html,
            status=200,
        )
        responses.add(
            responses.GET,
            "https://lu.ma/pc-transformers",
            body=event_html,
            status=200,
        )

        events = scrape_events(config, limit=1)
        assert len(events) == 1
        event = events[0]
        assert event.title == "Paper Club: Transformers"
        assert len(event.paper_urls) == 1
        assert "arxiv.org" in event.paper_urls[0]
        assert len(event.supplementary_urls) == 1

    @responses.activate
    def test_skips_non_luma_event_urls(self):
        """Events with non-Luma URLs should be filtered out."""
        config = LumaConfig(
            calendar_url="https://lu.ma/ls",
            event_filter="Paper Club",
        )

        calendar_html = """
        <html>
        <script type="application/json">
        [
            {"name": "Paper Club: External",
             "start_at": "2020-06-15T18:00:00Z",
             "url": "https://evil.com/fake-event"}
        ]
        </script>
        </html>
        """

        responses.add(
            responses.GET,
            "https://lu.ma/ls",
            body=calendar_html,
            status=200,
        )

        # Should not raise, but return empty (non-Luma URL skipped)
        # Actually this raises NoEventsFoundError since no past events pass
        # the filter... but the non-Luma URL event is in the list.
        # The event will be in events list but skipped during enrichment.
        # Let's add a valid Luma event too.
        # Actually with only the evil.com event, enriched will be empty
        # so scrape_events returns [].
        events = scrape_events(config, limit=1)
        assert len(events) == 0

    @responses.activate
    def test_no_events_raises(self):
        config = LumaConfig(
            calendar_url="https://lu.ma/ls",
            event_filter="Paper Club",
        )
        calendar_html = "<html><body>No events here</body></html>"
        responses.add(
            responses.GET,
            "https://lu.ma/ls",
            body=calendar_html,
            status=200,
        )
        with pytest.raises(NoEventsFoundError):
            scrape_events(config)

    @responses.activate
    def test_selects_most_recent_past_event(self):
        """With multiple past events, limit=1 should return most recent."""
        config = LumaConfig(
            calendar_url="https://lu.ma/ls",
            event_filter="Paper Club",
        )

        calendar_html = """
        <html>
        <script type="application/json">
        [
            {"name": "Paper Club: Old",
             "start_at": "2020-01-10T18:00:00Z",
             "url": "https://lu.ma/old-event"},
            {"name": "Paper Club: Recent",
             "start_at": "2020-06-15T18:00:00Z",
             "url": "https://lu.ma/recent-event"}
        ]
        </script>
        </html>
        """

        event_html = """
        <html><body>
        <a href="https://arxiv.org/abs/2301.07041">Paper</a>
        </body></html>
        """

        responses.add(
            responses.GET,
            "https://lu.ma/ls",
            body=calendar_html,
            status=200,
        )
        # Only the recent event page should be fetched (limit=1)
        responses.add(
            responses.GET,
            "https://lu.ma/recent-event",
            body=event_html,
            status=200,
        )

        events = scrape_events(config, limit=1)
        assert len(events) == 1
        assert events[0].title == "Paper Club: Recent"


# =========================================================================
# get_latest_paper_club_event
# =========================================================================

class TestGetLatestPaperClubEvent:
    """Test the convenience wrapper."""

    @responses.activate
    def test_returns_single_event(self):
        config = LumaConfig(
            calendar_url="https://lu.ma/ls",
            event_filter="Paper Club",
        )

        calendar_html = """
        <html>
        <script type="application/json">
        [
            {"name": "Paper Club: Test",
             "start_at": "2020-01-10T18:00:00Z",
             "url": "https://lu.ma/test-event"}
        ]
        </script>
        </html>
        """

        event_html = """
        <html><body>
        <a href="https://arxiv.org/abs/2301.07041">Paper</a>
        </body></html>
        """

        responses.add(
            responses.GET,
            "https://lu.ma/ls",
            body=calendar_html,
            status=200,
        )
        responses.add(
            responses.GET,
            "https://lu.ma/test-event",
            body=event_html,
            status=200,
        )

        event = get_latest_paper_club_event(config)
        assert isinstance(event, PaperClubEvent)
        assert event.title == "Paper Club: Test"
        assert event.date.tzinfo is not None


# =========================================================================
# HTML fallback (extract_event_cards)
# =========================================================================

class TestExtractEventCards:
    """Test HTML-based event card extraction."""

    def test_extracts_event_with_time_element(self):
        from bs4 import BeautifulSoup as BS
        html = """
        <div>
            <a href="/pc-test">
                <time datetime="2020-06-15T18:00:00Z">Jun 15</time>
                Paper Club: Test Event
            </a>
        </div>
        """
        soup = BS(html, "html.parser")
        events = extract_event_cards(soup, "Paper Club")
        assert len(events) == 1
        assert events[0].event_url == "https://lu.ma/pc-test"
        assert events[0].date.tzinfo is not None

    def test_skips_non_matching_titles(self):
        from bs4 import BeautifulSoup as BS
        html = """
        <div>
            <a href="/meetup">
                <time datetime="2020-06-15T18:00:00Z">Jun 15</time>
                Social Meetup
            </a>
        </div>
        """
        soup = BS(html, "html.parser")
        events = extract_event_cards(soup, "Paper Club")
        assert len(events) == 0
