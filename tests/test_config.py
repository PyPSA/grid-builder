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
        {"retrieve": {"source": "unsupported"}},
        {"retrieve": {"target_date": "not-a-date"}},
        {"retrieve": {"typo": True}},
        {"retrieve": {"overpass_api": {"max_tries": 0}}},
        {"retrieve": {"overpass_api": {"timeout": 0}}},
        {"retrieve": {"overpass_api": {"user_agent": {"typo": True}}}},
        {"network": {"frequency_hz": {"AC": -50}}},
        {"network": {"frequency_hz": {"DC": -1}}},
        {"network": {"frequency_hz": {"typo": True}}},
        {"network": {"remove_under_construction": "not-a-bool"}},
        {"crs": {"typo": True}},
        {"regions": {"BE": {"frequency_hz": {"typo": True}}}},
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
    validated = validate_config(defaults)
    assert validated.retrieve.overpass_api.max_tries == 5
    assert validated.retrieve.include_relations is True
    assert validated.network.frequency_hz.AC == 50.0
    assert validated.network.frequency_hz.DC == 0.0
    assert validated.network.remove_under_construction is True
    assert validated.crs.geo == "EPSG:4326"
    assert validated.crs.distance == "EPSG:3035"


def test_selected_regions_load_checked_in_overrides(tmp_path):
    """Load only selected regional defaults and preserve caller overrides."""
    regions = tmp_path / "regions"
    regions.mkdir()
    (regions / "config.BE.yaml").write_text("network:\n  minimum_voltage_kv: 230\n")
    (regions / "config.NL.yaml").write_text("network:\n  station_merge_radius_m: 600\n")
    config = load_region_configs(
        {"countries": ["BE", "NL"], "regions": {"BE": {"minimum_voltage_kv": 225}}},
        regions,
    )
    assert config["regions"] == {
        "BE": {"minimum_voltage_kv": 225},
        "NL": {"station_merge_radius_m": 600},
    }


def test_regional_frequency_override_falls_back_per_field():
    """A region overriding only AC still inherits the network default for DC."""
    config = validate_config({"regions": {"US": {"frequency_hz": {"AC": 60.0}}}})
    us_override = config.regions["US"].frequency_hz
    assert us_override.AC == 60.0
    assert us_override.DC is None
    assert config.network.frequency_hz.AC == 50.0
