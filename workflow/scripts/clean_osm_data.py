# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT

"""Clean earth-osm exports into generic high-voltage grid features."""

import ast
import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point, Polygon

if TYPE_CHECKING:
    snakemake: Any

logger = logging.getLogger(__name__)
GEO_CRS = "EPSG:4326"


def configure_logging(log_path: str) -> None:
    """Send rule and dependency logging to the Snakemake log file."""
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


def _present(value: Any) -> bool:
    return (
        value is not None
        and not (isinstance(value, float) and np.isnan(value))
        and value != ""
    )


def _tags(row: pd.Series) -> dict[str, str]:
    """Recover a tag mapping from earth-osm's flattened CSV columns."""
    tags = {
        key.removeprefix("tags."): str(value)
        for key, value in row.items()
        if key.startswith("tags.") and _present(value)
    }
    other = row.get("other_tags")
    if _present(other):
        try:
            parsed = json.loads(other) if isinstance(other, str) else other
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            tags.update(
                {
                    str(key).removeprefix("tags."): str(value)
                    for key, value in parsed.items()
                    if _present(value)
                }
            )
    return tags


def _coordinates(value: Any) -> list[tuple[float, float]]:
    if not _present(value):
        return []
    try:
        coords = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        coords = ast.literal_eval(value)
    return [(float(pair[0]), float(pair[1])) for pair in coords if len(pair) >= 2]


def _numbers(value: Any) -> list[float]:
    """Extract numeric values from inconsistent OSM tag values."""
    if not _present(value):
        return []
    normalised = str(value).lower().replace(",", ".").replace("kv", "000")
    return [float(number) for number in re.findall(r"\d+(?:\.\d+)?", normalised)]


def _voltages_kv(value: Any) -> list[float]:
    voltages = _numbers(value)
    return [voltage / 1000 if voltage >= 1000 else voltage for voltage in voltages]


def _is_under_construction(tags: dict[str, str]) -> bool:
    return (
        tags.get("power") == "construction"
        or tags.get("construction:power") in {"line", "cable", "substation"}
        or tags.get("construction") in {"line", "cable", "substation"}
    )


def _start_date(tags: dict[str, str]) -> date | None:
    value = tags.get("start_date")
    if not value:
        return None
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(0))
    except ValueError:
        return None


def _keep_asset(tags: dict[str, str], network: dict[str, Any]) -> bool:
    if network["under_construction"] == "remove" and _is_under_construction(tags):
        return False
    cutoff = network.get("remove_after")
    asset_start = _start_date(tags)
    return not (cutoff and asset_start and asset_start > date.fromisoformat(cutoff))


def _region_network(
    country: str, network: dict[str, Any], regions: dict[str, Any]
) -> dict[str, Any]:
    overrides = {
        key: value
        for key, value in regions.get(country, {}).items()
        if value is not None
    }
    return {**network, **overrides}


def _circuits(tags: dict[str, str], voltage_count: int, index: int) -> int:
    candidates = _numbers(tags.get("circuits"))
    if candidates:
        if len(candidates) == voltage_count:
            return max(1, int(candidates[index]))
        return max(1, int(candidates[0] // voltage_count))
    cables = _numbers(tags.get("cables"))
    if cables:
        return max(1, int(cables[0] // 3 // voltage_count))
    return 1


def _frequency(tags: dict[str, str]) -> int:
    values = _numbers(tags.get("frequency"))
    return int(values[0]) if values else 50


def _load_records(paths: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        frame = pd.read_csv(path)
        country, _ = Path(path).stem.split("_", maxsplit=1)
        for _, row in frame.iterrows():
            coords = _coordinates(row.get("lonlat"))
            if not coords:
                continue
            element_type = "way" if row["Type"] == "area" else row["Type"]
            records.append(
                {
                    "osm_id": f"{element_type}/{int(row['id'])}",
                    "country": country,
                    "element_type": element_type,
                    "coordinates": coords,
                    "tags": _tags(row),
                }
            )
    return records


def clean_osm_data(
    paths: list[str], network: dict[str, Any], regions: dict[str, Any]
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Return clean substations, substation polygons, and AC lines/cables."""
    substations: list[dict[str, Any]] = []
    polygons: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []

    for record in _load_records(paths):
        tags = record["tags"]
        settings = _region_network(record["country"], network, regions)
        if not _keep_asset(tags, settings):
            continue
        power = tags.get("power")
        voltage_values = _voltages_kv(tags.get("voltage"))
        valid_voltages = [
            value for value in voltage_values if value >= settings["minimum_voltage_kv"]
        ]

        if power == "substation" and valid_voltages:
            coordinates = record["coordinates"]
            polygon = (
                Polygon(coordinates)
                if len(coordinates) >= 4 and coordinates[0] == coordinates[-1]
                else None
            )
            point = polygon.representative_point() if polygon else Point(coordinates[0])
            for voltage in valid_voltages:
                bus_id = f"{record['osm_id']}-{int(voltage)}"
                substations.append(
                    {
                        "bus_id": bus_id,
                        "osm_id": record["osm_id"],
                        "country": record["country"],
                        "voltage_kv": voltage,
                        "tags": json.dumps(tags, sort_keys=True),
                        "geometry": point,
                    }
                )
                if polygon:
                    polygons.append(
                        {
                            "bus_id": bus_id,
                            "osm_id": record["osm_id"],
                            "country": record["country"],
                            "voltage_kv": voltage,
                            "geometry": polygon,
                        }
                    )
            continue

        if power not in {"line", "cable"} or _frequency(tags) != 50:
            continue
        coordinates = record["coordinates"]
        if len(coordinates) < 2:
            continue
        geometry = LineString(coordinates)
        for index, voltage in enumerate(valid_voltages):
            lines.append(
                {
                    "line_id": f"{record['osm_id']}-{int(voltage)}-{index + 1}",
                    "osm_id": record["osm_id"],
                    "country": record["country"],
                    "voltage_kv": voltage,
                    "circuits": _circuits(tags, len(valid_voltages), index),
                    "underground": power == "cable"
                    or tags.get("location") == "underground",
                    "tags": json.dumps(tags, sort_keys=True),
                    "geometry": geometry,
                }
            )

    buses = gpd.GeoDataFrame(substations, geometry="geometry", crs=GEO_CRS)
    substation_polygons = gpd.GeoDataFrame(polygons, geometry="geometry", crs=GEO_CRS)
    clean_lines = gpd.GeoDataFrame(lines, geometry="geometry", crs=GEO_CRS)

    if not clean_lines.empty and not substation_polygons.empty:
        joined = gpd.sjoin(
            clean_lines,
            substation_polygons[["geometry"]],
            how="left",
            predicate="within",
        )
        contained_line_indices = joined.loc[
            joined["index_right"].notna()
        ].index.unique()
        clean_lines = clean_lines.drop(index=contained_line_indices).copy()
    return buses, substation_polygons, clean_lines


if __name__ == "__main__":
    configure_logging(snakemake.log[0])
    buses, polygons, lines = clean_osm_data(
        list(snakemake.input.raw), snakemake.params.network, snakemake.params.regions
    )
    logger.info(
        "Retained %d buses, %d substation polygons, and %d AC lines/cables.",
        len(buses),
        len(polygons),
        len(lines),
    )
    buses.to_file(snakemake.output.substations, driver="GeoJSON")
    polygons.to_file(snakemake.output.substations_polygon, driver="GeoJSON")
    lines.to_file(snakemake.output.lines, driver="GeoJSON")
