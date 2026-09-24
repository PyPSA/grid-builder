# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT

"""Clean raw OSM power features into generic high-voltage grid inputs.

The tag-cleaning and topology functions below (``_clean_voltage`` through
``_create_line``) are ported near-verbatim from PyPSA-Eur's
``scripts/clean_osm_data.py`` (fix-osm branch): they operate purely on
pandas/shapely data, so they transfer unchanged. What's adapted is the I/O
boundary — earth-osm's flattened CSV exports for substations/lines/cables
(instead of PyPSA-Eur's raw Overpass JSON dumps) and this project's own
Overpass-only relation retrieval — plus everything downstream of PyPSA
network construction (line-type capacities, DC links/converters), which is
out of this project's scope.

Voltage, unlike this project's earlier hand-rolled parser, is kept in raw
volts as a string throughout cleaning (matching the reference), and is only
floored to whole kV at the very end.
"""

import itertools
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.algorithms.polylabel import polylabel
from shapely.geometry import LineString, MultiLineString, Point, Polygon
from shapely.ops import linemerge, unary_union

if TYPE_CHECKING:
    snakemake: Any

logger = logging.getLogger(__name__)

GEO_CRS = "EPSG:4326"
BUS_TOL = 500  # metres; matches PyPSA-Eur's station merge tolerance


def configure_logging(log_path: str) -> None:
    """Send rule and dependency logging to the Snakemake log file."""
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Ported from PyPSA-Eur's scripts/clean_osm_data.py (fix-osm branch).
# ---------------------------------------------------------------------------


def _create_linestring(row: pd.Series) -> LineString:
    coords = [(coord["lon"], coord["lat"]) for coord in row["geometry"]]
    return LineString(coords)


def _create_polygon(row: pd.Series) -> Polygon:
    point_coords = [(coord["lon"], coord["lat"]) for coord in row["geometry"]]
    if point_coords[0] != point_coords[-1]:
        point_coords.append(point_coords[0])
    return Polygon(point_coords)


def _to_str(column: pd.Series) -> pd.Series:
    """Convert a raw OSM tag column to strings, with missing values as empty strings."""
    return column.fillna("").astype(str)


def _clean_voltage(column: pd.Series) -> pd.Series:
    column = (
        _to_str(column)
        .str.lower()
        .str.replace("400/220/110 kV'", "400000;220000;110000")
        .str.replace("400/220/110/20_kv", "400000;220000;110000;20000")
        .str.replace("2x25000", "25000;25000")
        .str.replace("é", ";")
        .str.replace("kvv/", "")
        .str.replace("11000l400", "11000")
    )
    column = (
        column.str.lower()
        .str.replace("(temp 150000)", "")
        .str.replace("low", "1000")
        .str.replace("minor", "1000")
        .str.replace("medium", "33000")
        .str.replace("med", "33000")
        .str.replace("m", "33000")
        .str.replace("high", "150000")
        .str.replace("23000-109000", "109000")
        .str.replace("380000>220000", "380000;220000")
        .str.replace(":", ";")
        .str.replace("<", ";")
        .str.replace(",", ";")
        .str.replace("kv", "000")
        .str.replace("kva", "000")
        .str.replace("/", ";")
    )
    column = column.str.replace(r"[^0-9;]", "", regex=True)
    return column


def _clean_circuits(column: pd.Series) -> pd.Series:
    column = (
        _to_str(column)
        .str.replace("partial", "")
        .str.replace("1operator=RTE operator:wikidata=Q2178795", "")
        .str.lower()
        .str.replace("1,5", "3")
        .str.replace("1/3", "1")
    )
    column = column.str.replace(r"[^0-9;]", "", regex=True)
    return column


def _clean_cables(column: pd.Series) -> pd.Series:
    column = (
        _to_str(column).str.lower().str.replace("1/3", "1").str.replace("3x2;2", "3")
    )
    column = column.str.replace(r"[^0-9;]", "", regex=True)
    return column


def _clean_wires(column: pd.Series) -> pd.Series:
    column = (
        _to_str(column)
        .str.lower()
        .str.replace("?", "")
        .str.replace("trzyprzewodowe", "3")
        .str.replace("pojedyńcze", "1")
        .str.replace("single", "1")
        .str.replace("double", "2")
        .str.replace("triple", "3")
        .str.replace("quad", "4")
        .str.replace("fivefold", "5")
        .str.replace("yes", "3")
        .str.replace("1/3", "1")
        .str.replace("3x2;2", "3")
        .str.replace("_", "")
    )
    column = column.str.replace(r"[^0-9;]", "", regex=True)
    return column


def _check_voltage(voltage: str, list_voltages: Any) -> bool:
    voltages = voltage.split(";")
    return any(v in list_voltages for v in voltages)


def _clean_frequency(column: pd.Series) -> pd.Series:
    column = (
        _to_str(column)
        .str.lower()
        .str.replace("16.67", "16.7")
        .str.replace("16,7", "16.7")
        .str.replace("?", "")
        .str.replace("hz", "")
        .str.replace(" ", "")
    )
    column = column.str.replace(r"[^0-9;.]", "", regex=True)
    return column


