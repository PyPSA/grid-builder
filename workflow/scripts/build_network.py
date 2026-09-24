# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT

"""Build a connected, generic network from clean OSM features.

Line endpoints are merged through virtual buses using deterministic
geometry rules, nearby substations and line endpoints are clustered into
stations via buffer-and-union, and transformers are inferred between
voltage-level buses at the same station. The result is a generic
bus/line/transformer schema, with each component keeping its originating
OSM identifiers and geometry.
"""

import itertools
import logging
import string
from itertools import combinations
from typing import TYPE_CHECKING, Any

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from pyproj import Transformer
from scripts._helpers import BUS_TOL, configure_logging
from shapely import get_point
from shapely.algorithms.polylabel import polylabel
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import linemerge, split

if TYPE_CHECKING:
    snakemake: Any

logger = logging.getLogger(__name__)

COORD_PRECISION = 8


def _empty_geodataframe(columns: list[str], crs: str) -> gpd.GeoDataFrame:
    """Build an empty GeoDataFrame with the given non-geometry ``columns``."""
    return gpd.GeoDataFrame(
        {column: [] for column in columns}, geometry=gpd.GeoSeries([], crs=crs), crs=crs
    )


def _treat_under_construction(
    df: pd.DataFrame, remove_under_construction: bool, remove_after: str | None
) -> pd.DataFrame:
    """Drop under-construction rows (if ``remove_under_construction``) and rows started after ``remove_after``."""
    if remove_under_construction:
        len_before = len(df)
        df = df.drop(index=df.index[df["under_construction"]])
        logger.info("Removed %d elements under construction.", len_before - len(df))

    if remove_after is not None:
        df["start_date"] = pd.to_datetime(df["start_date"], errors="coerce")
        len_before = len(df)
        df = df.drop(index=df.index[df["start_date"] > pd.to_datetime(remove_after)])
        logger.info(
            "Removed %d elements with a start date after %s.",
            len_before - len(df),
            remove_after,
        )
    return df


