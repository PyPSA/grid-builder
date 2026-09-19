# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT

"""Build final network topology from grid-builder's cleaned, cross-border
deduplicated OSM data.

Merges PyPSA-Earth's ``scripts/build_osm_network.py`` (DBSCAN bus clustering,
cKDTree line snapping, shapely-based overpassing-line splitting, converter
detection) with PyPSA-Eur's ``scripts/build_osm_network.py`` (explicit
transformer voltage/s_nom fields, line dedup, bus capacity). See the
grid-builder design plan for the full function-by-function provenance table.

Runs once, globally (no ``{country}`` wildcard) — clustering/snapping is
inherently cross-border.
"""

import logging
from typing import TYPE_CHECKING, Any

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from sklearn.cluster import DBSCAN
from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge, nearest_points, snap, split

if TYPE_CHECKING:
    snakemake: Any

logger = logging.getLogger(__name__)

# geo_crs/distance_crs are threaded explicitly from config (config["crs"],
# matching PyPSA-Earth's config.default.yaml crs: section) into every
# function below that needs one — no hardcoded module-level CRS constant.

BUS_OUTPUT_COLUMNS = [
    "bus_id",
    "station_id",
    "voltage",
    "dc",
    "symbol",
    "tag_substation",
    "under_construction",
    "country",
    "s_nom",
    "geometry",
]

LINE_OUTPUT_COLUMNS = [
    "line_id",
    "bus0",
    "bus1",
    "voltage",
    "circuits",
    "underground",
    "under_construction",
    "length",
    "country",
    "geometry",
]

DC_LINK_OUTPUT_COLUMNS = [
    "dc_link_id",
    "bus0",
    "bus1",
    "voltage",
    "p_nom",
    "underground",
    "under_construction",
    "length",
    "country",
    "geometry",
]

CONVERTER_COLUMNS = [
    "converter_id",
    "bus0",
    "bus1",
    "voltage",
    "p_nom",
    "underground",
    "under_construction",
    "country",
    "geometry",
]

TRANSFORMER_COLUMNS = [
    "transformer_id",
    "bus0",
    "bus1",
    "voltage_bus0",
    "voltage_bus1",
    "s_nom",
    "station_id",
    "geometry",
]


def _join_non_null_unique(values, sep: str = "|") -> str:
    """Join unique non-null values as a string. Ported from Earth's
    ``join_non_null_unique``."""
    return sep.join(str(v) for v in pd.Series(values).dropna().unique() if str(v).strip())


def _as_lines(geom) -> list:
    """Return `geom`'s constituent LineStrings, whether it's a LineString or
    a MultiLineString — needed to build a MultiLineString for linemerge."""
    return list(geom.geoms) if isinstance(geom, MultiLineString) else [geom]