def _clean_date(column: pd.Series) -> pd.Series:
    column = (
        _to_str(column)
        .str.lower()
        .str.replace("unknown", "", regex=False)
        .str.replace("approx", "", regex=False)
        .str.replace("c.", "", regex=False)
        .str.replace("circa", "", regex=False)
        .str.replace("about", "", regex=False)
        .str.replace("?", "", regex=False)
        .str.strip()
    )
    column = column.str.replace(r"[^0-9-]", "", regex=True)
    column = column.mask(column == "")
    return pd.to_datetime(column, errors="coerce", format="mixed")


def _split_cells(df: pd.DataFrame, cols: list[str] | None = None) -> pd.DataFrame:
    """Split semicolon-separated cells into new, identically-tagged rows."""
    if cols is None:
        cols = ["voltage"]
    if df.empty:
        return df

    suffix_counts: dict[str, int] = {}

    x = df.assign(**{col: df[col].str.split(";") for col in cols})
    x = x.explode(cols, ignore_index=True)

    num_splits = x.groupby("id").size().to_dict()
    x["split_elements"] = x["id"].map(num_splits)

    def generate_new_id(row: pd.Series) -> str:
        original_id = row["id"]
        if row["split_elements"] == 1:
            return original_id
        suffix_counts[original_id] = suffix_counts.get(original_id, 0) + 1
        return f"{original_id}-{suffix_counts[original_id]}"

    x["id"] = x.apply(generate_new_id, axis=1)
    return x


def _distribute_to_circuits(row: pd.Series) -> str:
    circuits: float
    if row["circuits"] != "":
        circuits = int(row["circuits"])
    else:
        circuits = int(row["cables"]) / 3
    single_circuit = int(max(1, np.floor_divide(circuits, row["split_elements"])))
    return str(single_circuit)


def _drop_duplicate_lines(df_lines: pd.DataFrame) -> pd.DataFrame:
    """Drop duplicate elements (e.g. a cross-border line seen by both countries)."""
    duplicate_rows = df_lines[df_lines.duplicated(subset=["id"], keep=False)].copy()
    if duplicate_rows.empty:
        return df_lines

    grouped_duplicates = (
        duplicate_rows.groupby("id")["country"].agg(lambda x: ";".join(x)).reset_index()
    )
    duplicate_rows.drop_duplicates(subset="id", inplace=True)
    duplicate_rows.drop(columns=["country"], inplace=True)
    duplicate_rows = duplicate_rows.join(
        grouped_duplicates.set_index("id"), on="id", how="left"
    )

    len_before = len(df_lines)
    df_lines = df_lines[~df_lines["id"].isin(duplicate_rows["id"])]
    df_lines = pd.concat([df_lines, duplicate_rows], axis="rows")
    logger.info(
        "Dropped %d duplicate elements. Keeping %d.",
        len_before - len(df_lines),
        len(df_lines),
    )
    return df_lines


def _filter_by_voltage(
    df: pd.DataFrame, min_voltage: float = 220000
) -> tuple[pd.DataFrame, Any]:
    """Keep only rows at or above ``min_voltage`` [V]; return the surviving voltage set too."""
    if df.empty:
        return df, []

    list_voltages = df["voltage"].str.split(";").explode().unique().astype(str)
    list_voltages = list_voltages[np.vectorize(str.isnumeric)(list_voltages)]
    list_voltages = list_voltages.astype(int)
    list_voltages = list_voltages[list_voltages >= int(min_voltage)]
    list_voltages = list_voltages.astype(str)

    bool_voltages = df["voltage"].apply(_check_voltage, list_voltages=list_voltages)
    len_before = len(df)
    df = df[bool_voltages]
    logger.info(
        "Dropped %d elements below %s V. Keeping %d.",
        len_before - len(df),
        min_voltage,
        len(df),
    )
    return df, list_voltages


def _clean_substations(
    df_substations: pd.DataFrame, list_voltages: Any
) -> pd.DataFrame:
    df_substations = df_substations.copy()
    df_substations = _split_cells(df_substations)

    bool_voltages = df_substations["voltage"].apply(
        _check_voltage, list_voltages=list_voltages
    )
    df_substations = df_substations[bool_voltages]
    df_substations["split_count"] = df_substations["id"].apply(
        lambda x: x.split("-")[1] if "-" in x else "0"
    )
    df_substations["split_count"] = df_substations["split_count"].astype(int)

    bool_split = df_substations["split_elements"] > 1
    bool_frequency_len = (
        df_substations["frequency"].apply(lambda x: len(x.split(";")))
        == df_substations["split_elements"]
    )

    df_substations.loc[bool_frequency_len & bool_split, "frequency"] = (
        df_substations.loc[bool_frequency_len & bool_split].apply(
            lambda row: row["frequency"].split(";")[row["split_count"] - 1], axis=1
        )
    )

    df_substations = _split_cells(df_substations, cols=["frequency"])
    bool_invalid_frequency = df_substations["frequency"].apply(
        lambda x: x not in ["50", "0"]
    )
    df_substations.loc[bool_invalid_frequency, "frequency"] = "50"
    return df_substations


