"""Scrape Luma calendar for Paper Club events and extract paper URLs."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from src.config import LumaConfig
from src.errors import ConfigError, NoEventsFoundError, ScrapingError

logger = logging.getLogger(__name__)

# Default timezone when Luma data lacks timezone info (Paper Club is Pacific).
_DEFAULT_TZ_NAME = "America/Los_Angeles"

_USER_AGENT = "lspc-pipeline/1.0 (podcast automation)"

_LUMA_API_BASE = "https://api.lu.ma"


@dataclass
class PaperClubEvent:
    """A single Paper Club event scraped from Luma."""

    title: str
    date: datetime  # timezone-aware, original TZ preserved
    event_url: str  # stable identifier, Luma event page URL
    paper_urls: list[str] = field(default_factory=list)
    supplementary_urls: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# URL canonicalization
# ---------------------------------------------------------------------------

def canonicalize_paper_url(url: str) -> str:
    """Normalize arXiv URLs to abs/ form, strip version for arXiv only.

    For all URLs: strip fragments, upgrade http->https. Query params are
    preserved for non-arXiv URLs (may be needed for signed/download URLs).
    URLs without a scheme default to https.
    """
    # Reject non-web schemes
    if url.startswith(("mailto:", "javascript:", "ftp:", "data:")):
        return url  # return as-is; downstream domain check will reject it

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    # arXiv: normalize to https://arxiv.org/abs/{id}
    if host == "arxiv.org" or host.endswith(".arxiv.org"):
        match = re.search(r"/(abs|pdf|html)/(\d{4}\.\d{4,5})(v\d+)?", parsed.path)
        if match:
            return f"https://arxiv.org/abs/{match.group(2)}"

    # Non-arXiv: strip fragment only, preserve query params, upgrade http
    scheme = "https" if parsed.scheme == "http" else parsed.scheme
    return urlunparse(parsed._replace(fragment="", scheme=scheme))


def canonicalize_event_url(url: str) -> str:
    """Normalize Luma event URLs for consistent keying in processed.json.

    Strips query params, fragments, trailing slashes, lowercases host,
    removes www. prefix. Requires a valid hostname.
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not host:
        raise ConfigError(f"Cannot canonicalize event URL without hostname: {url}")
    path = parsed.path.rstrip("/")
    return f"https://{host}{path}"


# ---------------------------------------------------------------------------
# URL extraction from event page HTML
# ---------------------------------------------------------------------------

def extract_urls_from_description(event_html: str) -> tuple[list[str], list[str]]:
    """Extract paper URLs and supplementary URLs from a Luma event page.

    Returns (paper_urls, supplementary_urls).
    paper_urls: URLs on arXiv, or PDFs on allowed domains.
    supplementary_urls: blog posts, research pages on allowed domains.
    Only accepts http/https URLs; ignores mailto:, javascript:, etc.
    """
    soup = BeautifulSoup(event_html, "html.parser")
    seen: set[str] = set()
    paper_urls: list[str] = []
    supplementary_urls: list[str] = []

    for link in soup.find_all("a", href=True):
        url = link["href"].strip()

        # Skip non-web schemes and relative paths
        if url.startswith(("mailto:", "javascript:", "ftp:", "data:", "#", "/")):
            continue

        # Accept scheme-less URLs by adding https://
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        if url in seen:
            continue
        seen.add(url)

        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")

        if host == "arxiv.org" or host.endswith(".arxiv.org"):
            paper_urls.append(url)
        elif parsed.path.endswith(".pdf"):
            paper_urls.append(url)
        else:
            supplementary_urls.append(url)

    return paper_urls, supplementary_urls


# ---------------------------------------------------------------------------
# Luma API helpers
# ---------------------------------------------------------------------------

