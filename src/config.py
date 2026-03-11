"""Load and validate config.yaml into typed dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.errors import ConfigError


@dataclass
class LumaConfig:
    calendar_url: str = "https://lu.ma/ls"
    event_filter: str = "Paper Club"


@dataclass
class YouTubeConfig:
    channel_url: str = "https://www.youtube.com/@LatentSpaceTV"
    match_window_days: int = 7
    playlist_depth: int = 30


@dataclass
class NotebookLMConfig:
    prompt: str = (
        "Deeply explain this paper, covering motivation, methodology, "
        "key results, and implications. Make it accessible to a technical "
        "audience that may not be domain experts."
    )
    format: str = "deep-dive"
    length: str = "standard"


@dataclass
class FallbackConfig:
    enabled: bool = True
    llm_model: str = "gpt-4o-mini"
    tts_model: str = "tts-1"
    tts_voices: list[str] = field(default_factory=lambda: ["alloy", "echo"])
    chunk_max_chars: int = 4096


@dataclass
class RSSConfig:
    title: str = "Latent Space Paper Club Deep Dives"
    description: str = ""
    author: str = "Mason Grimshaw"
    base_url: str = ""  # required, no default
    owner_name: str = "Mason Grimshaw"
    owner_email: str = ""  # required, no default
    category: str = "Technology"
    subcategory: str = "Tech News"
    explicit: bool = False


@dataclass
class ErrorConfig:
    max_retries: int = 3
    backoff_base: int = 60


@dataclass
class ScheduleConfig:
    run_days: list[str] = field(
        default_factory=lambda: [
            "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday",
        ]
    )


@dataclass
class SecurityConfig:
    allowed_domains: list[str] = field(
        default_factory=lambda: [
            "arxiv.org", "openai.com", "anthropic.com", "ai.meta.com",
            "blog.google", "deepmind.google", "huggingface.co",
            "github.com", "github.io",
        ]
    )
    max_download_bytes: int = 100_000_000  # 100 MB
    max_supplementary_bytes: int = 5_000_000  # 5 MB
    enforce_https: bool = True


@dataclass
class PipelineConfig:
    luma: LumaConfig
    youtube: YouTubeConfig
    notebooklm: NotebookLMConfig
    fallback: FallbackConfig
    rss: RSSConfig
    errors: ErrorConfig
    schedule: ScheduleConfig
    security: SecurityConfig


# Map of top-level config keys to their dataclass types.
_SECTION_CLASSES: dict[str, type] = {
    "luma": LumaConfig,
    "youtube": YouTubeConfig,
    "notebooklm": NotebookLMConfig,
    "fallback": FallbackConfig,
    "rss": RSSConfig,
    "errors": ErrorConfig,
    "schedule": ScheduleConfig,
    "security": SecurityConfig,
}

# Fields that must be provided (no usable default).
_REQUIRED_FIELDS: dict[str, list[str]] = {
    "rss": ["base_url", "owner_email"],
}


def _build_section(section_key: str, raw: dict | None) -> object:
    """Instantiate a config section dataclass from a raw dict.

    Uses defaults from the dataclass when keys are absent in the YAML.
    """
    cls = _SECTION_CLASSES[section_key]
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"Config section '{section_key}' must be a mapping, "
            f"got {type(raw).__name__}"
        )

    # Check for unknown keys
    valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
    unknown = set(raw.keys()) - valid_fields
    if unknown:
        raise ConfigError(
            f"Unknown fields in '{section_key}': {', '.join(sorted(unknown))}"
        )

    return cls(**raw)


def load_config(path: str | Path = "config.yaml") -> PipelineConfig:
    """Read *path* and return a validated PipelineConfig.

    Raises
    ------
    FileNotFoundError
        If the config file does not exist (message references
        config.example.yaml).
    ConfigError
        If the YAML is invalid or required fields are missing.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. "
            "Copy config.example.yaml to config.yaml and fill in your settings."
        )

    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(
            f"Config file must contain a YAML mapping, got {type(data).__name__}"
        )

    # Build each section, using defaults when a section is absent.
    sections: dict[str, object] = {}
    for key in _SECTION_CLASSES:
        sections[key] = _build_section(key, data.get(key))

    # Validate required fields that have no usable default.
    errors: list[str] = []
    for section_key, required in _REQUIRED_FIELDS.items():
        section = sections[section_key]
        for field_name in required:
            value = getattr(section, field_name)
            if not value:
                errors.append(f"{section_key}.{field_name}")

    if errors:
        raise ConfigError(
            f"Missing required config fields: {', '.join(errors)}"
        )

    return PipelineConfig(**sections)  # type: ignore[arg-type]