def _clean_lines(df_lines: pd.DataFrame, list_voltages: Any) -> pd.DataFrame:
    """Clean lines/cables heuristically, deriving circuits from whatever tags exist."""
    df_lines = df_lines.copy()
    df_lines["cleaned"] = False
    df_lines["voltage_original"] = df_lines["voltage"]
    df_lines["circuits_original"] = df_lines["circuits"]

    df_lines = _split_cells(df_lines)
    bool_voltages = df_lines["voltage"].apply(
        _check_voltage, list_voltages=list_voltages
    )
    df_lines = df_lines[bool_voltages]

    bool_ac = df_lines["frequency"] != "0"
    bool_dc = ~bool_ac
    valid_frequency = ["50", "0"]
    bool_invalid_frequency = df_lines["frequency"].apply(
        lambda x: x not in valid_frequency
    )

    bool_noinfo = (df_lines["cables"] == "") & (df_lines["circuits"] == "")
    df_lines.loc[bool_noinfo, "circuits"] = "1"
    df_lines.loc[bool_noinfo & bool_invalid_frequency, "frequency"] = "50"
    df_lines.loc[bool_noinfo, "cleaned"] = True

    bool_cables_ac = (
        (df_lines["cables"] != "")
        & (df_lines["split_elements"] == 1)
        & (df_lines["cables"] != "0")
        & (df_lines["cables"].apply(lambda x: len(x.split(";")) == 1))
        & (df_lines["circuits"] == "")
        & (~df_lines["cleaned"])
        & bool_ac
    )
    df_lines.loc[bool_cables_ac, "circuits"] = df_lines.loc[
        bool_cables_ac, "cables"
    ].apply(lambda x: str(int(max(1, np.floor_divide(int(x), 3)))))
    df_lines.loc[bool_cables_ac, "frequency"] = "50"
    df_lines.loc[bool_cables_ac, "cleaned"] = True

    bool_cables_dc = (
        (df_lines["cables"] != "")
        & (df_lines["split_elements"] == 1)
        & (df_lines["cables"] != "0")
        & (df_lines["cables"].apply(lambda x: len(x.split(";")) == 1))
        & (df_lines["circuits"] == "")
        & (~df_lines["cleaned"])
        & bool_dc
    )
    df_lines.loc[bool_cables_dc, "circuits"] = df_lines.loc[
        bool_cables_dc, "cables"
    ].apply(lambda x: str(int(max(1, np.floor_divide(int(x), 2)))))
    df_lines.loc[bool_cables_dc, "frequency"] = "0"
    df_lines.loc[bool_cables_dc, "cleaned"] = True

    bool_lines = (
        (df_lines["circuits"] != "")
        & (df_lines["split_elements"] == 1)
        & (df_lines["circuits"] != "0")
        & (df_lines["circuits"].apply(lambda x: len(x.split(";")) == 1))
        & (~df_lines["cleaned"])
    )
    df_lines.loc[bool_lines & bool_ac, "frequency"] = "50"
    df_lines.loc[bool_lines & bool_dc, "frequency"] = "0"
    df_lines.loc[bool_lines, "cleaned"] = True

    bool_cables = (
        (df_lines["voltage_original"].apply(lambda x: len(x.split(";")) > 1))
        & (df_lines["cables"].apply(lambda x: len(x.split(";")) == 1))
        & (df_lines["circuits"].apply(lambda x: len(x.split(";")) == 1))
        & (~df_lines["cleaned"])
    )
    df_lines.loc[bool_cables, "circuits"] = df_lines[bool_cables].apply(
        _distribute_to_circuits, axis=1
    )
    df_lines.loc[bool_cables & bool_ac, "frequency"] = "50"
    df_lines.loc[bool_cables & bool_dc, "frequency"] = "0"
    df_lines.loc[bool_cables, "cleaned"] = True

    has_multiple_circuits = df_lines["circuits"].apply(lambda x: len(x.split(";")) > 1)
    circuits_match_split_elements = df_lines.apply(
        lambda row: len(row["circuits"].split(";")) == row["split_elements"], axis=1
    )
    is_not_cleaned = ~df_lines["cleaned"]
    bool_cables = has_multiple_circuits & circuits_match_split_elements & is_not_cleaned
    df_lines.loc[bool_cables, "circuits"] = df_lines.loc[bool_cables].apply(
        lambda row: str(row["circuits"].split(";")[int(row["id"].split("-")[-1]) - 1]),
        axis=1,
    )
    df_lines.loc[bool_cables & bool_ac, "frequency"] = "50"
    df_lines.loc[bool_cables & bool_dc, "frequency"] = "0"
    df_lines.loc[bool_cables, "cleaned"] = True

    has_multiple_cables = df_lines["cables"].apply(lambda x: len(x.split(";")) > 1)
    cables_match_split_elements = df_lines.apply(
        lambda row: len(row["cables"].split(";")) == row["split_elements"], axis=1
    )
    is_not_cleaned = ~df_lines["cleaned"]
    bool_cables = has_multiple_cables & cables_match_split_elements & is_not_cleaned
    df_lines.loc[bool_cables, "circuits"] = df_lines.loc[bool_cables].apply(
        lambda row: str(
            max(
                1,
                np.floor_divide(
                    int(row["cables"].split(";")[int(row["id"].split("-")[-1]) - 1]), 3
                ),
            )
        ),
        axis=1,
    )
    df_lines.loc[bool_cables & bool_ac, "frequency"] = "50"
    df_lines.loc[bool_cables & bool_dc, "frequency"] = "0"
    df_lines.loc[bool_cables, "cleaned"] = True

    bool_leftover = ~df_lines["cleaned"]
    df_lines.loc[bool_leftover, "circuits"] = "1"
    df_lines.loc[bool_leftover & bool_ac, "frequency"] = "50"
    df_lines.loc[bool_leftover & bool_dc, "frequency"] = "0"
    df_lines.loc[bool_leftover, "cleaned"] = True

    return df_lines