def _ensure_aware(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware; default to America/Los_Angeles."""
    if dt.tzinfo is not None:
        return dt
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(_DEFAULT_TZ_NAME)
    except ImportError:
        tz = timezone.utc
    return dt.replace(tzinfo=tz)


def _parse_iso_datetime(raw: str) -> datetime:
    """Parse an ISO 8601 datetime string, ensuring timezone awareness."""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ScrapingError(f"Cannot parse datetime: {raw!r}") from exc
    return _ensure_aware(dt)


def _resolve_calendar_api_id(calendar_url: str, headers: dict) -> str:
    """Resolve a Luma calendar URL (e.g. https://lu.ma/ls) to a calendar API ID.

    Uses the Luma /url endpoint to look up calendar metadata by slug.
    """
    parsed = urlparse(calendar_url)
    slug = parsed.path.strip("/")
    if not slug:
        raise ConfigError(f"Cannot extract calendar slug from URL: {calendar_url}")

    resp = requests.get(
        f"{_LUMA_API_BASE}/url",
        params={"url": slug},
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    calendar = data.get("data", {}).get("calendar", {})
    api_id = calendar.get("api_id")
    if not api_id:
        raise ScrapingError(f"Could not resolve calendar API ID for slug: {slug}")
    return api_id


def _fetch_past_events_api(
    calendar_api_id: str, event_filter: str, headers: dict, limit: int = 20
) -> list[PaperClubEvent]:
    """Fetch past events from Luma API and filter by event_filter."""
    resp = requests.get(
        f"{_LUMA_API_BASE}/calendar/get-items",
        params={
            "calendar_api_id": calendar_api_id,
            "period": "past",
            "pagination_limit": limit,
        },
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    events: list[PaperClubEvent] = []
    filter_lower = event_filter.lower()

    for entry in data.get("entries", []):
        event_data = entry.get("event", {})
        name = event_data.get("name", "")
        start_at = event_data.get("start_at", "")
        url_slug = event_data.get("url", "")

        if not name or not start_at:
            continue

        if filter_lower not in name.lower():
            continue

        # Build full event URL from slug
        if url_slug and not url_slug.startswith("http"):
            event_url = f"https://lu.ma/{url_slug}"
        else:
            event_url = url_slug

        if not event_url:
            continue

        try:
            dt = _parse_iso_datetime(start_at)
        except ScrapingError:
            logger.warning("Skipping event with unparseable date: %s", name)
            continue

        events.append(PaperClubEvent(
            title=name,
            date=dt,
            event_url=event_url,
        ))

    return events


# ---------------------------------------------------------------------------
# HTML fallback (for when API is unavailable)
# ---------------------------------------------------------------------------

def parse_luma_json_data(data: dict | list, event_filter: str) -> list[PaperClubEvent]:
    """Extract events from Luma's embedded JSON/Next.js data."""
    events: list[PaperClubEvent] = []
    filter_lower = event_filter.lower()
    _find_events_recursive(data, filter_lower, events)
    return events


def _find_events_recursive(
    data: object, filter_lower: str, results: list[PaperClubEvent]
) -> None:
    """Walk a JSON structure looking for objects that look like Luma events."""
    if isinstance(data, dict):
        name = data.get("name") or data.get("title") or ""
        start = (
            data.get("start_at")
            or data.get("startDate")
            or data.get("start_date")
            or ""
        )
        url = data.get("url") or data.get("event_url") or ""

        if name and start and filter_lower in name.lower():
            if url and not url.startswith("http"):
                url = f"https://lu.ma/{url}"
            if url:
                try:
                    dt = _parse_iso_datetime(start)
                    results.append(PaperClubEvent(
                        title=name,
                        date=dt,
                        event_url=url,
                    ))
                except ScrapingError:
                    logger.warning(
                        "Skipping event with unparseable date: %s", name
                    )

        for v in data.values():
            _find_events_recursive(v, filter_lower, results)

    elif isinstance(data, list):
        for item in data:
            _find_events_recursive(item, filter_lower, results)


def extract_events_from_json(
    html: str, event_filter: str
) -> list[PaperClubEvent]:
    """Extract events from embedded JSON/JSON-LD in Luma page."""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all(
        "script", type=["application/json", "application/ld+json"]
    ):
        try:
            data = json.loads(script.string or "")
            events = parse_luma_json_data(data, event_filter)
            if events:
                return events
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return []


def extract_event_cards(
    soup: BeautifulSoup, event_filter: str
) -> list[PaperClubEvent]:
    """Fallback: parse HTML event cards from Luma calendar page."""
    events: list[PaperClubEvent] = []
    filter_lower = event_filter.lower()

    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue

        text = link.get_text(strip=True)
        if not text or filter_lower not in text.lower():
            continue

        if href.startswith("/"):
            event_url = f"https://lu.ma{href}"
        elif not href.startswith("http"):
            event_url = f"https://lu.ma/{href}"
        else:
            event_url = href

        dt = _extract_date_from_context(link)
        if dt is None:
            continue

        events.append(PaperClubEvent(
            title=text,
            date=dt,
            event_url=event_url,
        ))

    return events


def _extract_date_from_context(element) -> datetime | None:
    """Try to find a date string near an HTML element."""
    parent = element.parent
    if parent:
        time_el = parent.find("time")
        if time_el and time_el.get("datetime"):
            try:
                return _parse_iso_datetime(time_el["datetime"])
            except ScrapingError:
                pass
        text = parent.get_text()
        iso_match = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text)
        if iso_match:
            try:
                return _parse_iso_datetime(iso_match.group(0))
            except ScrapingError:
                pass
    return None


# ---------------------------------------------------------------------------
# Main scraping entry points
# ---------------------------------------------------------------------------

def get_latest_paper_club_event(config: LumaConfig) -> PaperClubEvent:
    """Fetch the Luma calendar and return the most recent past Paper Club event.

    Raises NoEventsFoundError if no matching events are found.
    """
    events = scrape_events(config, limit=1)
    if not events:
        raise NoEventsFoundError("No Paper Club events found on calendar page")
    return events[0]


def scrape_events(
    config: LumaConfig, limit: int = 1
) -> list[PaperClubEvent]:
    """Scrape lu.ma calendar for recent past Paper Club events.

    Uses the Luma API (period=past) to get past events. Falls back to
    scraping the HTML page if the API is unavailable.

    Returns up to *limit* enriched events (with paper URLs), most recent first.
    """
    # Validate calendar URL host (SSRF protection)
    cal_host = urlparse(config.calendar_url).hostname or ""
    if not (cal_host == "lu.ma" or cal_host.endswith(".lu.ma")):
        raise ConfigError(
            f"calendar_url must be on lu.ma, got: {cal_host}"
        )

    headers = {"User-Agent": _USER_AGENT}

    # --- Primary: Luma API for past events ---
    events: list[PaperClubEvent] = []
    try:
        calendar_api_id = _resolve_calendar_api_id(
            config.calendar_url, headers
        )
        events = _fetch_past_events_api(
            calendar_api_id, config.event_filter, headers, limit=20
        )
        logger.info(
            "Luma API returned %d past %s events",
            len(events), config.event_filter,
        )
    except (requests.RequestException, ScrapingError) as exc:
        logger.warning("Luma API unavailable, falling back to HTML: %s", exc)

    # --- Fallback: scrape HTML page ---
    if not events:
        resp = requests.get(config.calendar_url, timeout=30, headers=headers)
        resp.raise_for_status()

        events = extract_events_from_json(resp.text, config.event_filter)
        if not events:
            soup = BeautifulSoup(resp.text, "html.parser")
            events = extract_event_cards(soup, config.event_filter)

    if not events:
        raise NoEventsFoundError(
            "No Paper Club events found on calendar page"
        )

    # API returns past events already, but filter just in case
    now = datetime.now(timezone.utc)
    past_events = sorted(
        [e for e in events if e.date < now],
        key=lambda e: e.date,
    )

    if not past_events:
        raise NoEventsFoundError("No past Paper Club events found")

    # Take the N most recent
    candidates = past_events[-limit:]

    # Fetch full event pages and extract URLs
    enriched: list[PaperClubEvent] = []
    for event in candidates:
        ev_host = urlparse(event.event_url).hostname or ""
        if not (ev_host == "lu.ma" or ev_host.endswith(".lu.ma")):
            logger.warning(
                "Skipping event with non-Luma URL: %s", event.event_url
            )
            continue
        try:
            event_page = requests.get(
                event.event_url, timeout=30, headers=headers
            )
            event_page.raise_for_status()
            event.paper_urls, event.supplementary_urls = (
                extract_urls_from_description(event_page.text)
            )
        except requests.RequestException as exc:
            logger.warning(
                "Failed to fetch event page %s: %s", event.event_url, exc
            )
            continue

        if not event.paper_urls:
            logger.warning(
                "No paper URLs found for event: %s", event.title
            )

        enriched.append(event)

    return enriched
