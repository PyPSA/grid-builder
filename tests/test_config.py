"""Regression tests for configuration validation and generated documentation."""

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from workflow.scripts._schema import (
    generate_config_defaults,
    generate_config_schema,
    load_region_configs,
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


def test_selected_regions_load_checked_in_overrides(tmp_path):
    """Load only selected regional defaults and preserve caller overrides."""
    regions = tmp_path / "regions"
    regions.mkdir()
    (regions / "config.BE.yaml").write_text("network:\n  minimum_voltage_kv: 230\n")
    (regions / "config.NL.yaml").write_text(
        "network:\n  station_merge_distance_m: 600\n"
    )
    config = load_region_configs(
        {"countries": ["BE", "NL"], "regions": {"BE": {"minimum_voltage_kv": 225}}},
        regions,
    )
    assert config["regions"] == {
        "BE": {"minimum_voltage_kv": 225},
        "NL": {"station_merge_distance_m": 600},
    }