def _merge_identical_lines(lines: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Aggregate lines with identical geometry and voltage (e.g. duplicated across a border)."""
    lines_all = lines.copy()
    lines_to_drop = []

    for _, group in lines_all.groupby(["geometry", "voltage"]):
        line_ids = list(group["line_id"])
        if len(line_ids) > 1:
            lid_old = line_ids[0]
            lid_agg = lid_old.split("-")[0]
            circuits_agg = group["circuits"].sum()
            lines_all.loc[lines_all["line_id"] == lid_old, "line_id"] = lid_agg
            lines_all.loc[lines_all["line_id"] == lid_agg, "circuits"] = circuits_agg
            lines_to_drop += line_ids[1:]

    lines_all = lines_all[~lines_all["line_id"].isin(lines_to_drop)]
    lines_all["line_id"] = (
        lines_all["line_id"]
        + "-"
        + lines_all["voltage"].div(1e3).astype(int).astype(str)
    )
    return lines_all


def _remove_loops_from_multiline(multiline: Any) -> Any:
    """Iteratively drop closed rings from a MultiLineString, remerging what remains."""
    elements_initial = (
        list(multiline.geoms)
        if multiline.geom_type == "MultiLineString"
        else [multiline]
    )
    if not any(line.is_closed for line in elements_initial):
        return multiline

    elements = elements_initial
    geometry_updated = multiline
    iteration_count = 0
    while any(line.is_closed for line in elements) and iteration_count < 5:
        elements = [line for line in elements if not line.is_closed]
        geometry_updated = linemerge(elements)
        elements = (
            list(geometry_updated.geoms)
            if geometry_updated.geom_type == "MultiLineString"
            else [geometry_updated]
        )
        iteration_count += 1
        if not any(line.is_closed for line in elements):
            break
    return geometry_updated


def _add_line_endings(lines: gpd.GeoDataFrame) -> pd.DataFrame:
    """Create deterministic virtual buses at each unique (voltage, endpoint) combination."""
    line_data = lines[["voltage", "geometry", "line_id"]]
    line_geoms = line_data["geometry"].apply(_remove_loops_from_multiline)

    endpoints0 = line_data.assign(
        geometry=get_point(line_geoms.geometry, 0), endpoint=0
    )
    endpoints1 = line_data.assign(
        geometry=get_point(line_geoms.geometry, -1), endpoint=1
    )
    endpoints = pd.concat([endpoints0, endpoints1], ignore_index=True)

    endpoints["line_id"] = endpoints["line_id"].str.split("-").str[0]
    endpoints["osm_id"] = endpoints["line_id"].str.extract(
        r"((?:way|relation)/\d+)", expand=False
    )
    endpoints["endpoint_name"] = (
        endpoints["line_id"] + ":" + endpoints["endpoint"].astype(str)
    )

    def create_bus_data(group: pd.DataFrame) -> pd.Series:
        endpoint_names = group["endpoint_name"]
        numeric_parts = endpoint_names.str.extract(r"/(\d+)", expand=False).astype(int)
        min_numeric = numeric_parts.min()
        candidates = endpoint_names[numeric_parts == min_numeric]
        bus_id = candidates.sort_values().iloc[0]
        osm_ids = list(set(group["osm_id"].tolist()))
        return pd.Series({"bus_id": bus_id, "contains": osm_ids})

    endpoints = (
        endpoints.groupby(["voltage", "geometry"])
        .apply(create_bus_data, include_groups=False)
        .reset_index()
    )
    endpoints["bus_id"] = (
        "virtual_"
        + endpoints["bus_id"]
        + "-"
        + (endpoints["voltage"] / 1000).astype(int).astype(str)
    )
    return endpoints[["bus_id", "voltage", "geometry", "contains"]]


def _split_linestring_by_point(
    linestring: LineString, points: list[Point]
) -> list[LineString]:
    """Split ``linestring`` at each of ``points`` in turn, returning the resulting segments."""
    list_linestrings = [linestring]
    for point in points:
        temp_list = [split(line, point) for line in list_linestrings]
        list_linestrings = [ls for result in temp_list for ls in result.geoms]
    return list_linestrings


def _alpha_suffix(i: int) -> str:
    """Convert a zero-based index to a spreadsheet-style letter suffix (0->'a', 26->'aa', ...)."""
    suffix = ""
    i += 1
    while i > 0:
        i, rem = divmod(i - 1, 26)
        suffix = string.ascii_lowercase[rem] + suffix
    return suffix


def split_overpassing_lines(
    lines: gpd.GeoDataFrame, buses: gpd.GeoDataFrame, distance_crs: str, tol: float = 1
) -> gpd.GeoDataFrame:
    """Split a line at any bus it geometrically overpasses without a shared OSM node."""
    lines = lines.copy()
    lines_to_add = []
    lines_to_split = []

    high_voltage_lines = lines.query("voltage >= 220000")
    if high_voltage_lines.empty:
        return lines

    lines_epsgmod = high_voltage_lines.to_crs(distance_crs)
    buses_epsgmod = buses.to_crs(distance_crs)
    buses_sindex = buses_epsgmod.sindex

    for line_index in high_voltage_lines.index:
        line_geom = lines_epsgmod.geometry.loc[line_index]
        possible_matches = list(buses_sindex.intersection(line_geom.bounds))
        if not possible_matches:
            continue

        nearby_buses = buses_epsgmod.iloc[possible_matches]
        bus_in_tol = nearby_buses[nearby_buses.geometry.distance(line_geom) <= tol]

        endpoint0 = line_geom.boundary.geoms[0]
        endpoint1 = line_geom.boundary.geoms[1]
        dist_to_ep0 = bus_in_tol.geometry.distance(endpoint0)
        dist_to_ep1 = bus_in_tol.geometry.distance(endpoint1)
        bus_in_tol = bus_in_tol[(dist_to_ep0 > tol) | (dist_to_ep1 > tol)]

        if not bus_in_tol.empty:
            lines_to_split.append(line_index)
            buses_locs = buses.geometry.loc[bus_in_tol.index]
            new_geometries = _split_linestring_by_point(
                lines.geometry[line_index], buses_locs
            )
            n_geoms = len(new_geometries)

            df_append = gpd.GeoDataFrame([lines.loc[line_index]] * n_geoms)
            df_append["geometry"] = new_geometries
            original_line_id = str(df_append["line_id"].iloc[0])
            parts = original_line_id.rsplit("-", 1)
            base_id = parts[0]
            voltage_suffix = parts[1] if len(parts) > 1 else ""
            df_append["line_id"] = [
                f"{base_id}:{_alpha_suffix(i)}-{voltage_suffix}"
                if n_geoms > 1
                else original_line_id
                for i in range(n_geoms)
            ]
            lines_to_add.append(df_append)

    if not lines_to_add:
        return lines

    df_to_add = gpd.GeoDataFrame(pd.concat(lines_to_add, ignore_index=True))
    df_to_add.set_crs(lines.crs, inplace=True)
    df_to_add.set_index(lines.index[-1] + df_to_add.index, inplace=True)

    lines = lines.drop(lines_to_split)
    lines = df_to_add if lines.empty else pd.concat([lines, df_to_add])
    return gpd.GeoDataFrame(lines.reset_index(drop=True), crs=lines.crs)


def _create_merge_mapping(
    lines: gpd.GeoDataFrame,
    buses: gpd.GeoDataFrame,
    buses_polygon: gpd.GeoDataFrame,
    geo_crs: str,
) -> gpd.GeoDataFrame:
    """Group lines connected by a pass-through virtual bus into one merged line each.

    A virtual bus qualifies only if it isn't inside a real substation polygon,
    touches exactly two lines of matching voltage and circuits, and those
    lines form a connected component via networkx — so only unambiguous
    merges happen.
    """
    buses_virtual = buses[buses["bus_id"].str.startswith("virtual")].copy()
    if not buses_polygon.empty:
        intersects_polygon = buses_virtual.intersects(buses_polygon.union_all())
        buses_virtual = buses_virtual[~intersects_polygon]

    buses_virtual = gpd.sjoin(
        buses_virtual,
        lines[["line_id", "geometry", "voltage", "circuits"]],
        how="left",
        predicate="touches",
    )
    buses_virtual = buses_virtual[
        buses_virtual["voltage_left"] == buses_virtual["voltage_right"]
    ]
    buses_virtual = buses_virtual.drop(columns=["voltage_right"]).rename(
        columns={"voltage_left": "voltage"}
    )

    counts = (
        buses_virtual.groupby(["bus_id", "voltage"]).size().reset_index(name="count")
    )
    two_line_buses = counts[counts["count"] == 2]
    buses_virtual = buses_virtual[
        buses_virtual["bus_id"].isin(two_line_buses["bus_id"])
    ]

    circuit_counts = (
        buses_virtual.groupby(["bus_id", "circuits", "voltage"])
        .size()
        .reset_index(name="count")
    )
    circuit_counts = circuit_counts[circuit_counts["count"] == 2]
    buses_virtual = buses_virtual[
        buses_virtual["bus_id"].isin(circuit_counts["bus_id"])
    ]

    buses_to_remove = (
        buses_virtual.groupby(["bus_id", "voltage", "circuits"])
        .agg({"line_id": list})
        .reset_index()
    )

    unique_lines = pd.Series(itertools.chain(*buses_to_remove["line_id"])).unique()
    lines_to_merge = lines.loc[
        lines["line_id"].isin(unique_lines),
        ["line_id", "voltage", "circuits", "length", "geometry", "underground"],
    ]
    lines_to_merge_dict = [
        (node, row.to_dict())
        for node, row in lines_to_merge.set_index("line_id").iterrows()
    ]

    graph: nx.Graph = nx.Graph()
    graph.add_nodes_from(lines_to_merge_dict)
    edges = [
        (row["line_id"][0], row["line_id"][1], {"bus_id": row["bus_id"]})
        for _, row in buses_to_remove.iterrows()
    ]
    graph.add_edges_from(edges)

    subgraph_data = []
    for component in nx.connected_components(graph):
        subgraph = graph.subgraph(component)
        first_node = next(iter(component))
        circuits = graph.nodes[first_node].get("circuits")
        voltage = graph.nodes[first_node].get("voltage")
        geometry = linemerge(
            [graph.nodes[node].get("geometry") for node in subgraph.nodes()]
        )

        contains_lines = list(subgraph.nodes())
        node_longest = max(
            subgraph.nodes(), key=lambda node: graph.nodes[node].get("length", 0)
        )
        underground = graph.nodes[node_longest].get("underground")

        contains_buses = [graph.edges[edge].get("bus_id") for edge in subgraph.edges()]

        if not isinstance(geometry, LineString) or geometry.is_closed:
            continue

        subgraph_data.append(
            {
                "line_id": f"merged_{node_longest}+{len(contains_lines) - 1}",
                "circuits": circuits,
                "voltage": voltage,
                "geometry": geometry,
                "underground": underground,
                "contains_lines": contains_lines,
                "contains_buses": contains_buses,
            }
        )

    columns = [
        "line_id",
        "circuits",
        "voltage",
        "geometry",
        "underground",
        "contains_lines",
        "contains_buses",
    ]
    if subgraph_data:
        return gpd.GeoDataFrame(subgraph_data, crs=geo_crs)
    return gpd.GeoDataFrame(columns=columns, crs=geo_crs)


def _merge_lines_over_virtual_buses(
    lines: gpd.GeoDataFrame,
    buses: gpd.GeoDataFrame,
    merged_lines_map: gpd.GeoDataFrame,
    distance_crs: str,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Apply ``_create_merge_mapping``'s result: drop the absorbed lines/buses, add the merged ones."""
    lines_merged = lines.copy()
    buses_merged = buses.copy()

    if merged_lines_map.empty:
        lines_merged["contains_lines"] = lines_merged["line_id"].apply(lambda x: [x])
        return lines_merged, buses_merged

    lines_to_remove = merged_lines_map["contains_lines"].explode().unique()
    buses_to_remove = merged_lines_map["contains_buses"].explode().unique()

    lines_merged = lines_merged[~lines_merged["line_id"].isin(lines_to_remove)]
    lines_merged["contains_lines"] = lines_merged["line_id"].apply(lambda x: [x])
    buses_merged = buses_merged[~buses_merged["bus_id"].isin(buses_to_remove)]

    lines_to_add = merged_lines_map.copy().reset_index(drop=True)
    lines_to_add["under_construction"] = False
    lines_to_add["length"] = lines_to_add["geometry"].to_crs(distance_crs).length
    lines_to_add["contains"] = lines_to_add["contains_lines"]
    lines_to_add = lines_to_add[lines_merged.columns]

    lines_merged = pd.concat([lines_merged, lines_to_add], ignore_index=True)
    return lines_merged, buses_merged


def _create_station_seeds(
    buses: gpd.GeoDataFrame,
    buses_polygon: gpd.GeoDataFrame,
    distance_crs: str,
    geo_crs: str,
    tol: float = BUS_TOL,
) -> gpd.GeoDataFrame:
    """Buffer-and-union substations and line endpoints into aggregated station seeds."""
    columns = ["bus_id", "geometry"]
    filtered_buses = buses[
        ~buses["bus_id"].str.startswith("way/")
        & ~buses["bus_id"].str.startswith("relation/")
    ]

    buses_buffer = gpd.GeoDataFrame(
        filtered_buses[columns], geometry="geometry"
    ).set_index("bus_id")
    buses_buffer["geometry"] = (
        buses_buffer["geometry"].to_crs(distance_crs).buffer(tol).to_crs(geo_crs)
    )
    buses_buffer["area"] = buses_buffer.to_crs(distance_crs).area

    buses_polygon_buffer = gpd.GeoDataFrame(
        buses_polygon[columns], geometry="geometry"
    ).set_index("bus_id")
    buses_polygon_buffer["geometry"] = (
        buses_polygon_buffer["geometry"]
        .to_crs(distance_crs)
        .buffer(tol)
        .to_crs(geo_crs)
    )
    buses_polygon_buffer["area"] = buses_polygon_buffer.to_crs(distance_crs).area
    buses_polygon_buffer["poi"] = (
        buses_polygon.set_index("bus_id")["geometry"]
        .to_crs(distance_crs)
        .apply(lambda polygon: polylabel(polygon, tolerance=tol / 2))
        .to_crs(geo_crs)
    )

    buses_all_buffer = pd.concat([buses_buffer, buses_polygon_buffer])
    buses_all_agg = gpd.GeoDataFrame(
        geometry=[poly for poly in buses_all_buffer.union_all().geoms], crs=geo_crs
    )
    buses_all_agg = gpd.sjoin(
        buses_all_agg, buses_all_buffer, how="left", predicate="intersects"
    ).reset_index()
    max_area_idx = buses_all_agg.groupby("index")["area"].idxmax()
    buses_all_agg = buses_all_agg.loc[max_area_idx].drop(columns=["index"])
    buses_all_agg = buses_all_agg.set_index("bus_id")

    poi_missing = buses_all_agg["poi"].isna()
    buses_all_agg.loc[poi_missing, "poi"] = (
        buses_all_agg.loc[poi_missing, "geometry"]
        .to_crs(distance_crs)
        .apply(lambda polygon: polylabel(polygon, tolerance=tol / 2))
        .to_crs(geo_crs)
    )

    buses_all_agg["osm_identifier"] = buses_all_agg.index.str.split("-").str[0]
    buses_all_agg = buses_all_agg.reset_index()
    buses_all_agg["id_occurence"] = buses_all_agg.groupby("osm_identifier")[
        "osm_identifier"
    ].transform("count")
    buses_all_agg.loc[buses_all_agg["id_occurence"] == 1, "bus_id"] = buses_all_agg.loc[
        buses_all_agg["id_occurence"] == 1, "osm_identifier"
    ]
    buses_all_agg = buses_all_agg.set_index("bus_id")
    buses_all_agg.index.name = "station_id"
    buses_all_agg = buses_all_agg.drop(
        columns=["area", "osm_identifier", "id_occurence"]
    )
    buses_all_agg["poi_perimeter"] = (
        buses_all_agg["poi"].to_crs(distance_crs).buffer(tol / 2).to_crs(geo_crs)
    )
    return buses_all_agg.reset_index()


def _merge_buses_to_stations(
    buses: gpd.GeoDataFrame, stations: gpd.GeoDataFrame, distance_crs: str, geo_crs: str
) -> gpd.GeoDataFrame:
    """Keep one bus per (station, voltage); offset multi-voltage stations for visual clarity."""
    buses_all = buses.copy().reset_index(drop=True)
    stations_all = stations.copy().set_index("station_id")
    stations_all["polygon"] = stations_all["geometry"].copy()

    buses_all = gpd.sjoin(buses_all, stations_all, how="left", predicate="within")
    buses_all = buses_all.drop_duplicates(subset=["station_id", "voltage"])

    offset = 15  # metres
    geo_to_dist = Transformer.from_crs(geo_crs, distance_crs, always_xy=True)
    dist_to_geo = Transformer.from_crs(distance_crs, geo_crs, always_xy=True)

    for station_id, group in buses_all.groupby("station_id"):
        voltages = sorted(group["voltage"].unique(), reverse=True)
        not_virtual = ~group.bus_id.str.startswith("virtual_")
        if len(voltages) > 1:
            poi_x, poi_y = geo_to_dist.transform(
                group["poi"].values[0].x, group["poi"].values[0].y
            )
            for idx, voltage in enumerate(voltages):
                poi_x_offset = poi_x + offset * np.sin(
                    np.pi / 4 + 2 * np.pi * idx / len(voltages)
                ).round(4)
                poi_y_offset = poi_y + offset * np.cos(
                    np.pi / 4 + 2 * np.pi * idx / len(voltages)
                ).round(4)
                poi_offset = Point(dist_to_geo.transform(poi_x_offset, poi_y_offset))

                group.loc[(group["voltage"] == voltage) & not_virtual, "bus_id"] = (
                    station_id + "-" + str(int(voltage / 1000))
                )
                group.loc[group["voltage"] == voltage, "geometry"] = poi_offset

            buses_all.loc[group.index, "bus_id"] = group["bus_id"]
            buses_all.loc[group.index, "geometry"] = group["geometry"]
        else:
            voltage = voltages[0]
            buses_all.loc[group.loc[not_virtual].index, "bus_id"] = (
                station_id + "-" + str(int(voltage / 1000))
            )
            buses_all.loc[group.index, "geometry"] = group["poi"]

    return buses_all


def _identify_linestring_between_polygons(
    multiline: Any, polygon0: Any, polygon1: Any, geo_crs: str, distance_crs: str
) -> Any:
    """Pick the part of ``multiline`` that touches both ``polygon0`` and ``polygon1``, if any."""
    list_lines = (
        list(multiline.geoms)
        if multiline.geom_type == "MultiLineString"
        else [multiline]
    )
    for line in list_lines:
        gdf_line = gpd.GeoDataFrame(geometry=[line], crs=geo_crs).to_crs(distance_crs)
        gdf_p0 = gpd.GeoDataFrame(geometry=[polygon0], crs=geo_crs).to_crs(distance_crs)
        gdf_p1 = gpd.GeoDataFrame(geometry=[polygon1], crs=geo_crs).to_crs(distance_crs)
        touches = gdf_line.intersects(gdf_p0.buffer(1e-2)) & gdf_line.intersects(
            gdf_p1.buffer(1e-2)
        )
        if touches.any():
            return line
    return multiline


def _map_endpoints_to_buses(
    connection: gpd.GeoDataFrame,
    buses: gpd.GeoDataFrame,
    distance_crs: str,
    geo_crs: str,
    shape: str = "station_polygon",
    id_col: str = "line_id",
) -> gpd.GeoDataFrame:
    """Map each line's two endpoints to the station polygon (and bus) they fall within."""
    buses_all = buses.copy().set_index("bus_id")
    buses_all["station_polygon"] = buses_all["polygon"].copy()
    buses_all = gpd.GeoDataFrame(buses_all, geometry="polygon", crs=buses.crs)

    lines_all = connection.copy().set_index(id_col)

    for coord in range(2):
        endpoints = lines_all[["voltage", "geometry"]].copy()
        endpoints["geometry"] = get_point(
            endpoints.geometry.apply(_remove_loops_from_multiline), -1 * coord
        )
        endpoints = gpd.sjoin(endpoints, buses_all, how="left", predicate="intersects")
        endpoints = endpoints[endpoints["voltage_left"] == endpoints["voltage_right"]]
        endpoints = endpoints.drop(columns=["voltage_right"]).rename(
            columns={"voltage_left": "voltage"}
        )

        lines_all[f"poi_perimeter{coord}"] = endpoints["poi_perimeter"]
        lines_all[f"station_polygon{coord}"] = endpoints["station_polygon"]
        lines_all[f"bus{coord}"] = endpoints["bus_id"]

    lines_all["geometry"] = lines_all.apply(
        lambda row: row["geometry"].difference(row[shape + "0"]), axis=1
    )
    lines_all["geometry"] = lines_all.apply(
        lambda row: row["geometry"].difference(row[shape + "1"]), axis=1
    )

    lines_all = lines_all[lines_all["bus0"] != lines_all["bus1"]]

    contains_stubs = lines_all["geometry"].apply(
        lambda x: isinstance(x, MultiLineString)
    )
    if contains_stubs.any():
        lines_stubs = lines_all.loc[contains_stubs].copy()
        lines_stubs["geometry"] = lines_stubs["geometry"].apply(
            _remove_loops_from_multiline
        )
        if shape == "station_polygon":
            lines_stubs["geometry"] = lines_stubs.apply(
                lambda row: _identify_linestring_between_polygons(
                    row["geometry"],
                    row[f"{shape}0"],
                    row[f"{shape}1"],
                    geo_crs=geo_crs,
                    distance_crs=distance_crs,
                ),
                axis=1,
            )
            lines_all.loc[lines_stubs.index, "geometry"] = lines_stubs["geometry"]

    lines_all = lines_all.reset_index()
    lines_all = lines_all.drop(
        columns=[
            "poi_perimeter0",
            "poi_perimeter1",
            "station_polygon0",
            "station_polygon1",
        ]
    )
    return lines_all


def _add_point_to_line(linestring: LineString, point: Point) -> LineString:
    """Extend ``linestring`` with a stub segment to ``point``, snapped to its nearer end."""
    start = linestring.boundary.geoms[0]
    end = linestring.boundary.geoms[1]
    dist_to_start = point.distance(start)
    dist_to_end = point.distance(end)
    new_segment = LineString([point, start if dist_to_start < dist_to_end else end])
    return linemerge([linestring, new_segment])


def _extend_lines_to_buses(
    connection: gpd.GeoDataFrame, buses: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    """Extend each line's geometry to literally touch its mapped bus points."""
    lines_all = connection.copy()
    buses_all = buses.copy()

    lines_all = lines_all.merge(
        buses_all[["geometry", "bus_id"]],
        left_on="bus0",
        right_on="bus_id",
        how="left",
        suffixes=("", "_right"),
    )
    lines_all = lines_all.drop(columns=["bus_id"]).rename(
        columns={"geometry_right": "bus0_point"}
    )
    lines_all = lines_all.merge(
        buses_all[["geometry", "bus_id"]],
        left_on="bus1",
        right_on="bus_id",
        how="left",
        suffixes=("", "_right"),
    )
    lines_all = lines_all.drop(columns=["bus_id"]).rename(
        columns={"geometry_right": "bus1_point"}
    )

    b_multi = lines_all["geometry"].apply(lambda x: isinstance(x, MultiLineString))
    if b_multi.any():
        lines_all = lines_all[~b_multi]

    lines_all["geometry"] = lines_all.apply(
        lambda row: _add_point_to_line(row["geometry"], row["bus0_point"]), axis=1
    )
    lines_all["geometry"] = lines_all.apply(
        lambda row: _add_point_to_line(row["geometry"], row["bus1_point"]), axis=1
    )
    return lines_all.drop(columns=["bus0_point", "bus1_point"])


def _add_transformers(buses: gpd.GeoDataFrame, geo_crs: str) -> gpd.GeoDataFrame:
    """All-pairs transformers between voltage-level buses of the same real station."""
    buses_all = buses.copy().set_index("bus_id")
    columns = ["bus0", "bus1", "voltage_bus0", "voltage_bus1", "station_id", "geometry"]
    all_transformers = gpd.GeoDataFrame(
        columns=[*columns, "transformer_id"], crs=geo_crs
    )

    for station_id, group in buses_all.groupby("station_id"):
        if group["voltage"].nunique() <= 1:
            continue
        pairs = list(
            combinations(group.sort_values("voltage", ascending=False).index, 2)
        )
        station_transformers = pd.DataFrame(pairs, columns=["bus0", "bus1"])
        station_transformers["voltage_bus0"] = station_transformers["bus0"].map(
            group["voltage"]
        )
        station_transformers["voltage_bus1"] = station_transformers["bus1"].map(
            group["voltage"]
        )
        station_transformers["geometry"] = station_transformers.apply(
            lambda row: LineString(
                [group.loc[row["bus0"]]["geometry"], group.loc[row["bus1"]]["geometry"]]
            ),
            axis=1,
        )
        station_transformers["station_id"] = station_id
        station_transformers["transformer_id"] = (
            station_id
            + "-"
            + station_transformers["voltage_bus0"].div(1e3).astype(int).astype(str)
            + "-"
            + station_transformers["voltage_bus1"].div(1e3).astype(int).astype(str)
        )
        all_transformers = pd.concat(
            [all_transformers, gpd.GeoDataFrame(station_transformers, crs=geo_crs)]
        ).reset_index(drop=True)

    return all_transformers[["transformer_id", *columns]]


def build_network(
    substations: gpd.GeoDataFrame,
    substations_polygon: gpd.GeoDataFrame,
    lines: gpd.GeoDataFrame,
    remove_under_construction: bool,
    remove_after: str | None,
    geo_crs: str,
    distance_crs: str,
    station_merge_radius_m: float = BUS_TOL,
) -> tuple[
    gpd.GeoDataFrame,
    gpd.GeoDataFrame,
    gpd.GeoDataFrame,
    gpd.GeoDataFrame,
    gpd.GeoDataFrame,
]:
    """Create buses, AC lines, and transformers from clean's output.

    Also returns two polygon views for visualisation: ``stations_polygon``
    (the clustered station shapes from station-seed buffering, keyed by
    ``station_id``) and ``buses_polygon`` (the substation polygons scoped to
    the buses that made it into the output, keyed by ``bus_id``).
    """
    buses = substations.drop(columns=["country"])
    buses = _treat_under_construction(
        buses, remove_under_construction, remove_after
    ).drop(columns=["start_date"])

    buses_polygon = substations_polygon[
        substations_polygon["bus_id"].isin(buses["bus_id"])
    ].copy()
    buses_polygon["bus_id"] = buses_polygon["bus_id"].apply(lambda x: x.split("-")[0])
    buses_polygon = buses_polygon.drop_duplicates(subset=["bus_id", "geometry"])
    if "voltage" in buses_polygon.columns:
        buses_polygon = buses_polygon.drop(columns=["voltage"])

    if lines.empty:
        empty_buses = _empty_geodataframe(["bus_id", "geometry"], crs=geo_crs)
        empty_lines = _empty_geodataframe(["line_id", "geometry"], crs=geo_crs)
        empty_transformers = _empty_geodataframe(
            ["transformer_id", "geometry"], crs=geo_crs
        )
        empty_stations_polygon = _empty_geodataframe(
            ["station_id", "geometry"], crs=geo_crs
        )
        return (
            empty_buses,
            empty_lines,
            empty_transformers,
            empty_stations_polygon,
            buses_polygon,
        )

    lines = _treat_under_construction(
        lines, remove_under_construction, remove_after
    ).drop(columns=["start_date"])
    lines = _merge_identical_lines(lines)

    buses["voltage"] = (np.floor(buses["voltage"] / 1000) * 1000).astype(
        buses["voltage"].dtype
    )
    lines["voltage"] = (np.floor(lines["voltage"] / 1000) * 1000).astype(
        lines["voltage"].dtype
    )

    buses_line_endings = _add_line_endings(lines)
    buses = pd.concat([buses, buses_line_endings], ignore_index=True)

    lines = split_overpassing_lines(lines, buses, distance_crs=distance_crs)

    bool_virtual = buses["bus_id"].str.startswith("virtual")
    buses = buses[~bool_virtual]
    buses = pd.concat([buses, _add_line_endings(lines)], ignore_index=True)

    lines["length"] = lines.to_crs(distance_crs).length

    merged_lines_map = _create_merge_mapping(
        lines, buses, buses_polygon, geo_crs=geo_crs
    )
    lines, buses = _merge_lines_over_virtual_buses(
        lines, buses, merged_lines_map, distance_crs=distance_crs
    )

    stations = _create_station_seeds(
        buses,
        buses_polygon,
        distance_crs=distance_crs,
        geo_crs=geo_crs,
        tol=station_merge_radius_m,
    )
    buses = _merge_buses_to_stations(
        buses, stations, distance_crs=distance_crs, geo_crs=geo_crs
    )

    buses["geometry"] = gpd.points_from_xy(
        buses.geometry.x.round(COORD_PRECISION),
        buses.geometry.y.round(COORD_PRECISION),
        crs=buses.crs,
    )
    buses["poi"] = gpd.points_from_xy(
        buses.poi.x.round(COORD_PRECISION),
        buses.poi.y.round(COORD_PRECISION),
        crs=buses.crs,
    )

    internal_lines = gpd.sjoin(lines, stations, how="inner", predicate="within").line_id
    lines = lines[~lines.line_id.isin(internal_lines)].reset_index(drop=True)

    lines = _map_endpoints_to_buses(
        lines,
        buses,
        shape="station_polygon",
        id_col="line_id",
        distance_crs=distance_crs,
        geo_crs=geo_crs,
    )
    lines = _extend_lines_to_buses(lines, buses)

    bool_not_connected = ~(
        buses["bus_id"].isin(lines["bus0"]) | buses["bus_id"].isin(lines["bus1"])
    )
    logger.info(
        "Dropping %d buses not connected to any line.", bool_not_connected.sum()
    )
    buses = buses[~bool_not_connected].reset_index(drop=True)

    transformers = _add_transformers(buses, geo_crs=geo_crs)

    lines["length"] = lines.to_crs(distance_crs).length

    # --- Finalise to this project's generic output schema ---------------
    def _contains_to_osm_ids(value: Any) -> str:
        if isinstance(value, list):
            ids = {item.split("-")[0] for item in value if isinstance(item, str)}
        elif isinstance(value, str):
            ids = {value.split("-")[0]}
        else:
            ids = set()
        return ";".join(sorted(ids))

    buses_out = buses.copy()
    buses_out["voltage_kv"] = (buses_out["voltage"] / 1000).astype(int)
    buses_out["osm_ids"] = buses_out["contains"].apply(_contains_to_osm_ids)
    buses_out = buses_out.rename(columns={"station_id": "station_id"})
    buses_out = gpd.GeoDataFrame(
        buses_out[["bus_id", "station_id", "voltage_kv", "osm_ids", "geometry"]],
        geometry="geometry",
        crs=geo_crs,
    )

    lines_out = lines.copy()
    lines_out["voltage_kv"] = (lines_out["voltage"] / 1000).astype(int)
    lines_out["osm_ids"] = lines_out["contains_lines"].apply(_contains_to_osm_ids)
    lines_out["length_m"] = lines_out["length"].round(2)
    lines_out = gpd.GeoDataFrame(
        lines_out[
            [
                "line_id",
                "bus0",
                "bus1",
                "voltage_kv",
                "circuits",
                "length_m",
                "underground",
                "osm_ids",
                "geometry",
            ]
        ],
        geometry="geometry",
        crs=geo_crs,
    )

    transformers_out = transformers.copy()
    if not transformers_out.empty:
        transformers_out["voltage_bus0_kv"] = (
            transformers_out["voltage_bus0"] / 1000
        ).astype(int)
        transformers_out["voltage_bus1_kv"] = (
            transformers_out["voltage_bus1"] / 1000
        ).astype(int)
        transformers_out = gpd.GeoDataFrame(
            transformers_out[
                [
                    "transformer_id",
                    "station_id",
                    "bus0",
                    "bus1",
                    "voltage_bus0_kv",
                    "voltage_bus1_kv",
                    "geometry",
                ]
            ],
            geometry="geometry",
            crs=geo_crs,
        )
    else:
        transformers_out = _empty_geodataframe(
            [
                "transformer_id",
                "station_id",
                "bus0",
                "bus1",
                "voltage_bus0_kv",
                "voltage_bus1_kv",
            ],
            crs=geo_crs,
        )

    stations_polygon_out = stations[["station_id", "geometry"]].copy()
    buses_polygon_out = buses_polygon.copy()

    return (
        buses_out,
        lines_out,
        transformers_out,
        stations_polygon_out,
        buses_polygon_out,
    )


def _write_components(
    components: gpd.GeoDataFrame, csv_path: str, geojson_path: str
) -> None:
    """Write ``components`` as both a WKT-geometry CSV and a GeoJSON file."""
    csv = pd.DataFrame(components.drop(columns="geometry"))
    csv["geometry"] = components.geometry.to_wkt()
    csv.to_csv(csv_path, index=False)
    components.to_file(geojson_path, driver="GeoJSON")


if __name__ == "__main__":
    if "snakemake" not in globals():
        from scripts._helpers import mock_snakemake

        snakemake = mock_snakemake("build_network")

    configure_logging(snakemake.log[0])
    buses, lines, transformers, stations_polygon, buses_polygon = build_network(
        gpd.read_file(snakemake.input.substations),
        gpd.read_file(snakemake.input.substations_polygon),
        gpd.read_file(snakemake.input.lines),
        snakemake.params.remove_under_construction,
        snakemake.params.remove_after,
        snakemake.params.crs["geo"],
        snakemake.params.crs["distance"],
        snakemake.params.station_merge_radius_m,
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
    stations_polygon.to_file(snakemake.output.stations_polygon, driver="GeoJSON")
    buses_polygon.to_file(snakemake.output.buses_polygon, driver="GeoJSON")
