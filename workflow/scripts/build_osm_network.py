# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT

"""Build a connected, PyPSA-independent network from clean OSM features."""

import logging
from itertools import combinations
from pathlib import Path
from typing import TYPE_CHECKING, Any

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

if TYPE_CHECKING:
    snakemake: Any

logger = logging.getLogger(__name__)
GEO_CRS = "EPSG:4326"
DISTANCE_CRS = "EPSG:3035"


def _empty_geodataframe(columns: list[str]) -> gpd.GeoDataFrame:
    """Create an empty GeoDataFrame with an active geometry column."""
    return gpd.GeoDataFrame(
        {column: [] for column in columns},
        geometry=gpd.GeoSeries([], crs=GEO_CRS),
        crs=GEO_CRS,
    )


def configure_logging(log_path: str) -> None:
    """Send rule and dependency logging to the Snakemake log file."""
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


def _station_regions(
    substations: gpd.GeoDataFrame,
    polygons: gpd.GeoDataFrame,
    lines: gpd.GeoDataFrame,
    merge_distance_m: float,
) -> tuple[list[Any], dict[str, Point]]:
    """Create unioned station areas from OSM substations and line endpoints."""
    entities: list[tuple[str, object]] = []
    for _, row in substations.iterrows():
        entities.append((row["bus_id"], row.geometry))
    for _, row in polygons.iterrows():
        entities.append((f"polygon:{row['bus_id']}", row.geometry))
    for _, row in lines.iterrows():
        coords = list(row.geometry.coords)
        entities.extend(
            [
                (f"{row['line_id']}:0", Point(coords[0])),
                (f"{row['line_id']}:1", Point(coords[-1])),
            ]
        )

    if not entities:
        return [], {}
    geometries = gpd.GeoSeries([geometry for _, geometry in entities], crs=GEO_CRS)
    buffers = geometries.to_crs(DISTANCE_CRS).buffer(merge_distance_m)
    union = unary_union(buffers.tolist())
    regions: list[Any] = list(union.geoms) if hasattr(union, "geoms") else [union]
    points = geometries.to_crs(DISTANCE_CRS)
    assignment: dict[str, Point] = {}
    for (identifier, _), point in zip(entities, points, strict=True):
        for index, region in enumerate(regions):
            if region.covers(point):
                assignment[identifier] = Point(index, 0)
                break
    return regions, assignment