def _create_substations_geometry(df_substations: pd.DataFrame) -> pd.DataFrame:
    df_substations = df_substations.copy()
    df_substations["polygon"] = df_substations["geometry"]
    return df_substations


def _create_substations_poi(
    df_substations: pd.DataFrame, tol: float = BUS_TOL / 2
) -> pd.DataFrame:
    df_substations = df_substations.copy()
    df_substations["geometry"] = df_substations["polygon"].apply(
        lambda polygon: polylabel(polygon, tol)
    )
    return df_substations


def _aggregate_substations(df_substations: pd.DataFrame) -> pd.DataFrame:
    """One row per (original id, voltage, country), even after voltage-splitting."""
    df_substations = df_substations.copy()
    df_substations["id"] = df_substations["id"].apply(
        lambda x: x.split("-")[0] if "-" in x else x
    )
    df_substations = (
        df_substations.groupby(["id", "voltage", "country"])
        .agg(
            {
                col: "first"
                for col in df_substations.columns
                if col not in ["id", "voltage", "country"]
            }
        )
        .reset_index()
    )
    return df_substations


def _create_lines_geometry(df_lines: pd.DataFrame) -> pd.DataFrame:
    """Build LineString geometry and drop closed rings (area outlines, not lines)."""
    df_lines = df_lines.copy()
    df_lines["geometry"] = df_lines.apply(_create_linestring, axis=1)
    bool_circle = df_lines["geometry"].apply(lambda x: x.coords[0] == x.coords[-1])
    return df_lines[~bool_circle]


def _add_bus_poi_to_line(linestring: LineString, point: Point) -> LineString:
    start = linestring.coords[0]
    end = linestring.coords[-1]
    dist_to_start = point.distance(Point(start))
    dist_to_end = point.distance(Point(end))
    new_segment = LineString(
        [point.coords[0], start if dist_to_start < dist_to_end else end]
    )
    return linemerge([linestring, new_segment])


def _finalise_substations(df_substations: pd.DataFrame) -> gpd.GeoDataFrame:
    df_substations = df_substations.rename(columns={"id": "bus_id"})
    if df_substations.empty:
        df_substations["contains"] = pd.Series(dtype=object)
    else:
        df_substations["contains"] = df_substations["bus_id"].apply(
            lambda x: x.split("-")[0]
        )
    columns = [
        "bus_id",
        "voltage",
        "country",
        "under_construction",
        "start_date",
        "geometry",
        "polygon",
        "contains",
    ]
    df_substations = df_substations[columns]
    if not df_substations.empty:
        df_substations["voltage"] = df_substations["voltage"].astype(int)
    return df_substations


def _aggregate_lines(df_lines: pd.DataFrame) -> pd.DataFrame:
    """One row per (original line_id, voltage), summing circuits across splits."""
    df_lines = df_lines.copy()
    df_lines["line_id"] = df_lines["line_id"].apply(
        lambda x: x.split("-")[0] if "-" in x else x
    )
    df_lines = (
        df_lines.groupby(["line_id", "voltage"])
        .agg(
            {
                **{
                    col: "first"
                    for col in df_lines.columns
                    if col not in ["line_id", "voltage", "circuits"]
                },
                "circuits": "sum",
            }
        )
        .reset_index()
    )
    return df_lines[
        [
            "line_id",
            "circuits",
            "voltage",
            "underground",
            "under_construction",
            "start_date",
            "geometry",
            "contains",
        ]
    ]


def _finalise_lines(df_lines: pd.DataFrame) -> pd.DataFrame:
    df_lines = df_lines.rename(columns={"id": "line_id", "power": "tag_type"})
    df_lines["underground"] = df_lines["tag_type"] == "cable"
    df_lines["contains"] = df_lines["line_id"].apply(lambda x: [x.split("-")[0]])
    df_lines = df_lines[
        [
            "line_id",
            "circuits",
            "voltage",
            "underground",
            "under_construction",
            "start_date",
            "geometry",
            "contains",
        ]
    ]
    df_lines["circuits"] = df_lines["circuits"].astype(int)
    df_lines["voltage"] = df_lines["voltage"].astype(int)
    return df_lines


