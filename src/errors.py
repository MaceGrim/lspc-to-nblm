"""Custom exception hierarchy for the LSPC pipeline."""


class LSPCError(Exception):
    """Base exception for all pipeline errors."""


class ConfigError(LSPCError):
    """Invalid or missing configuration."""


class ScrapingError(LSPCError):
    """Failed to scrape Luma events."""


class NoEventsFoundError(ScrapingError):
    """No matching Paper Club events found."""


class PaperDownloadError(LSPCError):
    """Failed to download a paper PDF."""

    def __init__(self, url: str, status_code: int, message: str):
        self.url = url
        self.status_code = status_code
        super().__init__(f"Failed to download {url}: {status_code} {message}")


class YouTubeDiscoveryError(LSPCError):
    """yt-dlp failed to list channel videos."""


class VideoNotFoundError(LSPCError):
    """No matching YouTube video found."""


class PodcastGenerationError(LSPCError):
    """NotebookLM podcast generation failed."""


class FallbackConfigError(LSPCError):
    """Missing env vars for fallback pipeline."""

    def __init__(self, missing_vars: list[str]):
        self.missing_vars = missing_vars
        super().__init__(f"Missing env vars: {', '.join(missing_vars)}")


class PublishError(LSPCError):
    """Git push failed."""
