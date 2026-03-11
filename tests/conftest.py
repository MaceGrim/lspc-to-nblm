"""Shared test fixtures for the LSPC pipeline test suite."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


@pytest.fixture()
def tmp_config(tmp_path: Path):
    """Return a helper that writes a config.yaml in tmp_path and returns its Path."""

    def _write(content: str) -> Path:
        p = tmp_path / "config.yaml"
        p.write_text(textwrap.dedent(content), encoding="utf-8")
        return p

    return _write


# Minimal valid config with all required fields populated.
MINIMAL_VALID_YAML = """\
rss:
  base_url: "https://example.github.io/podcast"
  owner_email: "test@example.com"
"""


@pytest.fixture()
def valid_config_path(tmp_config) -> Path:
    """Write and return a path to a minimal valid config file."""
    return tmp_config(MINIMAL_VALID_YAML)