def _remove_lines_within_substations(
    gdf_lines: gpd.GeoDataFrame, gdf_substations_polygon: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    """Drop lines fully inside a substation polygon (busbars, jumpers, switchgear)."""
    if gdf_lines.empty or gdf_substations_polygon.empty:
        return gdf_lines
    contained = gpd.sjoin(
        gdf_lines[["line_id", "geometry"]],
        gdf_substations_polygon,
        how="inner",
        predicate="within",
    )["line_id"]
    logger.info(
        "Removed %d lines within substations of original %d lines.",
        len(contained),
        len(gdf_lines),
    )
    return gdf_lines[~gdf_lines["line_id"].isin(contained)]


def _merge_touching_polygons(df: pd.DataFrame, crs: str = GEO_CRS) -> gpd.GeoDataFrame:
    """Union adjacent/overlapping substation polygons before any bus is placed."""
    gdf = gpd.GeoDataFrame(df, geometry="polygon", crs=crs)
    invalid = gdf[~gdf.is_valid]
    if not invalid.empty:
        logger.warning("Found %d invalid geometries. Dropping them.", len(invalid))
        gdf = gdf[gdf.is_valid]

    combined_polygons = unary_union(gdf.geometry)
    if combined_polygons.geom_type == "MultiPolygon":
        gdf_combined = gpd.GeoDataFrame(
            geometry=[poly for poly in combined_polygons.geoms], crs=crs
        )
    else:
        gdf_combined = gpd.GeoDataFrame(geometry=[combined_polygons], crs=crs)

    gdf = gdf.reset_index(drop=True)
    for _, combined_geom in gdf_combined.iterrows():
        mask = gdf.intersects(combined_geom.geometry)
        gdf.loc[mask, "polygon_merged"] = combined_geom.geometry

    gdf = gdf.drop(columns=["polygon"]).rename(columns={"polygon_merged": "polygon"})
    return gdf


def _get_polygons_at_endpoints(
    linestring: LineString, polygon_dict: dict[str, Any]
) -> dict[str, Any]:
    start_point = Point(linestring.coords[0])
    end_point = Point(linestring.coords[-1])
    return {
        bus_id: polygon
        for bus_id, polygon in polygon_dict.items()
        if polygon.contains(start_point) or polygon.contains(end_point)
    }


def _add_endpoints_to_line(
    linestring: LineString, polygon_dict: dict[str, Any], tol: float = BUS_TOL / 2
) -> LineString:
    if not polygon_dict:
        return linestring
    polygon_pois = {
        bus_id: polylabel(polygon, tol) for bus_id, polygon in polygon_dict.items()
    }
    polygon_unary = unary_union(list(polygon_dict.values()))
    linestring_new = linestring.difference(polygon_unary)
    if isinstance(linestring_new, MultiLineString):
        if not linestring_new.geoms:
            return linestring
        linestring_new = max(linestring_new.geoms, key=lambda x: x.length)
    if linestring_new.is_empty:
        return linestring
    for bus_id in polygon_pois:
        linestring_new = _add_bus_poi_to_line(linestring_new, polygon_pois[bus_id])
    return linestring_new


def _extend_lines_to_substations(
    gdf_lines: gpd.GeoDataFrame,
    gdf_substations_polygon: gpd.GeoDataFrame,
    tol: float = BUS_TOL / 2,
) -> gpd.GeoDataFrame:
    """Snap a line's endpoint onto a touched substation's Pole of Inaccessibility."""
    if gdf_lines.empty or gdf_substations_polygon.empty:
        return gdf_lines

    gdf = gpd.sjoin(
        gdf_lines,
        gdf_substations_polygon.drop_duplicates(subset="polygon"),
        how="left",
        lsuffix="line",
        rsuffix="bus",
        predicate="intersects",
    ).drop(columns="index_bus")

    gdf = (
        gdf.groupby(["line_id", "voltage_line"])
        .apply(
            lambda x: (
                x[["bus_id", "geometry_bus"]]
                .dropna()
                .set_index("bus_id")["geometry_bus"]
                .to_dict()
            ),
            include_groups=False,
        )
        .reset_index()
    )
    gdf.columns = ["line_id", "voltage", "bus_dict"]
    gdf = gdf.set_index(["line_id", "voltage"])

    gdf["line_geometry"] = gdf.join(
        gdf_lines.set_index(["line_id", "voltage"])["geometry"]
    )["geometry"]
    gdf["bus_endpoints"] = gdf.apply(
        lambda row: _get_polygons_at_endpoints(row["line_geometry"], row["bus_dict"]),
        axis=1,
    )
    gdf["line_geometry_new"] = gdf.apply(
        lambda row: _add_endpoints_to_line(
            row["line_geometry"], row["bus_endpoints"], tol
        ),
        axis=1,
    )

    gdf_lines = gdf_lines.set_index(["line_id", "voltage"])
    gdf_lines["geometry"] = gdf["line_geometry_new"]
    return gdf_lines.reset_index()


def _check_if_ways_in_multi(members: list[str], longer_list: Any) -> bool:
    return any(member in longer_list for member in members)


def _create_line(row: pd.Series) -> tuple[Any, list[str]]:
    """Merge a relation's member ways into one line, dropping closed rings (substations)."""
    df = pd.json_normalize(row["members"])
    df["ref"] = df["ref"].astype(str)
    df["ways"] = "way/" + df["ref"]
    df = df.dropna(subset=["geometry"])
    df["geometry"] = df.apply(_create_linestring, axis=1)
    closed_geom = df["geometry"].apply(lambda x: x.is_closed)

    line = linemerge(df[~closed_geom]["geometry"].values.tolist())
    members = df[~closed_geom]["ways"].values.tolist()
    return line, members


# ---------------------------------------------------------------------------
# Adapters: earth-osm CSV exports and our Overpass relation JSON dumps ->
# the DataFrame shapes the ported functions above expect.
# ---------------------------------------------------------------------------


def _present(value: Any) -> bool:
    return (
        value is not None
        and not (isinstance(value, float) and np.isnan(value))
        and value != ""
    )


def _lonlat_to_geometry(value: Any) -> list[dict[str, float]]:
    """Convert earth-osm's ``[[lon, lat], ...]`` column to Overpass-style coord dicts."""
    if not _present(value):
        return []
    pairs = json.loads(value) if isinstance(value, str) else value
    return [
        {"lon": float(pair[0]), "lat": float(pair[1])}
        for pair in pairs
        if len(pair) >= 2
    ]


def _normalise_tag(value: Any) -> Any:
    """Undo pandas inferring a numeric-looking tag column (e.g. frequency) as float.

    OSM tags are strings (``frequency=0``, not ``0.0``); reference's cleaning
    functions compare against exact string literals like ``"0"`` and ``"50"``,
    so a whole-valued float must lose its ``.0`` or those comparisons silently
    fail and DC lines (frequency=0) get treated as AC.
    """
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return value


def _tag_value(row: dict[str, Any], other_tags: dict[str, str], key: str) -> Any:
    column = f"tags.{key}"
    if column in row and _present(row[column]):
        return _normalise_tag(row[column])
    return _normalise_tag(other_tags.get(key))


def _other_tags(row: pd.Series) -> dict[str, str]:
    value = row.get("other_tags")
    if not _present(value):
        return {}
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


_LINE_TAG_COLUMNS = [
    "power",
    "cables",
    "circuits",
    "frequency",
    "voltage",
    "wires",
    "construction",
    "construction:power",
    "start_date",
]
_SUBSTATION_TAG_COLUMNS = [
    "power",
    "substation",
    "voltage",
    "frequency",
    "construction",
    "construction:power",
    "start_date",
]


def _feature_of(path: str) -> str:
    """earth-osm names raw exports ``{country}_{feature}.csv``; recover the feature."""
    return Path(path).stem.split("_", maxsplit=1)[1]


def _import_lines_and_cables(paths: list[str]) -> pd.DataFrame:
    """Read earth-osm line/cable CSVs into the shape ``_clean_lines`` expects."""
    frames = []
    for path in paths:
        if _feature_of(path) not in ("line", "cable"):
            continue
        frame = pd.read_csv(path)
        if frame.empty:
            continue
        country = Path(path).stem.split("_", maxsplit=1)[0]
        other_tags = frame.apply(_other_tags, axis=1)
        record = pd.DataFrame({"id": "way/" + frame["id"].astype(int).astype(str)})
        record["country"] = country
        record["geometry"] = frame["lonlat"].apply(_lonlat_to_geometry)
        for column in _LINE_TAG_COLUMNS:
            record[column] = [
                _tag_value(row, tags, column)
                for row, tags in zip(frame.to_dict("records"), other_tags, strict=True)
            ]
            record[column] = record[column].where(record[column].notna(), None)
        record = record[record["geometry"].apply(len) >= 2]
        frames.append(record)
    if not frames:
        return pd.DataFrame(columns=["id", "country", "geometry", *_LINE_TAG_COLUMNS])
    return pd.concat(frames, ignore_index=True)


def _import_substations(paths: list[str]) -> pd.DataFrame:
    """Read earth-osm substation CSVs (nodes and ways) into ``_clean_substations`` shape."""
    frames = []
    for path in paths:
        if _feature_of(path) != "substation":
            continue
        frame = pd.read_csv(path)
        if frame.empty:
            continue
        country = Path(path).stem.split("_", maxsplit=1)[0]
        other_tags = frame.apply(_other_tags, axis=1)
        element_type = frame["Type"].map({"node": "node", "area": "way", "way": "way"})
        record = pd.DataFrame(
            {"id": element_type + "/" + frame["id"].astype(int).astype(str)}
        )
        record["country"] = country
        record["_is_node"] = frame["Type"] == "node"
        record["_coords"] = frame["lonlat"].apply(_lonlat_to_geometry)
        for column in _SUBSTATION_TAG_COLUMNS:
            record[column] = [
                _tag_value(row, tags, column)
                for row, tags in zip(frame.to_dict("records"), other_tags, strict=True)
            ]
            record[column] = record[column].where(record[column].notna(), None)
        record = record[record["_coords"].apply(len) >= 1]
        frames.append(record)
    if not frames:
        return pd.DataFrame(
            columns=["id", "country", "_is_node", "_coords", *_SUBSTATION_TAG_COLUMNS]
        )
    return pd.concat(frames, ignore_index=True)


def _import_routes_relation(paths: list[str]) -> pd.DataFrame:
    """Read our raw Overpass ``out body geom;`` relation dumps into ``_create_line`` shape."""
    columns = [
        "id",
        "members",
        "country",
        "power",
        "circuits",
        "cables",
        "frequency",
        "voltage",
        "construction",
        "construction:power",
        "start_date",
    ]
    frames = []
    for path in paths:
        if not Path(path).exists() or Path(path).stat().st_size < 50:
            continue
        with open(path) as handle:
            payload = json.load(handle)
        country = Path(path).stem.split("_", maxsplit=1)[0]
        elements = [
            e for e in payload.get("elements", []) if e.get("type") == "relation"
        ]
        if not elements:
            continue
        frame = pd.DataFrame(elements)
        frame["id"] = "relation/" + frame["id"].astype(str)
        frame["country"] = country
        tags = pd.json_normalize(frame["tags"]).map(
            lambda x: str(x) if pd.notnull(x) else x
        )
        for column in columns:
            if (
                column not in ("id", "members", "country")
                and column not in tags.columns
            ):
                tags[column] = pd.NA
        frame = pd.concat(
            [
                frame[["id", "members", "country"]],
                tags[[c for c in columns if c not in ("id", "members", "country")]],
            ],
            axis="columns",
        )
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _region_min_voltage(
    country: str, network: dict[str, Any], regions: dict[str, Any]
) -> float:
    override = regions.get(country, {}).get("minimum_voltage_kv")
    kv = override if override is not None else network["minimum_voltage_kv"]
    return float(kv) * 1000  # kV -> V


def clean_osm_data(
    paths: list[str],
    network: dict[str, Any],
    regions: dict[str, Any],
    relation_paths: list[str] | None = None,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Return clean substation buses, substation polygons, and AC lines/cables."""
    crs = GEO_CRS
    min_voltage_ac = network["minimum_voltage_kv"] * 1000  # V

    # --- Substations -------------------------------------------------
    logger.info("Importing substations.")
    df_substations = _import_substations(paths)
    if df_substations.empty:
        empty_buses = gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs=crs), crs=crs)
        empty_polygons = gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs=crs), crs=crs)
    else:
        df_substations["voltage"] = _clean_voltage(df_substations["voltage"])
        df_substations["under_construction"] = (
            (df_substations["construction"] == "substation")
            | (df_substations["construction:power"] == "substation")
            | (df_substations["power"] == "construction")
        )
        df_substations["start_date"] = _clean_date(df_substations["start_date"])

        df_substations, list_voltages = _filter_by_voltage(
            df_substations, min_voltage=min_voltage_ac
        )
        df_substations["frequency"] = _clean_frequency(df_substations["frequency"])
        df_substations = _clean_substations(df_substations, list_voltages)

        # Regional per-country minimum voltage override.
        row_min = df_substations["country"].map(
            lambda c: _region_min_voltage(c, network, regions)
        )
        df_substations = df_substations[
            df_substations["voltage"].astype(int) >= row_min
        ]

        # earth-osm also retrieves plain-node substations (no mapped extent),
        # which PyPSA-Eur's way/relation-only Overpass retrieval never has.
        # Nodes skip the polygon-only pipeline (touching-polygon merge, PoI,
        # line-snapping) and keep their point as-is; ways go through it
        # unchanged.
        df_way = df_substations[~df_substations["_is_node"]].copy()
        df_node = df_substations[df_substations["_is_node"]].copy()

        if not df_way.empty:
            df_way["geometry"] = df_way["_coords"].apply(
                lambda coords: _create_polygon({"geometry": coords})
            )
            df_way = df_way.drop(columns=["_is_node", "_coords"])
            df_way = _create_substations_geometry(df_way)
            df_way = _merge_touching_polygons(df_way, crs=crs)
            df_way = _create_substations_poi(df_way)

        if not df_node.empty:
            df_node["geometry"] = df_node["_coords"].apply(
                lambda coords: Point(coords[0]["lon"], coords[0]["lat"])
            )
            df_node = df_node.drop(columns=["_is_node", "_coords"])
            df_node["polygon"] = None

        df_substations = pd.concat([df_way, df_node], ignore_index=True)
        df_substations = _aggregate_substations(df_substations)
        df_substations = _finalise_substations(df_substations)

        empty_buses = gpd.GeoDataFrame(
            df_substations.drop(columns=["polygon"]), geometry="geometry", crs=crs
        )
        polygon_rows = df_substations[df_substations["polygon"].notna()]
        empty_polygons = gpd.GeoDataFrame(
            polygon_rows[["bus_id", "polygon", "voltage"]], geometry="polygon", crs=crs
        )
        empty_polygons["geometry"] = empty_polygons["polygon"]

    buses = empty_buses
    substation_polygons = empty_polygons

    # --- AC lines/cables via relations --------------------------------
    lines_frames = []
    ways_to_replace: set[str] = set()
    if relation_paths:
        logger.info("Importing power route relations.")
        df_relation = _import_routes_relation(relation_paths)
        if not df_relation.empty:
            df_relation = _drop_duplicate_lines(df_relation)
            df_relation["under_construction"] = (
                df_relation["construction"].isin(["line", "cable"])
                | df_relation["construction:power"].isin(["line", "cable"])
                | (df_relation["power"] == "construction")
            )
            df_relation["start_date"] = _clean_date(df_relation["start_date"])
            df_relation["voltage"] = _clean_voltage(df_relation["voltage"])
            df_relation, list_voltages = _filter_by_voltage(
                df_relation, min_voltage=min_voltage_ac
            )
            if not df_relation.empty:
                df_relation["frequency"] = _clean_frequency(df_relation["frequency"])
                df_relation = df_relation[df_relation["frequency"] != "0"]
                df_relation["frequency"] = "50"
                df_relation["circuits"] = _clean_circuits(df_relation["circuits"])
                df_relation["cables"] = _clean_cables(df_relation["cables"])
                df_relation = _clean_lines(df_relation, list_voltages)
                df_relation = df_relation.drop(
                    columns=[
                        "voltage_original",
                        "cleaned",
                        "circuits_original",
                        "split_elements",
                    ]
                )

                row_min = df_relation["country"].map(
                    lambda c: _region_min_voltage(c, network, regions)
                )
                df_relation = df_relation[df_relation["voltage"].astype(int) >= row_min]

            if not df_relation.empty:
                components = df_relation.apply(_create_line, axis=1)
                df_relation["geometry"] = components.apply(lambda x: x[0])
                df_relation["contains"] = components.apply(lambda x: x[1])

                multi = df_relation[
                    df_relation["geometry"].apply(
                        lambda x: x.geom_type == "MultiLineString"
                    )
                ]
                ways_in_multi = pd.Series(itertools.chain(*multi["contains"])).unique()
                df_relation["contains_ways_in_multi"] = df_relation["contains"].apply(
                    lambda x: _check_if_ways_in_multi(x, ways_in_multi)
                )
                df_relation = df_relation[~df_relation["contains_ways_in_multi"]]
                df_relation = df_relation[
                    df_relation["geometry"].apply(lambda x: x.geom_type == "LineString")
                ]

                ways_to_replace = set(itertools.chain(*df_relation["contains"]))
                df_relation = df_relation.rename(columns={"id": "line_id"})
                df_relation["circuits"] = df_relation["circuits"].astype(int)
                df_relation["voltage"] = df_relation["voltage"].astype(int)
                df_relation["underground"] = False
                lines_frames.append(
                    df_relation[
                        [
                            "line_id",
                            "circuits",
                            "voltage",
                            "underground",
                            "under_construction",
                            "start_date",
                            "geometry",
                            "contains",
                        ]
                    ]
                )

    # --- AC lines/cables via individual ways ---------------------------
    logger.info("Importing lines and cables.")
    df_lines = _import_lines_and_cables(paths)
    if not df_lines.empty:
        df_lines = _drop_duplicate_lines(df_lines)
        len_before = len(df_lines)
        df_lines = df_lines[~df_lines["id"].isin(ways_to_replace)]
        logger.info(
            "Dropping %d ways already covered by a relation.",
            len_before - len(df_lines),
        )

    if not df_lines.empty:
        df_lines["voltage"] = _clean_voltage(df_lines["voltage"])
        df_lines["under_construction"] = (
            df_lines["construction"].isin(["line", "cable"])
            | df_lines["construction:power"].isin(["line", "cable"])
            | (df_lines["power"] == "construction")
        )
        df_lines["start_date"] = _clean_date(df_lines["start_date"])
        df_lines, list_voltages = _filter_by_voltage(
            df_lines, min_voltage=min_voltage_ac
        )

    if not df_lines.empty:
        df_lines["circuits"] = _clean_circuits(df_lines["circuits"])
        df_lines["cables"] = _clean_cables(df_lines["cables"])
        df_lines["frequency"] = _clean_frequency(df_lines["frequency"])
        df_lines["wires"] = _clean_wires(df_lines["wires"])
        df_lines = _clean_lines(df_lines, list_voltages)

        len_before = len(df_lines)
        df_lines = df_lines[df_lines["frequency"] == "50"]
        logger.info(
            "Dropped %d DC lines. Keeping %d AC lines.",
            len_before - len(df_lines),
            len(df_lines),
        )

    if not df_lines.empty:
        row_min = df_lines["country"].map(
            lambda c: _region_min_voltage(c, network, regions)
        )
        df_lines = df_lines[df_lines["voltage"].astype(int) >= row_min]

    if not df_lines.empty:
        df_lines = _create_lines_geometry(df_lines)
        df_lines = _finalise_lines(df_lines)
        lines_frames.append(df_lines)

    if lines_frames:
        df_lines_all = pd.concat(lines_frames, axis=0, ignore_index=True)
        df_lines_all = _aggregate_lines(df_lines_all)
        clean_lines = gpd.GeoDataFrame(df_lines_all, geometry="geometry", crs=crs)
        clean_lines = _remove_lines_within_substations(clean_lines, substation_polygons)
        if not clean_lines.empty and not substation_polygons.empty:
            clean_lines = _extend_lines_to_substations(clean_lines, substation_polygons)
        clean_lines = gpd.GeoDataFrame(clean_lines, geometry="geometry", crs=crs)
    else:
        clean_lines = gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs=crs), crs=crs)

    if "polygon" in substation_polygons.columns:
        substation_polygons = substation_polygons.drop(columns=["geometry"])
        substation_polygons = substation_polygons.rename_geometry("geometry")

    return buses, substation_polygons, clean_lines


if __name__ == "__main__":
    configure_logging(snakemake.log[0])
    buses, polygons, lines = clean_osm_data(
        list(snakemake.input.raw),
        snakemake.params.network,
        snakemake.params.regions,
        list(snakemake.input.relations),
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
