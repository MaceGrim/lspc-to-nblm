"""Download supplementary content (blog posts, threads) as text files."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from urllib.parse import urlparse

import requests
import trafilatura

logger = logging.getLogger("lspc.supplementary")

USER_AGENT = "lspc-to-nblm/1.0 (+https://github.com/lspc-to-nblm)"

# Domains / patterns that indicate a paper rather than supplementary content.
_PAPER_HOSTS = {"arxiv.org"}
_PAPER_EXTENSIONS = {".pdf"}


def _is_paper_url(url: str) -> bool:
    """Return True if *url* looks like a paper (arXiv or PDF)."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if any(host == d or host.endswith(f".{d}") for d in _PAPER_HOSTS):
        return True
    if any(parsed.path.lower().endswith(ext) for ext in _PAPER_EXTENSIONS):
        return True
    return False


def download_supplementary(
    urls: list[str],
    tmp_dir: Path,
    *,
    max_bytes: int = 5_000_000,
) -> list[Path]:
    """Fetch supplementary web content and save as text files.

    Parameters
    ----------
    urls:
        List of URLs to fetch.
    tmp_dir:
        Directory to save extracted text files into.
    max_bytes:
        Maximum bytes to download per URL before truncating.

    Returns
    -------
    list[Path]
        Paths to successfully downloaded text files.
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": USER_AGENT}
    paths: list[Path] = []

    for url in urls:
        if _is_paper_url(url):
            logger.debug("Skipping paper URL: %s", url)
            continue

        try:
            # Optional HEAD request — failure is non-fatal
            try:
                requests.head(url, timeout=10, headers=headers, allow_redirects=True)
            except requests.RequestException:
                pass  # Continue to GET

            # Stream the GET response with byte cap
            resp = requests.get(
                url,
                timeout=15,
                headers=headers,
                stream=True,
                allow_redirects=True,
            )
            resp.raise_for_status()

            content_parts: list[bytes] = []
            bytes_read = 0
            for chunk in resp.iter_content(chunk_size=8192):
                bytes_read += len(chunk)
                if bytes_read > max_bytes:
                    logger.warning("Supplementary URL too large, truncating: %s", url)
                    break
                content_parts.append(chunk)

            downloaded = b"".join(content_parts).decode("utf-8", errors="replace")
            if not downloaded:
                logger.warning("Empty response from supplementary URL: %s", url)
                continue

            text = trafilatura.extract(downloaded)
            if not text:
                logger.warning("No extractable text from supplementary URL: %s", url)
                continue

            url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]
            out_path = tmp_dir / f"{url_hash}.txt"
            out_path.write_text(text, encoding="utf-8")
            paths.append(out_path)
            logger.info("Saved supplementary content: %s → %s", url, out_path)

        except Exception as exc:
            logger.warning("Failed to fetch supplementary URL %s: %s", url, exc)
            continue

    return paths
