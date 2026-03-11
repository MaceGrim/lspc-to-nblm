"""Download paper PDFs from arXiv or direct URLs."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from src.config import SecurityConfig
from src.errors import PaperDownloadError
from src.scraper import canonicalize_paper_url

_USER_AGENT = "LSPC-Pipeline/1.0 (academic-podcast-generator)"


def _is_allowed_domain(netloc_or_hostname: str, allowed: list[str]) -> bool:
    """Check if hostname matches allowed domain list (exact or subdomain).

    Uses parsed.hostname (not netloc) to avoid port/userinfo bypass.
    """
    parsed = urlparse(f"https://{netloc_or_hostname}")
    host = (parsed.hostname or netloc_or_hostname).lower().removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in allowed)


def download_paper(url: str, tmp_dir: Path, security: SecurityConfig) -> Path:
    """Download a single paper PDF.

    Parameters
    ----------
    url : str
        arXiv abs/pdf URL or direct PDF URL.
    tmp_dir : Path
        Directory to save the downloaded PDF.
    security : SecurityConfig
        Security settings (allowed domains, max bytes, HTTPS enforcement).

    Returns
    -------
    Path
        Path to the downloaded PDF file.

    Raises
    ------
    PaperDownloadError
        On any download failure.
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    canonical = canonicalize_paper_url(url)

    # Validate domain
    parsed = urlparse(canonical)
    if security.enforce_https and parsed.scheme != "https":
        raise PaperDownloadError(url, 0, "HTTPS required")
    if not _is_allowed_domain(parsed.netloc, security.allowed_domains):
        raise PaperDownloadError(url, 0, f"Domain not allowed: {parsed.netloc}")

    # Determine download URL and filename
    host = parsed.netloc.lower().removeprefix("www.")
    if host == "arxiv.org" or host.endswith(".arxiv.org"):
        arxiv_id = canonical.split("/abs/")[-1]
        download_url = f"https://arxiv.org/pdf/{arxiv_id}"
        filename = f"{arxiv_id}.pdf"
    else:
        download_url = canonical
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]
        filename = f"{url_hash}.pdf"

    output_path = tmp_dir / filename
    try:
        resp = requests.get(
            download_url,
            timeout=30,
            stream=True,
            allow_redirects=True,
            headers={
                "Accept": "application/pdf",
                "User-Agent": _USER_AGENT,
            },
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        status = getattr(exc.response, "status_code", 0) if hasattr(exc, "response") else 0
        raise PaperDownloadError(url, status, str(exc)) from exc

    # Validate final URL after redirects
    final_parsed = urlparse(resp.url)
    if not _is_allowed_domain(final_parsed.netloc, security.allowed_domains):
        raise PaperDownloadError(
            url, 0, f"Redirect to disallowed domain: {final_parsed.netloc}"
        )
    if security.enforce_https and final_parsed.scheme != "https":
        raise PaperDownloadError(
            url, 0, f"Redirect downgraded to {final_parsed.scheme}"
        )

    # Validate content type for arXiv
    content_type = resp.headers.get("content-type", "")
    if host == "arxiv.org" and "pdf" not in content_type.lower():
        raise PaperDownloadError(url, 0, f"Unexpected content type: {content_type}")

    # Stream with hard byte limit
    bytes_read = 0
    with open(output_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            bytes_read += len(chunk)
            if bytes_read > security.max_download_bytes:
                output_path.unlink(missing_ok=True)
                raise PaperDownloadError(
                    url, 413,
                    f"Download exceeded {security.max_download_bytes} bytes",
                )
            f.write(chunk)

    # Validate PDF magic bytes
    with open(output_path, "rb") as f:
        magic = f.read(5)
    if magic != b"%PDF-":
        output_path.unlink(missing_ok=True)
        raise PaperDownloadError(url, 0, "File is not a valid PDF")

    return output_path


def download_all_papers(
    urls: list[str], tmp_dir: Path, security: SecurityConfig
) -> list[Path]:
    """Download all paper PDFs. Returns list of paths.

    Enforces a 3-second delay between requests for arXiv rate limiting.
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, url in enumerate(urls):
        if i > 0:
            time.sleep(3)  # arXiv rate limiting
        path = download_paper(url, tmp_dir, security)
        paths.append(path)
    return paths