def build_osm_network(
    substations: gpd.GeoDataFrame,
    polygons: gpd.GeoDataFrame,
    lines: gpd.GeoDataFrame,
    merge_distance_m: float,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Create buses, AC lines, and transformers while retaining OSM attributes."""
    if lines.empty:
        empty_buses = _empty_geodataframe(["bus_id", "geometry"])
        empty_lines = _empty_geodataframe(["line_id", "geometry"])
        empty_transformers = _empty_geodataframe(["transformer_id", "geometry"])
        return empty_buses, empty_lines, empty_transformers

    regions, assignment = _station_regions(
        substations, polygons, lines, merge_distance_m
    )
    region_points = gpd.GeoSeries(
        [region.representative_point() for region in regions], crs=DISTANCE_CRS
    ).to_crs(GEO_CRS)

    sources: list[dict[str, Any]] = []
    for _, row in substations.iterrows():
        location = assignment.get(row["bus_id"])
        if location is None:
            continue
        sources.append(
            {
                "source_id": row["bus_id"],
                "region_index": int(location.x),
                "voltage_kv": row["voltage_kv"],
                "country": row["country"],
                "osm_id": row["osm_id"],
            }
        )
    for _, row in lines.iterrows():
        for endpoint in (0, 1):
            location = assignment[f"{row['line_id']}:{endpoint}"]
            sources.append(
                {
                    "source_id": f"{row['line_id']}:{endpoint}",
                    "region_index": int(location.x),
                    "voltage_kv": row["voltage_kv"],
                    "country": row["country"],
                    "osm_id": row["osm_id"],
                }
            )
    source_frame = pd.DataFrame(sources)
    station_ids = {
        region_index: f"station/{min(group['source_id'])}"
        for region_index, group in source_frame.groupby("region_index", sort=True)
    }
    grouped = source_frame.groupby(["region_index", "voltage_kv"], sort=True)
    buses_rows: list[dict[str, Any]] = []
    source_to_bus: dict[str, str] = {}
    for (region_index, voltage), group in grouped:
        station_id = station_ids[region_index]
        bus_id = f"{station_id}-{int(voltage)}"
        countries = sorted(group["country"].dropna().unique())
        osm_ids = sorted(group["osm_id"].dropna().unique())
        buses_rows.append(
            {
                "bus_id": bus_id,
                "station_id": station_id,
                "voltage_kv": voltage,
                "dc": False,
                "country": ";".join(countries),
                "osm_ids": ";".join(osm_ids),
                "geometry": region_points.iloc[region_index],
            }
        )
        source_to_bus.update({source_id: bus_id for source_id in group["source_id"]})
    buses = gpd.GeoDataFrame(buses_rows, geometry="geometry", crs=GEO_CRS)
    bus_geometry = buses.set_index("bus_id").geometry

    line_rows: list[dict[str, Any]] = []
    for _, row in lines.iterrows():
        bus0 = source_to_bus[f"{row['line_id']}:0"]
        bus1 = source_to_bus[f"{row['line_id']}:1"]
        if bus0 == bus1:
            continue
        coordinates = list(row.geometry.coords)
        geometry = LineString(
            [bus_geometry[bus0], *coordinates[1:-1], bus_geometry[bus1]]
        )
        line_rows.append(
            {
                "line_id": row["line_id"],
                "osm_id": row["osm_id"],
                "bus0": bus0,
                "bus1": bus1,
                "voltage_kv": row["voltage_kv"],
                "circuits": row["circuits"],
                "length_m": geometry.length,
                "underground": row["underground"],
                "country": row["country"],
                "tags": row["tags"],
                "geometry": geometry,
            }
        )
    built_lines = (
        gpd.GeoDataFrame(line_rows, geometry="geometry", crs=GEO_CRS)
        if line_rows
        else _empty_geodataframe(
            [
                "line_id",
                "osm_id",
                "bus0",
                "bus1",
                "voltage_kv",
                "circuits",
                "length_m",
                "underground",
                "country",
                "tags",
                "geometry",
            ]
        )
    )
    if not built_lines.empty:
        built_lines["length_m"] = built_lines.to_crs(DISTANCE_CRS).length.round(2)

    transformer_rows: list[dict[str, Any]] = []
    for station_id, group in buses.groupby("station_id"):
        for bus0, bus1 in combinations(group.itertuples(), 2):
            transformer_rows.append(
                {
                    "transformer_id": f"{station_id}-{int(bus0.voltage_kv)}-{int(bus1.voltage_kv)}",
                    "station_id": station_id,
                    "bus0": bus0.bus_id,
                    "bus1": bus1.bus_id,
                    "voltage_bus0_kv": bus0.voltage_kv,
                    "voltage_bus1_kv": bus1.voltage_kv,
                    "geometry": LineString([bus0.geometry, bus1.geometry]),
                }
            )
    transformers = (
        gpd.GeoDataFrame(transformer_rows, geometry="geometry", crs=GEO_CRS)
        if transformer_rows
        else _empty_geodataframe(
            [
                "transformer_id",
                "station_id",
                "bus0",
                "bus1",
                "voltage_bus0_kv",
                "voltage_bus1_kv",
                "geometry",
            ]
        )
    )
    return buses, built_lines, transformers


def _write_components(
    components: gpd.GeoDataFrame, csv_path: str, geojson_path: str
) -> None:
    csv = pd.DataFrame(components.drop(columns="geometry"))
    csv["geometry"] = components.geometry.to_wkt()
    csv.to_csv(csv_path, index=False)
    components.to_file(geojson_path, driver="GeoJSON")


if __name__ == "__main__":
    configure_logging(snakemake.log[0])
    buses, lines, transformers = build_osm_network(
        gpd.read_file(snakemake.input.substations),
        gpd.read_file(snakemake.input.substations_polygon),
        gpd.read_file(snakemake.input.lines),
        snakemake.params.station_merge_distance_m,
    )
    logger.info(
        "Built %d buses, %d lines, and %d transformers.",
        len(buses),
        len(lines),
        len(transformers),
    )
    _write_components(buses, snakemake.output.buses, snakemake.output.buses_geojson)
    _write_components(lines, snakemake.output.lines, snakemake.output.lines_geojson)
    _write_components(
        transformers,
        snakemake.output.transformers,
        snakemake.output.transformers_geojson,
    )
