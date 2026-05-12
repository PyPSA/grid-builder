"""Config validation for grid-builder.

Pydantic models are the single source of truth for config structure,
defaults, schema, and validation.
"""

import json
import math
from collections.abc import Iterator
from datetime import datetime
from typing import Any, Literal

from earth_osm.regions import get_all_valid_codes
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

_VALID_REGIONS: frozenset[str] = frozenset(get_all_valid_codes())


class ConfigModel(BaseModel):
    """Base model with dict-like access for Snakemake compatibility."""

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def keys(self) -> Iterator[str]:
        return iter(type(self).model_fields.keys())

    def values(self) -> Iterator[Any]:
        return (getattr(self, k) for k in type(self).model_fields.keys())

    def items(self) -> Iterator[tuple[str, Any]]:
        return ((k, getattr(self, k)) for k in type(self).model_fields.keys())


class RetrieveConfig(ConfigModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["geofabrik", "overpass"] = Field(
        "geofabrik", description="Retrieval backend for OSM data"
    )
    primary_name: str = Field(
        "power", description="Primary OSM feature to retrieve (e.g., 'power')"
    )
    features: list[str] = Field(
        default=["substation", "line"],
        description="OSM features to retrieve for each country",
        min_length=1,
    )
    force_redownload: bool = Field(
        False, description="Force refresh of cached data in earth-osm"
    )
    mp: bool = Field(True, description="Enable multiprocessing in earth-osm")
    stream_backend: bool = Field(
        True, description="Enable streaming backend in earth-osm"
    )
    cache_primary: bool = Field(
        False, description="Enable caching of primary feature data in earth-osm"
    )
    target_date: datetime | None = Field(
        None,
        description="Optional historical date for data retrieval in ISO 8601 datetime format",
    )


class ConfigSchema(ConfigModel):
    model_config = ConfigDict(extra="forbid")

    countries: list[str] = Field(
        default=["BE"],
        description="List of countries to retrieve OSM data for",
        min_length=1,
    )

    @field_validator("countries")
    @classmethod
    def validate_country_identifiers(cls, v: list[str]) -> list[str]:
        invalid = [c for c in v if c not in _VALID_REGIONS]
        if invalid:
            raise ValueError(
                f"Unknown country identifier(s): {invalid}. "
                "Use an English name (e.g. 'benin') or ISO 3166-1 alpha-2 code (e.g. 'BE')."
            )
        return v

    retrieve: RetrieveConfig = Field(
        default_factory=RetrieveConfig,
        description="Configuration for OSM data retrieval using earth-osm",
    )


def validate_config(config: dict) -> ConfigSchema:
    """Validate config dict against schema."""
    return ConfigSchema(**config)


def generate_config_defaults(path: str = "config/config.yaml") -> dict:
    """Generate config defaults YAML file and return the defaults dict."""
    config = validate_config({})
    defaults = config.model_dump()

    yaml_writer = YAML()
    yaml_writer.version = (1, 1)
    yaml_writer.default_flow_style = False
    yaml_writer.width = 4096
    yaml_writer.indent(mapping=2, sequence=2, offset=0)

    def str_representer(dumper, data):
        TAG = "tag:yaml.org,2002:str"
        if "\n" in data:
            return dumper.represent_scalar(TAG, data, style="|")
        if data == "" or any(c in data for c in ":{}[]&*#?|-<>=!%@"):
            return dumper.represent_scalar(TAG, data, style='"')
        return dumper.represent_scalar(TAG, data, style="")

    yaml_writer.representer.add_representer(str, str_representer)

    data = CommentedMap()
    data.yaml_set_start_comment("yaml-language-server: $schema=./config.schema.json")

    for key, value in defaults.items():
        data[key] = value

    with open(path, "w") as f:
        yaml_writer.dump(data, f)

    return defaults


def generate_config_schema(path: str = "config/config.schema.json") -> dict:
    """Generate JSON schema file and return the schema dict."""

    def resolve_refs(obj, defs):
        if isinstance(obj, dict):
            if "$ref" in obj:
                ref_name = obj["$ref"].split("/")[-1]
                if ref_name in defs:
                    resolved = resolve_refs(defs[ref_name].copy(), defs)
                    if "description" in obj and "description" not in resolved:
                        resolved["description"] = obj["description"]
                    return resolved
            return {k: resolve_refs(v, defs) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [resolve_refs(item, defs) for item in obj]
        return obj

    def sanitize_for_json(obj):
        if isinstance(obj, dict):
            return {k: sanitize_for_json(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [sanitize_for_json(v) for v in obj]
        elif isinstance(obj, float) and math.isinf(obj):
            return None
        return obj

    def remove_nested_titles(obj, is_root=True):
        if isinstance(obj, dict):
            result = {}
            for k, v in obj.items():
                if k == "title" and not is_root:
                    continue
                result[k] = remove_nested_titles(v, is_root=False)
            return result
        elif isinstance(obj, list):
            return [remove_nested_titles(item, is_root=False) for item in obj]
        return obj

    def remove_object_type(obj, is_root=True):
        if isinstance(obj, dict):
            result = {}
            for k, v in obj.items():
                if (
                    k == "type"
                    and v == "object"
                    and not is_root
                    and "properties" in obj
                ):
                    continue
                result[k] = remove_object_type(v, is_root=False)
            return result
        elif isinstance(obj, list):
            return [remove_object_type(item, is_root=False) for item in obj]
        return obj

    config = validate_config({})
    schema = config.model_json_schema()
    defs = schema.pop("$defs", {})
    schema = resolve_refs(schema, defs)
    schema = sanitize_for_json(schema)
    schema = remove_nested_titles(schema)
    schema = remove_object_type(schema)

    with open(path, "w") as f:
        json.dump(schema, f, indent=2)
        f.write("\n")

    return schema


__all__ = [
    "ConfigSchema",
    "validate_config",
    "generate_config_defaults",
    "generate_config_schema",
    "ValidationError",
]


if __name__ == "__main__":
    generate_config_defaults()
    generate_config_schema()
