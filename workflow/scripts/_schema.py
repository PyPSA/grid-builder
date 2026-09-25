"""Config validation for grid-builder.

Pydantic models are the single source of truth for config structure,
defaults, schema, and validation.
"""

import json
import math
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from earth_osm.regions import get_all_valid_codes, get_region_tuple
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

_VALID_REGIONS: frozenset[str] = frozenset(get_all_valid_codes())


def _validate_countries(countries: list[str]) -> None:
    """Raise a clear error for any country not recognised by earth-osm's region list.

    Shared by ``ConfigSchema``'s field validator and ``load_region_configs``:
    the latter resolves ``countries`` into region codes *before* the model is
    ever validated, so without this same check up front, an invalid code
    reaches ``earth_osm.regions.get_region_tuple`` first and surfaces as a
    raw ``KeyError`` instead of this message.
    """
    invalid = [c for c in countries if c not in _VALID_REGIONS]
    if invalid:
        raise ValueError(
            f"Unknown country identifier(s): {invalid}. "
            "Use an English name (e.g. 'benin') or ISO 3166-1 alpha-2 code (e.g. 'BE')."
        )


class ConfigModel(BaseModel):
    """Base model with dict-like access for Snakemake compatibility."""

    def __getitem__(self, key: str) -> Any:
        """Read a field by name, e.g. ``config["countries"]``."""
        return getattr(self, key)

    def __contains__(self, key: str) -> bool:
        """True if ``key`` names a field on this model."""
        return hasattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        """Read a field by name, falling back to ``default`` if it doesn't exist."""
        return getattr(self, key, default)

    def keys(self) -> Iterator[str]:
        """Iterate over field names."""
        return iter(type(self).model_fields.keys())

    def values(self) -> Iterator[Any]:
        """Iterate over field values."""
        return (getattr(self, k) for k in type(self).model_fields.keys())

    def items(self) -> Iterator[tuple[str, Any]]:
        """Iterate over ``(field name, value)`` pairs."""
        return ((k, getattr(self, k)) for k in type(self).model_fields.keys())


class OverpassUserAgentConfig(ConfigModel):
    """Identifies this tool to the Overpass API, per its fair-use policy."""

    model_config = ConfigDict(extra="forbid")

    project_name: str = Field(
        "grid-builder", description="Project name sent in the User-Agent header"
    )
    email: str = Field("", description="Contact email sent in the User-Agent header")
    website: str = Field(
        "https://github.com/pypsa/grid-builder",
        description="Project URL sent in the User-Agent header",
    )


class OverpassApiConfig(ConfigModel):
    """Settings for retrieve_osm_overpass.py's own Overpass API client."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        "https://overpass-api.de/api/interpreter",
        description="Overpass API endpoint to query",
    )
    max_tries: int = Field(
        5, description="Maximum number of attempts per query before giving up", ge=1
    )
    timeout: int = Field(600, description="Per-request timeout in seconds", gt=0)
    user_agent: OverpassUserAgentConfig = Field(default_factory=OverpassUserAgentConfig)


class RetrieveConfig(ConfigModel):
    """Settings for retrieve_osm_pbf.py/retrieve_osm_overpass.py."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["geofabrik", "overpass"] = Field(
        "geofabrik", description="Retrieval backend for OSM data"
    )
    force_redownload: bool = Field(False, description="Force refresh of cached data")
    target_date: datetime | None = Field(
        None,
        description=(
            "Optional historical date for data retrieval in ISO 8601 datetime "
            "format. Only honoured for retrieve.source: geofabrik."
        ),
    )
    overpass_api: OverpassApiConfig = Field(
        default_factory=OverpassApiConfig,
        description="Settings for retrieve_osm_overpass.py's Overpass API client",
    )


class FrequencyConfig(ConfigModel):
    """AC/DC frequency in Hz, used to classify and normalise OSM ``frequency`` tags."""

    model_config = ConfigDict(extra="forbid")

    AC: float = Field(
        50.0,
        description="AC frequency in Hz (50 for most of the world; 60 for the Americas and parts of Asia)",
        gt=0,
    )
    DC: float = Field(
        0.0,
        description="Frequency tag value OSM uses to mark a DC line/converter",
        ge=0,
    )


class NetworkConfig(ConfigModel):
    """Global assumptions for cleaning and connecting OSM grid features."""

    model_config = ConfigDict(extra="forbid")

    include_relations: bool = Field(
        True,
        description=(
            "Whether the network should consider OSM route=power/power=circuit "
            "relations, grouping their member ways into a single line matching "
            "the relation's real-world circuit; retrieval respects this too, "
            "so relations aren't fetched at all when it's off"
        ),
    )
    minimum_voltage_kv: float = Field(
        220.0, description="Minimum nominal AC voltage retained from OSM, in kV", gt=0
    )
    frequency_hz: FrequencyConfig = Field(
        default_factory=FrequencyConfig,
        description="AC/DC frequency in Hz; override per country in config/regions for e.g. 60 Hz grids",
    )
    station_merge_radius_m: float = Field(
        500.0,
        description="Buffer radius used to merge nearby substations and line endpoints, in metres",
        gt=0,
    )
    remove_under_construction: bool = Field(
        True, description="Whether assets tagged as under construction are dropped"
    )
    remove_after: date | None = Field(
        date(2026, 12, 31),
        description="Exclude assets with a later planned start date; null disables this filter",
    )


