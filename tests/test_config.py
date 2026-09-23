"""Regression tests for configuration validation and generated documentation."""

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from workflow.scripts._schema import (
    generate_config_defaults,
    generate_config_schema,
    validate_config,
)


@pytest.mark.parametrize(
    "config",
    [
        {"countries": []},
        {"countries": ["not-a-country"]},
        {"retrieve": {"features": []}},
        {"retrieve": {"source": "unsupported"}},
        {"retrieve": {"mp": True}},
        {"retrieve": {"target_date": "not-a-date"}},
        {"retrieve": {"typo": True}},
    ],
)
def test_invalid_config(config):
    """Reject invalid settings before starting expensive retrieval jobs."""
    with pytest.raises(ValidationError):
        validate_config(config)


def test_generated_config_matches_repository(tmp_path):
    """Keep the shipped defaults and JSON schema in sync with validation."""
    config_dir = Path(__file__).resolve().parents[1] / "config"
    defaults = generate_config_defaults(str(tmp_path / "config.yaml"))
    schema = generate_config_schema(str(tmp_path / "config.schema.json"))
    assert defaults == yaml.safe_load((config_dir / "config.yaml").read_text())
    assert schema == json.loads((config_dir / "config.schema.json").read_text())
    assert validate_config(defaults).retrieve.mp is False
