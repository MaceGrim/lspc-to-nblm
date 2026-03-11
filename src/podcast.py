"""Generate podcast episodes via NotebookLM and extract text from PDFs."""

from __future__ import annotations

import concurrent.futures
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from src.config import NotebookLMConfig
from src.errors import PodcastGenerationError
from src.scraper import PaperClubEvent, canonicalize_event_url

logger = logging.getLogger(__name__)


@dataclass
class ContentBundle:
    """All content needed to generate a podcast episode."""

    paper_paths: list[Path] = field(default_factory=list)
    audio_path: Path | None = None
    supplementary_paths: list[Path] = field(default_factory=list)


def generate_episode_slug(event: PaperClubEvent) -> str:
    """Generate a stable episode slug: {date_YYYYMMDD}-{sha256(event_url)[:8]}.

    Uses the canonicalized event URL for hashing to ensure consistency.
    """
    date_str = event.date.strftime("%Y%m%d")
    canonical = canonicalize_event_url(event.event_url)
    url_hash = hashlib.sha256(canonical.encode()).hexdigest()[:8]
    return f"{date_str}-{url_hash}"


def generate_podcast(
    bundle: ContentBundle,
    event: PaperClubEvent,
    config: NotebookLMConfig,
) -> Path:
    """Generate a podcast episode via NotebookLM.

    Creates a NotebookLM notebook, uploads all sources, generates audio,
    and downloads the result MP3.

    Parameters
    ----------
    bundle : ContentBundle
        Paper PDFs, audio file, and supplementary text files.
    event : PaperClubEvent
        The event this episode covers.
    config : NotebookLMConfig
        NotebookLM generation settings (prompt, format, length).

    Returns
    -------
    Path
        Path to the downloaded MP3 file at docs/episodes/{slug}.mp3.

    Raises
    ------
    PodcastGenerationError
        If NotebookLM client init, generation, or download fails.
    """
    from notebooklm import NotebookLMClient

    slug = generate_episode_slug(event)

    # Initialize client
    try:
        client = NotebookLMClient.from_storage()
    except Exception as e:
        err_msg = str(e).lower()
        if "auth" in err_msg or "credential" in err_msg or "login" in err_msg:
            logger.error(
                "NotebookLM auth expired. Run `notebooklm login` to re-authenticate."
            )
            Path("tmp").mkdir(exist_ok=True)
            Path("tmp/notebooklm_auth_expired").touch()
        raise PodcastGenerationError(
            f"NotebookLM client init failed: {e}"
        ) from e

    # Create notebook
    notebook_title = f"LSPC: {event.title} ({event.date.strftime('%Y-%m-%d')})"
    nb = client.notebooks.create(notebook_title)

    try:
        # Upload sources
        for paper_path in bundle.paper_paths:
            nb.sources.add_file(str(paper_path))
        if bundle.audio_path:
            nb.sources.add_file(str(bundle.audio_path))
        for supp_path in bundle.supplementary_paths:
            nb.sources.add_text(supp_path.read_text())

        # Generate audio with timeout (no context manager to avoid blocking)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            nb.artifacts.generate_audio,
            format=config.format,
            length=config.length,
            focus=config.prompt,
        )
        try:
            audio = future.result(timeout=1800)  # 30 min max
        except concurrent.futures.TimeoutError:
            raise PodcastGenerationError(
                "NotebookLM generation timed out after 30 minutes"
            )
        finally:
            executor.shutdown(wait=False)

        # Download MP3
        output_path = Path(f"docs/episodes/{slug}.mp3")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        audio.download_audio(str(output_path))

        # Clear auth-expired flag on success
        auth_flag = Path("tmp/notebooklm_auth_expired")
        if auth_flag.exists():
            auth_flag.unlink(missing_ok=True)

        return output_path
    finally:
        # Always cleanup notebook
        try:
            client.notebooks.delete(nb.id)
        except Exception:
            logger.warning("Failed to delete NotebookLM notebook: %s", nb.id)


def extract_text_from_pdfs(
    paper_paths: list[Path], max_chars: int = 100_000
) -> str:
    """Extract text from PDFs using pymupdf.

    Distributes a per-paper character budget and stops extracting pages
    once the budget is reached.

    Parameters
    ----------
    paper_paths : list[Path]
        Paths to PDF files.
    max_chars : int
        Maximum total characters to extract across all papers.

    Returns
    -------
    str
        Extracted text from all papers, separated by "---".
    """
    import pymupdf

    texts: list[str] = []
    total_chars = 0
    per_paper_budget = max_chars // max(len(paper_paths), 1)

    for path in paper_paths:
        doc = pymupdf.open(str(path))
        try:
            pages_text: list[str] = []
            paper_chars = 0
            for page in doc:
                if paper_chars >= per_paper_budget or total_chars >= max_chars:
                    break
                page_text = page.get_text()
                pages_text.append(page_text)
                paper_chars += len(page_text)
                total_chars += len(page_text)
            texts.append("\n".join(pages_text))
        finally:
            doc.close()
        if total_chars >= max_chars:
            break

    return "\n\n---\n\n".join(texts)