class SimplifyGeometriesConfig(ConfigModel):
    """Douglas-Peucker simplification tolerances for the interactive map."""

    model_config = ConfigDict(extra="forbid")

    enable: bool = Field(
        True,
        description="Whether to simplify station/bus-polygon/line geometries before embedding them in the interactive map",
    )
    stations_m: float = Field(
        100.0,
        description="Simplification tolerance for station polygon outlines, in metres",
        ge=0,
    )
    buses_polygon_m: float = Field(
        5.0,
        description="Simplification tolerance for individual bus/substation footprint polygons, in metres",
        ge=0,
    )
    lines_m: float = Field(
        30.0,
        description="Simplification tolerance for line geometries, in metres",
        ge=0,
    )


class InteractiveMapConfig(ConfigModel):
    """Geometry simplification and coordinate rounding for build_interactive_map.py."""

    model_config = ConfigDict(extra="forbid")

    coordinate_decimals: int = Field(
        5,
        description=(
            "Decimal places kept for coordinates embedded in the interactive "
            "map; 5 is about 1.1m of precision at the equator, comfortably "
            "below every simplification tolerance below"
        ),
        ge=0,
    )
    simplify_geometries: SimplifyGeometriesConfig = Field(
        default_factory=SimplifyGeometriesConfig,
        description="Geometry simplification tolerances for the interactive map",
    )


class RegionalFrequencyConfig(ConfigModel):
    """Optional per-country AC/DC override; unset fields fall back to network.frequency_hz."""

    model_config = ConfigDict(extra="forbid")

    AC: float | None = Field(None, gt=0)
    DC: float | None = Field(None, ge=0)


class RegionalNetworkConfig(ConfigModel):
    """Optional country-specific overrides loaded from config/regions."""

    model_config = ConfigDict(extra="forbid")

    minimum_voltage_kv: float | None = Field(None, gt=0)
    frequency_hz: RegionalFrequencyConfig | None = Field(None)


class CrsConfig(ConfigModel):
    """Coordinate reference systems used throughout clean/build_network."""

    model_config = ConfigDict(extra="forbid")

    geo: str = Field(
        "EPSG:4326", description="Geographic CRS used to store and exchange coordinates"
    )
    distance: str = Field(
        "EPSG:3035",
        description=(
            "Equal-area/equal-distance CRS used for buffering and length "
            "calculations; must suit the geographic extent in use (the "
            "default, ETRS89-LAEA, covers Europe)"
        ),
    )


class ConfigSchema(ConfigModel):
    """Top-level grid-builder config."""

    model_config = ConfigDict(extra="forbid")

    countries: list[str] = Field(
        default=["BE"],
        description="List of countries to retrieve OSM data for",
        min_length=1,
    )

    @field_validator("countries")
    @classmethod
    def validate_country_identifiers(cls, v: list[str]) -> list[str]:
        """Reject any country not recognised by earth-osm's region list."""
        _validate_countries(v)
        return v

    retrieve: RetrieveConfig = Field(
        default_factory=RetrieveConfig,
        description="Configuration for OSM data retrieval",
    )
    network: NetworkConfig = Field(
        default_factory=NetworkConfig,
        description="Settings for clean and build_network",
    )
    regions: dict[str, RegionalNetworkConfig] = Field(
        default_factory=dict,
        description="Country-specific network overrides loaded from config/regions",
    )
    interactive_map: InteractiveMapConfig = Field(
        default_factory=InteractiveMapConfig,
        description="Settings for build_interactive_map.py",
    )
    crs: CrsConfig = Field(
        default_factory=CrsConfig,
        description="Coordinate reference systems used throughout clean/build_network",
    )


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge nested dictionaries without mutating either input."""
    merged = base.copy()
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_region_configs(
    config: dict[str, Any], regions_dir: str | Path
) -> dict[str, Any]:
    """Load country settings selected by ``countries`` before validation.

    Files use the name ``config.<ISO-3166-1-alpha-2>.yaml`` and contain a
    ``network`` mapping. Explicit ``regions`` values in the calling config take
    precedence over the checked-in regional defaults.
    """
    raw = config.copy()
    supplied_regions = raw.pop("regions", {})
    countries = raw.get("countries", ConfigSchema.model_fields["countries"].default)
    _validate_countries(countries)
    yaml_reader = YAML(typ="safe")
    loaded_regions: dict[str, Any] = {}

    for country in countries:
        region = get_region_tuple(country).short
        path = Path(regions_dir) / f"config.{region}.yaml"
        if path.exists():
            loaded = yaml_reader.load(path) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"Regional config {path} must be a mapping.")
            loaded_regions[region] = loaded.get("network", loaded)

    raw["regions"] = _deep_merge(loaded_regions, supplied_regions)
    return raw


def validate_config(config: dict[str, Any]) -> ConfigSchema:
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
        """Quote strings only where needed: block style for multiline, else plain/quoted."""
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

    # Blank line between top-level keys for readability.
    for key in list(data.keys())[1:]:
        data.yaml_set_comment_before_after_key(key, before="\n")

    with open(path, "w") as f:
        yaml_writer.dump(data, f)

    return defaults


def generate_config_schema(path: str = "config/config.schema.json") -> dict:
    """Generate JSON schema file and return the schema dict."""

    def resolve_refs(obj, defs):
        """Inline every ``$ref`` against pydantic's ``$defs``, keeping any local description."""
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
        """Replace non-JSON-safe values (``inf``/``-inf``) with ``None``."""
        if isinstance(obj, dict):
            return {k: sanitize_for_json(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [sanitize_for_json(v) for v in obj]
        elif isinstance(obj, float) and math.isinf(obj):
            return None
        return obj

    def remove_nested_titles(obj, is_root=True):
        """Drop pydantic's auto-generated ``title`` on every level but the root."""
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
        """Drop the redundant ``"type": "object"`` on nested models that already have ``properties``."""
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
    "load_region_configs",
    "validate_config",
    "generate_config_defaults",
    "generate_config_schema",
    "ValidationError",
]


if __name__ == "__main__":
    generate_config_defaults()
    generate_config_schema()
