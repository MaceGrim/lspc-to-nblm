"""Tests for src.config — loading, validation, and defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import (
    ErrorConfig,
    FallbackConfig,
    LumaConfig,
    NotebookLMConfig,
    PipelineConfig,
    RSSConfig,
    ScheduleConfig,
    SecurityConfig,
    YouTubeConfig,
    load_config,
)
from src.errors import ConfigError


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestLoadValidConfig:
    """Loading a well-formed config.yaml returns a fully populated PipelineConfig."""

    def test_returns_pipeline_config(self, valid_config_path: Path):
        cfg = load_config(valid_config_path)
        assert isinstance(cfg, PipelineConfig)

    def test_rss_required_fields(self, valid_config_path: Path):
        cfg = load_config(valid_config_path)
        assert cfg.rss.base_url == "https://example.github.io/podcast"
        assert cfg.rss.owner_email == "test@example.com"

    def test_default_luma(self, valid_config_path: Path):
        cfg = load_config(valid_config_path)
        assert isinstance(cfg.luma, LumaConfig)
        assert cfg.luma.calendar_url == "https://lu.ma/ls"
        assert cfg.luma.event_filter == "Paper Club"

    def test_default_youtube(self, valid_config_path: Path):
        cfg = load_config(valid_config_path)
        assert isinstance(cfg.youtube, YouTubeConfig)
        assert cfg.youtube.channel_url == "https://www.youtube.com/@LatentSpaceTV"
        assert cfg.youtube.match_window_days == 7
        assert cfg.youtube.playlist_depth == 30

    def test_default_notebooklm(self, valid_config_path: Path):
        cfg = load_config(valid_config_path)
        assert isinstance(cfg.notebooklm, NotebookLMConfig)
        assert cfg.notebooklm.format == "deep-dive"
        assert cfg.notebooklm.length == "standard"

    def test_default_fallback(self, valid_config_path: Path):
        cfg = load_config(valid_config_path)
        assert isinstance(cfg.fallback, FallbackConfig)
        assert cfg.fallback.enabled is True
        assert cfg.fallback.llm_model == "gpt-4o-mini"
        assert cfg.fallback.tts_voices == ["alloy", "echo"]
        assert cfg.fallback.chunk_max_chars == 4096

    def test_default_errors(self, valid_config_path: Path):
        cfg = load_config(valid_config_path)
        assert isinstance(cfg.errors, ErrorConfig)
        assert cfg.errors.max_retries == 3
        assert cfg.errors.backoff_base == 60

    def test_default_schedule(self, valid_config_path: Path):
        cfg = load_config(valid_config_path)
        assert isinstance(cfg.schedule, ScheduleConfig)
        assert len(cfg.schedule.run_days) == 7
        assert "monday" in cfg.schedule.run_days

    def test_default_security(self, valid_config_path: Path):
        cfg = load_config(valid_config_path)
        assert isinstance(cfg.security, SecurityConfig)
        assert "arxiv.org" in cfg.security.allowed_domains
        assert cfg.security.max_download_bytes == 100_000_000
        assert cfg.security.enforce_https is True

    def test_override_values(self, tmp_config):
        path = tmp_config("""\
            rss:
              base_url: "https://example.github.io/podcast"
              owner_email: "test@example.com"
            errors:
              max_retries: 5
              backoff_base: 120
            luma:
              event_filter: "Reading Group"
        """)
        cfg = load_config(path)
        assert cfg.errors.max_retries == 5
        assert cfg.errors.backoff_base == 120
        assert cfg.luma.event_filter == "Reading Group"
        # Non-overridden defaults still apply
        assert cfg.luma.calendar_url == "https://lu.ma/ls"


# ---------------------------------------------------------------------------
# Missing config file
# ---------------------------------------------------------------------------


class TestMissingConfigFile:
    def test_raises_file_not_found(self, tmp_path: Path):
        missing = tmp_path / "nonexistent.yaml"
        with pytest.raises(FileNotFoundError, match="config.example.yaml"):
            load_config(missing)


# ---------------------------------------------------------------------------
# Invalid YAML
# ---------------------------------------------------------------------------


class TestInvalidYAML:
    def test_malformed_yaml_raises_config_error(self, tmp_config):
        path = tmp_config("luma: {bad yaml: [")
        with pytest.raises(ConfigError, match="Invalid YAML"):
            load_config(path)

    def test_yaml_with_non_mapping_root(self, tmp_config):
        path = tmp_config("- item1\n- item2\n")
        with pytest.raises(ConfigError, match="must contain a YAML mapping"):
            load_config(path)


# ---------------------------------------------------------------------------
# Missing required fields
# ---------------------------------------------------------------------------


class TestMissingRequiredFields:
    def test_missing_base_url(self, tmp_config):
        path = tmp_config("""\
            rss:
              owner_email: "test@example.com"
        """)
        with pytest.raises(ConfigError, match="rss.base_url"):
            load_config(path)

    def test_missing_owner_email(self, tmp_config):
        path = tmp_config("""\
            rss:
              base_url: "https://example.github.io/podcast"
        """)
        with pytest.raises(ConfigError, match="rss.owner_email"):
            load_config(path)

    def test_missing_both_required(self, tmp_config):
        path = tmp_config("""\
            rss:
              title: "My Podcast"
        """)
        with pytest.raises(ConfigError, match="rss.base_url"):
            load_config(path)

    def test_empty_config_missing_required(self, tmp_config):
        """An empty YAML mapping should still fail on required RSS fields."""
        path = tmp_config("{}")
        with pytest.raises(ConfigError, match="rss.base_url"):
            load_config(path)


# ---------------------------------------------------------------------------
# Unknown fields
# ---------------------------------------------------------------------------


class TestUnknownFields:
    def test_unknown_field_raises_config_error(self, tmp_config):
        path = tmp_config("""\
            rss:
              base_url: "https://example.github.io/podcast"
              owner_email: "test@example.com"
            luma:
              nonexistent_field: "value"
        """)
        with pytest.raises(ConfigError, match="Unknown fields.*nonexistent_field"):
            load_config(path)


# ---------------------------------------------------------------------------
# Section type errors
# ---------------------------------------------------------------------------


class TestSectionTypeErrors:
    def test_section_as_scalar_raises_config_error(self, tmp_config):
        path = tmp_config("""\
            rss:
              base_url: "https://example.github.io/podcast"
              owner_email: "test@example.com"
            luma: "not a mapping"
        """)
        with pytest.raises(ConfigError, match="must be a mapping"):
            load_config(path)


# ---------------------------------------------------------------------------
# Full config.example.yaml loads correctly
# ---------------------------------------------------------------------------


class TestExampleConfig:
    def test_example_config_loads(self):
        """config.example.yaml must parse and load (but has empty required fields)."""
        example = Path(__file__).resolve().parent.parent / "config.example.yaml"
        if not example.exists():
            pytest.skip("config.example.yaml not found")
        # The example has empty required fields, so it should raise ConfigError
        with pytest.raises(ConfigError, match="Missing required"):
            load_config(example)

    def test_example_config_with_required_fields(self, tmp_path: Path):
        """config.example.yaml with required fields filled in should load."""
        import yaml

        example = Path(__file__).resolve().parent.parent / "config.example.yaml"
        if not example.exists():
            pytest.skip("config.example.yaml not found")
        data = yaml.safe_load(example.read_text(encoding="utf-8"))
        data["rss"]["base_url"] = "https://example.github.io/podcast"
        data["rss"]["owner_email"] = "test@example.com"
        patched = tmp_path / "config.yaml"
        patched.write_text(yaml.dump(data), encoding="utf-8")
        cfg = load_config(patched)
        assert isinstance(cfg, PipelineConfig)
        assert cfg.rss.base_url == "https://example.github.io/podcast"