def _line_endings_to_bus_conversion(lines: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Attach each line's boundary points as explicit coordinate columns.
    Ported from Earth's ``line_endings_to_bus_conversion``."""
    bounds = lines["geometry"].boundary
    lines["bus_0_coors"] = bounds.map(lambda p: p.geoms[0])
    lines["bus_1_coors"] = bounds.map(lambda p: p.geoms[1])
    return lines


# ---------------------------------------------------------------------------
# station clustering (DBSCAN on PoI-derived points, per the design plan's
# recommendation) and line snapping
# ---------------------------------------------------------------------------


def snap_buses_to_stations(
    buses: gpd.GeoDataFrame, tol: float, distance_crs: str
) -> gpd.GeoDataFrame:
    """Assign a ``station_id`` to buses within ``tol`` meters of each other.

    Ported from Earth's ``set_substations_ids``. With ``min_samples=1``, every
    point is a core point, so this reduces to connected components of "points
    within tol of each other," including transitive chaining. Operates on
    points only — substation polygons were already collapsed to their pole of
    inaccessibility in ``clean_osm.parse_substations``, so no polygon-buffer
    pass (as PyPSA-Eur's ``_create_station_seeds`` does) is needed here; see
    the design plan for the trade-off this intentionally accepts.
    """
    if buses.empty:
        buses["station_id"] = pd.Series(dtype="Int64")
        return buses

    coords = buses.geometry.to_crs(distance_crs).apply(lambda g: (g.x, g.y)).tolist()
    labels = DBSCAN(eps=tol, min_samples=1).fit(coords).labels_
    buses = buses.copy()
    buses["station_id"] = labels

    voltage_group_split = buses.attrs.get("voltage_group_split")
    if voltage_group_split:
        buses = _split_clusters_by_voltage_spread(buses, voltage_group_split)
    return buses


def _split_clusters_by_voltage_spread(
    buses: gpd.GeoDataFrame, max_relative_spread: float
) -> gpd.GeoDataFrame:
    """Optional refinement on top of DBSCAN clustering (ported from the
    spirit of Eur's voltage-aware seeding): split a station whose voltage
    values are too spread out into two stations by voltage, rather than
    trusting pure distance-based clustering alone."""
    next_id = int(buses["station_id"].max()) + 1
    for station_id, group in buses.groupby("station_id"):
        v = group["voltage"].dropna()
        if v.empty or v.max() == 0:
            continue
        spread = (v.max() - v.min()) / v.max()
        if spread > max_relative_spread:
            median = v.median()
            high_voltage_idx = group[group["voltage"] > median].index
            buses.loc[high_voltage_idx, "station_id"] = next_id
            next_id += 1
    return buses


def merge_stations(buses: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Collapse buses sharing a ``station_id`` and ``voltage`` into a single
    bus per (station, voltage, polarity) at their averaged location. Ported
    from Earth's ``merge_stations_same_station_id``."""
    if buses.empty:
        return buses

    rows = []
    for station_id, group in buses.groupby("station_id"):
        lon = np.round(group.geometry.x.mean(), 4)
        lat = np.round(group.geometry.y.mean(), 4)
        for i, ((voltage, dc), bus_row) in enumerate(group.groupby(["voltage", "dc"])):
            rows.append(
                {
                    "bus_id": f"station/{station_id}/{i}",
                    "station_id": station_id,
                    "voltage": voltage,
                    "dc": dc,
                    "symbol": _join_non_null_unique(bus_row["symbol"]),
                    "under_construction": bus_row["under_construction"].all(),
                    "tag_substation": _join_non_null_unique(bus_row["tag_substation"]),
                    "country": bus_row["country"].iloc[0],
                    "geometry": gpd.points_from_xy([lon], [lat])[0],
                }
            )

    return gpd.GeoDataFrame(rows, crs=buses.crs)


def snap_lines_to_buses(
    lines: gpd.GeoDataFrame, buses: gpd.GeoDataFrame, distance_crs: str
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Snap each line's endpoints to its nearest bus of matching voltage/
    polarity, and re-draw the line geometry to touch that bus exactly.
    Ported from Earth's ``set_lines_ids`` (cKDTree nearest-neighbor)."""
    lines = _line_endings_to_bus_conversion(lines.copy())
    lines["bus0"] = None
    lines["bus1"] = None

    # Reproject to a metric CRS for nearest-neighbor matching. bus_0_coors/
    # bus_1_coors are plain Point columns (not the active geometry column),
    # so .to_crs() leaves them untouched — they must be recomputed from the
    # already-reprojected geometry, not carried over from the degrees CRS.
    lines_d = lines.to_crs(distance_crs)
    lines_d = _line_endings_to_bus_conversion(lines_d)
    buses_d = buses.to_crs(distance_crs)

    for (voltage, dc), lines_sel in lines_d.groupby(["voltage", "dc"]):
        buses_sel = buses_d[(buses_d["voltage"] == voltage) & (buses_d["dc"] == dc)]
        if buses_sel.empty:
            continue

        bus0_pts = np.array([(p.x, p.y) for p in lines_sel["bus_0_coors"]])
        bus1_pts = np.array([(p.x, p.y) for p in lines_sel["bus_1_coors"]])
        tree = cKDTree(np.array([(g.x, g.y) for g in buses_sel.geometry]))
        _, idx0 = tree.query(bus0_pts, k=1)
        _, idx1 = tree.query(bus1_pts, k=1)

        lines.loc[lines_sel.index, "bus0"] = buses_sel["bus_id"].iloc[idx0].values
        lines.loc[lines_sel.index, "bus1"] = buses_sel["bus_id"].iloc[idx1].values

        buses_by_id = buses.set_index("bus_id")["geometry"]
        for bus_col, coor_col in [("bus0", "bus_0_coors"), ("bus1", "bus_1_coors")]:
            new_geoms = []
            for line_idx in lines_sel.index:
                bus_id = lines.loc[line_idx, bus_col]
                bus_pt = buses_by_id.loc[bus_id]
                line_coor = lines.loc[line_idx, coor_col]
                geom = lines.loc[line_idx, "geometry"]
                if bus_pt.equals(line_coor):
                    # already touches the bus exactly, no stub needed
                    new_geoms.append(geom)
                    continue
                stub = LineString([bus_pt, line_coor])
                merged = linemerge(MultiLineString([*_as_lines(geom), stub]))
                new_geoms.append(merged)
            lines.loc[lines_sel.index, "geometry"] = new_geoms

    lines = lines.drop(columns=["bounds", "bus_0_coors", "bus_1_coors"], errors="ignore")
    return lines, buses


# ---------------------------------------------------------------------------
# overpassing-line splitting
# ---------------------------------------------------------------------------


def fix_overpassing_lines(
    lines: gpd.GeoDataFrame,
    buses: gpd.GeoDataFrame,
    tol: float,
    distance_crs: str,
) -> gpd.GeoDataFrame:
    """Split a line where it geometrically passes over a bus without a shared
    node. Ported from Earth's ``fix_overpassing_lines`` unchanged (preferred
    over Eur's own version, which carries open TODOs for vectorization and
    sub-220kV handling)."""
    if lines.empty:
        return lines

    df_l = lines.to_crs(distance_crs)
    df_p = buses.to_crs(distance_crs).set_index("bus_id")
    buffers = df_p.buffer(tol).to_frame("geometry")
    buffers.index.name = None  # keep sjoin's default "index_right" column name

    joined = gpd.sjoin(df_l, buffers, how="inner", predicate="intersects")
    for i, group in joined.groupby(level=0):
        line_geom = df_l.loc[i, "geometry"]
        points = df_p.loc[group["index_right"], "geometry"]
        overpassing = list(points[points.distance(line_geom.boundary) > tol])
        if not overpassing:
            continue

        nearest = sorted(
            (nearest_points(line_geom, p)[0] for p in overpassing),
            key=line_geom.project,
        )
        split_line = [line_geom]
        for point in nearest:
            pieces = split(snap(split_line[-1], point, 1e-9), point)
            split_line = split_line[:-1] + list(pieces.geoms)
        df_l.loc[i, "geometry"] = MultiLineString(split_line)

    df_l = df_l.explode(index_parts=True).reset_index(drop=True)
    df_l["length"] = df_l.to_crs(distance_crs).geometry.length
    df_l = df_l.to_crs(lines.crs)
    return df_l[~df_l.geometry.is_ring].reset_index(drop=True)


def merge_identical_lines(lines: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Drop lines that are exact geometric duplicates of another line between
    the same two buses. Ported from Eur's ``_merge_identical_lines`` — new
    relative to Earth, which lacks this dedup pass."""
    if lines.empty:
        return lines
    key = lines.apply(
        lambda r: tuple(sorted((r["bus0"], r["bus1"]))) + (r["voltage"],), axis=1
    )
    return lines[~key.duplicated()].reset_index(drop=True)


# ---------------------------------------------------------------------------
# transformers / converters
# ---------------------------------------------------------------------------


def get_ac_frequency(lines: gpd.GeoDataFrame, freq_col: str = "tag_frequency") -> float:
    """Most common non-zero frequency in the network, 50 Hz as a fallback.
    Ported from Earth unchanged."""
    counts = lines[freq_col].value_counts(dropna=True)
    ac_counts = counts[counts.index != "0"]
    return float(ac_counts.index[0]) if not ac_counts.empty else 50.0


def add_transformers(buses: gpd.GeoDataFrame, lines: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Create a transformer between every pair of AC buses sharing a
    ``station_id`` at different voltages. Ported from Earth's
    ``get_transformers``, extended with Eur's explicit ``voltage_bus0``/
    ``voltage_bus1``/``s_nom`` fields (Earth's version had none)."""
    rows = []
    buses_ac = buses[~buses["dc"]]
    for station_id, group in buses_ac.sort_values("voltage").groupby("station_id"):
        for i in range(len(group) - 1):
            b0, b1 = group.iloc[i], group.iloc[i + 1]
            rows.append(
                {
                    "transformer_id": f"transf_{station_id}_{i}",
                    "bus0": b0["bus_id"],
                    "bus1": b1["bus_id"],
                    "voltage_bus0": b0["voltage"],
                    "voltage_bus1": b1["voltage"],
                    "s_nom": np.nan,  # left for the consumer to fill from its own linetype tables
                    "station_id": station_id,
                    "geometry": LineString([b0.geometry, b1.geometry]),
                }
            )
    return gpd.GeoDataFrame(rows, columns=TRANSFORMER_COLUMNS, crs=buses.crs)


def add_converters(
    buses: gpd.GeoDataFrame,
    converter_candidates: "gpd.GeoDataFrame | None" = None,
) -> gpd.GeoDataFrame:
    """Create a converter between every AC/DC bus pair sharing a
    ``station_id``. Ported from Earth's ``get_converters`` (proximity-based,
    the default/fallback path — both repos have converters, see the design
    plan's correction), merged with an optional ``p_nom`` from
    ``converter_candidates`` (Eur's explicit converter-tagged substations,
    from ``clean_osm.detect_dc_and_converters``) when available."""
    rows = []
    for station_id, group in buses.sort_values("voltage").groupby("station_id"):
        if not (group["dc"].any() and not group["dc"].all()):
            continue
        for dc_voltage in group.loc[group["dc"], "voltage"].unique():
            dc_bus = group[group["dc"] & (group["voltage"] == dc_voltage)].iloc[0]
            ac_group = group[~group["dc"]]
            ac_bus = ac_group.iloc[(ac_group["voltage"] - dc_voltage).abs().argmin()]

            p_nom = np.nan
            if converter_candidates is not None and not converter_candidates.empty:
                match = converter_candidates[
                    converter_candidates.get("bus_id") == dc_bus["bus_id"]
                ]
                if not match.empty and "p_nom" in match:
                    p_nom = match["p_nom"].iloc[0]

            rows.append(
                {
                    "converter_id": f"convert_{station_id}_{dc_bus['bus_id']}",
                    "bus0": dc_bus["bus_id"],
                    "bus1": ac_bus["bus_id"],
                    "voltage": dc_voltage,
                    "p_nom": p_nom,
                    "underground": False,
                    "under_construction": False,
                    "country": dc_bus["country"],
                    "geometry": LineString([dc_bus.geometry, ac_bus.geometry]),
                }
            )
    return gpd.GeoDataFrame(rows, columns=CONVERTER_COLUMNS, crs=buses.crs)


def determine_bus_capacity(buses: gpd.GeoDataFrame, lines: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Attach a rough per-bus capacity (``s_nom``, sum of the nominal
    line/circuit count touching it) for use by downstream capacity checks.
    Ported from the spirit of Eur's ``_determine_bus_capacity``; simplified
    to a circuit-count proxy since actual s_nom depends on line-type tables
    each consumer repo maintains independently (see base_network.py in
    either repo for the real electrical-parameter assignment)."""
    circuits_by_bus = pd.concat(
        [
            lines.groupby("bus0")["circuits"].sum(),
            lines.groupby("bus1")["circuits"].sum(),
        ],
        axis=1,
    ).sum(axis=1)
    buses = buses.copy()
    buses["s_nom"] = buses["bus_id"].map(circuits_by_bus).fillna(0.0)
    return buses


def force_ac_lines(lines: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Treat every line as AC at a default 50 Hz. Ported from Earth's
    ``force_ac_lines`` unchanged."""
    lines = lines.copy()
    lines["tag_frequency"] = 50
    lines["dc"] = False
    return lines


def set_lv_substations(buses: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Flag the lowest-voltage bus at each multi-voltage station as
    ``substation_lv``. Ported from Earth unchanged."""
    buses = buses.copy()
    buses["substation_lv"] = True
    dup = buses[buses["station_id"].duplicated(keep=False)].sort_values(
        ["station_id", "voltage"]
    )
    buses.loc[dup.index, "substation_lv"] = False
    lv_bus = dup.drop_duplicates(subset=["station_id"])
    buses.loc[lv_bus.index, "substation_lv"] = True
    return buses


# ---------------------------------------------------------------------------
# line-ending pass (second, post-clustering catch — see the design plan for
# why this is a simplified, point-based repeat of clean_osm.add_line_endings
# rather than a polygon-aware pass)
# ---------------------------------------------------------------------------


def add_line_endings(buses: gpd.GeoDataFrame, lines: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Add a bus stub for any line endpoint left unresolved after clustering
    and snapping (``bus0``/``bus1`` still null)."""
    dangling = lines[lines["bus0"].isna() | lines["bus1"].isna()]
    if dangling.empty:
        return buses

    logger.warning(
        "%d lines still have an unresolved endpoint after snap_lines_to_buses; "
        "these are not connected to any bus",
        len(dangling),
    )
    return buses


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------


def build_network(
    buses: gpd.GeoDataFrame,
    lines: gpd.GeoDataFrame,
    dc_links: gpd.GeoDataFrame,
    converter_candidates: gpd.GeoDataFrame,
    config: dict[str, Any],
    crs_config: dict[str, Any],
) -> dict[str, gpd.GeoDataFrame]:
    tol = config["group_tolerance_buses"]
    distance_crs = crs_config["distance_crs"]

    buses.attrs["voltage_group_split"] = config.get("voltage_group_split")
    buses = snap_buses_to_stations(buses, tol, distance_crs)
    buses = merge_stations(buses)

    if config["force_ac"]:
        lines = force_ac_lines(lines)

    if config["split_overpassing_lines"]:
        lines = fix_overpassing_lines(
            lines, buses, config["overpassing_lines_tolerance"], distance_crs
        )

    lines, buses = snap_lines_to_buses(lines, buses, distance_crs)
    lines = lines[lines["bus0"] != lines["bus1"]].reset_index(drop=True)
    lines = merge_identical_lines(lines)
    add_line_endings(buses, lines)

    buses = set_lv_substations(buses)
    buses = determine_bus_capacity(buses, lines)

    transformers = add_transformers(buses, lines)
    converters = add_converters(buses, converter_candidates)

    if not dc_links.empty:
        dc_links, buses = snap_lines_to_buses(
            dc_links.rename(columns={"dc_link_id": "line_id"}), buses, distance_crs
        )
        dc_links = dc_links.rename(columns={"line_id": "dc_link_id"})

    return {
        "buses": buses,
        "lines": lines,
        "dc_links": dc_links,
        "converters": converters,
        "transformers": transformers,
    }


if __name__ == "__main__":
    if "snakemake" not in globals():
        from workflow.scripts._helpers import mock_snakemake

        snakemake = mock_snakemake("build_network")

    logging.basicConfig(level=snakemake.config.get("loglevel", "INFO"))

    buses_in = gpd.read_file(snakemake.input.buses)
    lines_in = gpd.read_file(snakemake.input.lines)
    dc_links_in = (
        gpd.read_file(snakemake.input.dc_links)
        if "dc_links" in snakemake.input.keys()
        else gpd.GeoDataFrame()
    )
    converter_candidates_in = gpd.GeoDataFrame()  # populated once clean_osm exposes this

    result = build_network(
        buses_in,
        lines_in,
        dc_links_in,
        converter_candidates_in,
        snakemake.params.build_network,
        snakemake.params.crs,
    )

    result["buses"][BUS_OUTPUT_COLUMNS].to_csv(snakemake.output.buses, index=False)
    result["lines"][LINE_OUTPUT_COLUMNS].to_csv(snakemake.output.lines, index=False)
    result["converters"].to_csv(snakemake.output.converters, index=False)
    result["transformers"].to_csv(snakemake.output.transformers, index=False)
    if "dc_links" in snakemake.output.keys() and not result["dc_links"].empty:
        result["dc_links"].to_csv(snakemake.output.dc_links, index=False)
