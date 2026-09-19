# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT

"""Clean raw OSM power data into grid-builder's canonical schema.

Functions below are ported as close to verbatim as possible from their
source, per the grid-builder design plan:

- Most functions are copied from PyPSA-Earth's ``scripts/clean_osm_data.py``
  unchanged (same names, same bodies) — these are marked "Ported from Earth,
  verbatim" in their docstring.
- A few capabilities Earth lacks are copied from PyPSA-Eur's
  ``scripts/clean_osm_data.py`` instead (pole-of-inaccessibility substation
  geometry, wires/rating/date tag cleaning, cross-border line
  deduplication) — marked "Ported from Eur, verbatim".
- The one genuinely new piece is the orchestrator (``clean_data`` and its
  per-country helpers): grid-builder's ``retrieve_osm`` rule fetches OSM data
  per country (unlike Earth's single already-aggregated fetch), so the
  per-country cleaning logic below runs in a loop, then results are
  concatenated and cross-border-deduplicated — all inside the single
  ``clean_osm`` rule (no separate aggregation rule), matching Earth/Eur's
  convention that one rule runs one script.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import geopandas as gpd
import numpy as np
import pandas as pd
import reverse_geocode as rg
from shapely.algorithms.polylabel import polylabel
from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.ops import linemerge

if TYPE_CHECKING:
    snakemake: Any

logger = logging.getLogger(__name__)


# =============================================================================
# Ported from PyPSA-Earth's scripts/clean_osm_data.py, verbatim
# =============================================================================


def prepare_substation_df(df_all_buses):
    """
    Prepare raw substations dataframe to the structure compatible with PyPSA-
    Eur.

    Parameters
    ----------
    df_all_buses : dataframe
        Raw substations dataframe as downloaded from OpenStreetMap
    """
    # Modify the naming of the DataFrame columns to adapt to the PyPSA-Eur-like format
    df_all_buses = df_all_buses.rename(
        columns={
            "id": "bus_id",
            "tags.voltage": "voltage",
            # "dc", will be added below
            "tags.power": "symbol",
            # "under_construction", will be added below
            "tags.substation": "tag_substation",
            "Country": "country",
            "Area": "tag_area",
            "lonlat": "geometry",
        }
    )

    # Convert polygons to points (a no-op if geometry was already reduced to
    # a point upstream, e.g. by _create_substations_poi below)
    df_all_buses["geometry"] = df_all_buses["geometry"].centroid

    # Add longitude (lon) and latitude (lat) coordinates in the dataset
    df_all_buses["lon"] = df_all_buses["geometry"].x
    df_all_buses["lat"] = df_all_buses["geometry"].y

    # Initialize columns to default value
    df_all_buses["dc"] = False

    if "under_construction" in df_all_buses.columns:
        df_all_buses["under_construction"] = df_all_buses["under_construction"].fillna(
            False
        )
    else:
        df_all_buses["under_construction"] = False

    # Rearrange columns
    clist = [
        "bus_id",
        "station_id",
        "voltage",
        "dc",
        "symbol",
        "under_construction",
        "tag_substation",
        "tag_area",
        "lon",
        "lat",
        "geometry",
        "country",
    ]

    # Check. If column is not in df create an empty one.
    for c in clist:
        if c not in df_all_buses:
            df_all_buses[c] = np.nan

    df_all_buses.drop(
        df_all_buses.columns[~df_all_buses.columns.isin(clist)],
        axis=1,
        inplace=True,
        errors="ignore",
    )

    return df_all_buses


def add_line_endings_tosubstations(substations, lines):
    if lines.empty:
        return substations

    # extract columns from substation df
    bus_s = gpd.GeoDataFrame(columns=substations.columns, crs=substations.crs)
    bus_e = gpd.GeoDataFrame(columns=substations.columns, crs=substations.crs)

    # Read information from line.csv
    bus_s[["voltage", "country"]] = lines[["voltage", "country"]].astype(str)
    bus_s["geometry"] = lines.geometry.boundary.map(
        lambda p: p.geoms[0] if len(p.geoms) >= 2 else None
    )
    bus_s["lon"] = bus_s["geometry"].map(lambda p: p.x if p != None else None)
    bus_s["lat"] = bus_s["geometry"].map(lambda p: p.y if p != None else None)
    bus_s["bus_id"] = (
        (substations["bus_id"].max() if "bus_id" in substations else 0)
        + 1
        + bus_s.index
    )
    bus_s["dc"] = lines["dc"]

    bus_e[["voltage", "country"]] = lines[["voltage", "country"]].astype(str)
    bus_e["geometry"] = lines.geometry.boundary.map(
        lambda p: p.geoms[1] if len(p.geoms) >= 2 else None
    )
    bus_e["lon"] = bus_e["geometry"].map(lambda p: p.x if p != None else None)
    bus_e["lat"] = bus_e["geometry"].map(lambda p: p.y if p != None else None)
    bus_e["bus_id"] = bus_s["bus_id"].max() + 1 + bus_e.index
    bus_e["dc"] = lines["dc"]

    bus_all = pd.concat([bus_s, bus_e], ignore_index=True)

    # Initialize default values
    bus_all["station_id"] = np.nan
    # Assuming substations completed for installed lines
    bus_all["under_construction"] = False
    bus_all["tag_area"] = 0.0
    bus_all["symbol"] = "substation"
    # TODO: this tag may be improved, maybe depending on voltage levels
    bus_all["tag_substation"] = "transmission"

    buses = pd.concat([substations, bus_all], ignore_index=True)

    # Assign index to bus_id
    buses["bus_id"] = buses.index

    return buses


def set_unique_id(df, col):
    """
    Create unique id's, where id is specified by the column "col" The steps
    below create unique bus id's without losing the original OSM bus_id.

    Unique bus_id are created by simply adding -1,-2,-3 to the original bus_id
    Every unique id gets a -1
    If a bus_id exist i.e. three times it it will the counted by cumcount -1,-2,-3 making the id unique

    Parameters
    ----------
    df : dataframe
        Dataframe considered for the analysis
    col : str
        Column name for the analyses; examples: "bus_id" for substations or "line_id" for lines
    """
    # operate only if id is not already unique (nunique counts unique values)
    if df[col].count() != df[col].nunique():
        # create cumcount column. Cumcount counts 0,1,2,3 the number of duplicates
        df["cumcount"] = df.groupby([col]).cumcount()
        # avoid 0 value for better understanding
        df["cumcount"] = df["cumcount"] + 1
        # add cumcount to id to make id unique
        df[col] = df[col].astype(str) + "-" + df["cumcount"].values.astype(str)
        # remove cumcount column
        df.drop(columns="cumcount", inplace=True)

    return df


def split_cells(df, cols=["voltage"]):
    """
    Split semicolon separated cells i.e. [66000;220000] and create new
    identical rows.

    Parameters
    ----------
    df : dataframe
        Dataframe under analysis
    cols : list
        List of target columns over which to perform the analysis

    Example
    -------
    Original data:
    row 1: '66000;220000', '50'

    After applying split_cells():
    row 1, '66000', '50'
    row 2, '220000', '50'
    """
    if df.empty:
        return df

    x = df.assign(**{col: df[col].str.split(";") for col in cols})

    return x.explode(cols, ignore_index=True)


def filter_voltage(df, threshold_voltage=35000):
    """
    Filters df to contain only lines with voltage above threshold_voltage.
    """
    # Convert to numeric and drop any row with N/A voltage
    df["voltage"] = pd.to_numeric(df["voltage"], errors="coerce").astype(float)
    df.dropna(subset=["voltage"], inplace=True)

    # convert voltage to int
    df["voltage"] = df["voltage"].astype(int)

    # drop lines with a voltage lower than than threshold_voltage
    df.drop(
        df[df.voltage < threshold_voltage].index,
        axis=0,
        inplace=True,
        errors="ignore",
    )

    return df


def filter_frequency(df, accepted_values=[50, 60, 0], threshold=0.1):
    """
    Filters df to contain only lines with frequency with accepted_values.
    """
    df["tag_frequency"] = pd.to_numeric(df["tag_frequency"], errors="coerce").astype(
        float
    )
    df.dropna(subset=["tag_frequency"], inplace=True)

    accepted_rows = pd.concat(
        [(df["tag_frequency"] - f_val).abs() <= threshold for f_val in accepted_values],
        axis=1,
    ).any(axis="columns")

    df.drop(df[~accepted_rows].index, inplace=True)

    df["dc"] = df["tag_frequency"].abs() <= threshold

    return df


def filter_circuits(df, min_value_circuit=0.1):
    """
    Filters df to contain only lines with circuit value above
    min_value_circuit.
    """
    df["circuits"] = pd.to_numeric(df["circuits"], errors="coerce").astype(float)
    df.dropna(subset=["circuits"], inplace=True)

    accepted_rows = df["circuits"] >= min_value_circuit

    df.drop(df[~accepted_rows].index, inplace=True)

    return df


def finalize_substation_types(df_all_buses):
    """
    Specify bus_id and voltage columns as integer.
    """
    df_all_buses["bus_id"] = df_all_buses["bus_id"].astype(int)
    df_all_buses["voltage"] = df_all_buses["voltage"].astype(int)

    return df_all_buses


def prepare_lines_df(df_lines):
    """
    This function prepares the dataframe for lines and cables.

    Parameters
    ----------
    df_lines : dataframe
        Raw lines or cables dataframe as downloaded from OpenStreetMap
    """
    # Modification - create final dataframe layout
    df_lines = df_lines.rename(
        columns={
            "id": "line_id",
            "tags.voltage": "voltage",
            "tags.circuits": "circuits",
            "tags.cables": "cables",
            "tags.frequency": "tag_frequency",
            "tags.power": "tag_type",
            "lonlat": "geometry",
            "Country": "country",
            "Length": "length",
        }
    )

    # Rearrange columns
    clist = [
        "line_id",
        "bus0",
        "bus1",
        "voltage",
        "circuits",
        "length",
        "underground",
        "under_construction",
        "tag_type",
        "tag_frequency",
        "dc",
        "cables",
        "geometry",
        "country",
    ]

    # Check. If column is not in df create an empty one.
    for c in clist:
        if c not in df_lines:
            df_lines[c] = np.nan

    df_lines.drop(
        df_lines.columns[~df_lines.columns.isin(clist)],
        axis=1,
        inplace=True,
        errors="ignore",
    )

    return df_lines


def finalize_lines_type(df_lines):
    """
    This function is aimed at finalizing the type of the columns of the
    dataframe.
    """
    df_lines["line_id"] = df_lines["line_id"].astype(int)

    return df_lines


def clean_frequency(df, default_frequency="50"):
    """
    Function to clean raw frequency column: manual fixing and fill nan values
    """
    # replace raw values
    repl_freq = {
        "16.67": "16.7",
        "50;50;16.716.7": "50;50;16.7;16.7",
        "50;16.7?": "50;16.7",
        "50.0": "50",
        "60.0": "60",
        # "24 kHz": "24000",
    }

    # TODO: default frequency may be by country
    df["tag_frequency"] = (
        df["tag_frequency"].fillna(default_frequency).astype(str).replace(repl_freq)
    )

    return df


def clean_voltage(df):
    """
    Function to clean the raw voltage column: manual fixing and drop nan values
    """
    # replace raw values
    repl_voltage = {
        "medium": "33000",
        "19.1 kV": "19100",
        "high": "220000",
        "240 VAC": "240",
        "2*220000": "220000;220000",
        "KV30": "30kV",
    }

    df.dropna(subset=["voltage"], inplace=True)

    df["voltage"] = (
        df["voltage"]
        .astype(str)
        .replace(repl_voltage)
        .str.lower()
        .str.replace(" ", "")
        .str.replace("_", "")
        .str.replace("kv", "000")
        .str.replace("v", "")
        # .str.replace("/", ";")  # few OSM entries are separated by / instead of ;
        # this line can be a fix for that if relevant
    )

    return df


def clean_circuits(df):
    """
    Function to clean the raw circuits column: manual fixing and clean nan values
    """
    # replace raw values
    repl_circuits = {
        "1/3": "1",
        "2/3": "2",
        # assumption of two lines and one grounding wire
        "2-1": "2",
        "single": "1",
        "partial": "1",
        "1;1 disused": "1;0",
        # assuming that in case of a typo there is at least one line
        "`": "1",
        "^1": "1",
        "e": "1",
        "d": "1",
        "1.": "1",
    }

    # note: no string conversion for all entries in clean_circuits! it is performed later on
    df["circuits"] = (
        df["circuits"]
        .replace(repl_circuits)
        .map(lambda x: x.replace(" ", "") if isinstance(x, str) else x)
    )

    # Convert numbers in different dtypes to string while preserving NaN or other strings.
    is_numeric = ~pd.to_numeric(df["circuits"], errors="coerce").isna()
    df["circuits"] = df["circuits"].mask(is_numeric, df["circuits"].astype(str))

    # Report non-numeric and non-NaN values, which should be added to repl_circuits.
    if df.loc[~is_numeric, "circuits"].notna().any():
        logger.warning(
            "Non-numeric and non-NaN values found in circuits column, consider replacement: "
            + str(df.loc[~is_numeric, "circuits"].unique())
        )

    return df


def clean_cables(df):
    """
    Function to clean the raw cables column: manual fixing and drop undesired values
    """
    # replace raw values
    repl_cables = {
        "1 disused": "0",
        "ground": "0",
        "single": "1",
        "triple": "3",
        "3;3 disused": "3;0",
        "1 (Looped - Haul & Return) + 1 power wire": "1",
        "2-1": "3",
        "3+3": "6",
        "6+1": "6",
        "2x3": "6",
        "3x2": "6",
        "2x2": "4",
        # assuming that in case of a typo there is at least one line
        "partial": "1",
        "`": "1",
        "^1": "1",
        "e": "1",
        "d": "1",
        "line": "1",
    }

    df["cables"] = (
        df["cables"]
        .replace(repl_cables)
        .map(lambda x: x.replace(" ", "") if isinstance(x, str) else x)
    )

    # Convert numbers in different dtypes to string while preserving NaN or other strings.
    is_numeric = ~pd.to_numeric(df["cables"], errors="coerce").isna()
    df["cables"] = df["cables"].mask(is_numeric, df["cables"].astype(str))

    # Report non-numeric and non-NaN values, which should be added to repl_cables.
    if df.loc[~is_numeric, "cables"].notna().any():
        logger.warning(
            "Non-numeric and non-NaN values found in cables column, consider replacement: "
            + str(df.loc[~is_numeric, "cables"].unique())
        )

    return df


def split_and_match_voltage_frequency_size(df):
    """
    Function to match the length of the columns in subset by duplicating the
    last value in the column.

    The function does as follows:

    1. First, it splits voltage and frequency columns by semicolon
       For example, the following lines
       row 1: '50', '220000
       row 2: '50;50;50', '220000;380000'

       become:
       row 1: ['50'], ['220000']
       row 2: ['50','50','50'], ['220000','380000']

    2. Then, it harmonize each row to match the length of the lists
       by filling the missing values with the last elements of each list.
       In agreement to the example of before, after the cleaning:

       row 1: ['50'], ['220000']
       row 2: ['50','50','50'], ['220000','380000','380000']
    """
    for col in ["tag_frequency", "voltage"]:
        df[col] = df[col].str.split(";")

    len_freq = df["tag_frequency"].map(len)
    len_voltage = df["voltage"].map(len)

    def _fill_by_last(row, col_to_fill, size_col):
        """
        This functions takes a series and checks two elements in locations
        col_to_fill and size_col that are lists.

        The list of col_to_fill has less elements than of size_col. This
        function extends the col_to_fill element to match the size of
        size_col by replicating the last element as necessary.
        """
        size_to_fill = len(row[size_col])
        if not row[col_to_fill]:
            return None
        n_missing = size_to_fill - len(row[col_to_fill])
        fill_val = row[col_to_fill][-1]

        return row[col_to_fill] + [fill_val] * n_missing

    df_fhv = df[len_freq > len_voltage].apply(
        lambda row: _fill_by_last(row, "voltage", "tag_frequency"),
        axis=1,
    )
    df.loc[df_fhv.index, "voltage"] = df_fhv

    df_vhf = df[len_freq < len_voltage].apply(
        lambda row: _fill_by_last(row, "tag_frequency", "voltage"),
        axis=1,
    )
    df.loc[df_vhf.index, "tag_frequency"] = df_vhf

    return df


def fill_circuits(df):
    """
    This function fills the rows circuits column so that the size of each list
    element matches the size of the list in the frequency column.

    Multiple procedure are adopted:

    1. In the rows of circuits where the number of elements matches
       the number of the frequency column, nothing is done
    2. Where the number of elements in the cables column match the ones
       in the frequency column, then the values of cables are used.
    3. Where the number of elements in cables exceed those in frequency,
       the cables elements are downscaled and the last values of cables
       are summed.
       Let's assume that cables is [3,3,3] but frequency is [50,50].
       With this procedure, cables is treated as [3,6] and used for
       calculating the circuits
    4. Where the number in cables has an unique number, e.g. ['6'],
       but frequency does not, e.g. ['50', '50'],
       then distribute the cables proportionally across the values.
       Note: the distribution accounts for the frequency type;
       when the frequency is 50 or 60, then a circuit requires 3 cables,
       when DC (0 frequency) is used, a circuit requires 2 cables.
    5. Where no information of cables or circuits is available,
       a circuit is assumed for every frequency entry.
    """

    def _get_circuits_status(df):
        len_f = df["tag_frequency"].map(len)
        len_c = df["circuits"].map(
            lambda x: x.count(";") + 1 if isinstance(x, str) else np.nan
        )
        isna_c = df["circuits"].isna()
        len_cab = df["cables"].map(
            lambda x: x.count(";") + 1 if isinstance(x, str) else np.nan
        )
        isna_cab = df["cables"].isna()
        return len_f, len_c, isna_c, len_cab, isna_cab

    def _parse_float(x, ret_def=0.0):
        try:
            return float(x)
        except:
            return ret_def

    # cables requirement for circuits calculation
    cables_req = {"50": 3, "60": 3, "16.7": 2, "0": 2}

    def _basic_cables(f_val, cables_req=cables_req, def_circ=3):
        return cables_req[f_val] if f_val in cables_req.keys() else def_circ

    len_f, len_c, isna_c, len_cab, isna_cab = _get_circuits_status(df)

    is_numeric_cables = ~pd.to_numeric(df["cables"], errors="coerce").isna()

    to_fill = isna_c | (len_f != len_c)
    to_fill_direct = to_fill & (len_cab == len_f)
    to_fill_merge = to_fill & (len_cab > len_f)
    to_fill_indirect = to_fill & ~to_fill_direct & is_numeric_cables
    to_fill_default = to_fill & ~to_fill_merge & ~to_fill_direct & ~to_fill_indirect

    # length of cables match the frequency one
    # matching uses directly only the cables series
    df_match_by_cables = df[to_fill_direct][["tag_frequency", "cables"]].copy()
    df_match_by_cables["cables"] = (
        df_match_by_cables["cables"].astype(str).str.split(";")
    )

    def _filter_cables(row):
        return ";".join(
            [
                str(_parse_float(vc) / _basic_cables(vf))
                for (vc, vf) in zip(row["cables"], row["tag_frequency"])
            ]
        )

    df.loc[df_match_by_cables.index, "circuits"] = df_match_by_cables.apply(
        _filter_cables, axis=1
    )

    # length of cables elements is larger than frequency; the last cable data are merged to match
    df_merge_by_cables = df[to_fill_merge][["tag_frequency", "cables"]].copy()
    df_merge_by_cables["cables"] = (
        df_merge_by_cables["cables"].astype(str).str.split(";")
    )

    def _parse_cables_to_len(row):
        lf = len(row["tag_frequency"])
        float_cable = [_parse_float(vc) for vc in row["cables"]]
        parsed_cable_list = float_cable[0 : lf - 1] + [sum(float_cable[lf - 1 :])]

        return ";".join(
            [
                str(vc / _basic_cables(vf))
                for (vc, vf) in zip(parsed_cable_list, row["tag_frequency"])
            ]
        )

    df.loc[df_merge_by_cables.index, "circuits"] = df_merge_by_cables.apply(
        _parse_cables_to_len, axis=1
    )

    # indirect matching exploiting the total numeric value in cables
    # the minimum requirement of cables by frequency value is calculated
    # then using the total numeric cables number, the values are scaled proportionally
    df_indirect = df[to_fill_indirect]

    basic_cables = (
        df_indirect["tag_frequency"]
        .map(lambda x: [_basic_cables(v) for v in x])
        .rename("basic_cables")
    )
    min_cables = basic_cables.map(sum).rename("min_cables")
    multiplier = (
        pd.to_numeric(df_indirect["cables"], errors="coerce") / min_cables
    ).rename("multiplier")
    filled_values = pd.concat([basic_cables, multiplier], axis=1).apply(
        lambda x: ";".join([str(x["multiplier"] * v) for v in x["basic_cables"]]),
        axis=1,
    )
    df["circuits"] = df["circuits"].astype(str)
    df.loc[filled_values.index, "circuits"] = filled_values

    # otherwise assume a circuit per element
    df_fill_default = df[to_fill_default]
    df.loc[df_fill_default.index, "circuits"] = len_f.loc[df_fill_default.index].map(
        lambda x: ";".join(["1"] * x)
    )

    # explode column
    df["circuits"] = df["circuits"].astype(str).str.split(";")

    return df


def explode_rows(df, cols):
    """
    Function that explodes the rows as specified in cols, including warning
    alerts for unexpected values.

    Example
    --------
    row 1: [50,50], [33000, 110000]

    after explode_rows applied on the two columns becomes
    row 1: 50, 33000
    row 2: 50, 110000
    """
    # check if all row elements are list
    is_all_list = df[cols].map(lambda x: isinstance(x, list)).all(axis=1)
    if not is_all_list.all():
        df_nonlist = df[~is_all_list]
        logger.warning(
            f"Unexpected non-list values in dataframe; dropping rows. Needed fix for entries:\n{df_nonlist}"
        )
        df.drop(df_nonlist.index, inplace=True)

    # check if errors in the columns
    nunique_values = df[cols].map(len).nunique(axis=1)
    df_nunique = df[nunique_values != 1]
    if not df_nunique.empty:
        logger.warning(
            f"Improper explosion of dataframe entries; dropping rows. Needed fix for entries:\n{df_nunique}"
        )
        df.drop(df_nunique.index, inplace=True)

    df = df.explode(cols, ignore_index=True)

    return df


def integrate_lines_df(df_all_lines, distance_crs):
    """
    Function to add underground, under_construction, frequency and circuits.
    """
    # explode frequency and columns
    df = pd.DataFrame(df_all_lines)

    # preliminary raw parsing of raw columns
    clean_frequency(df)
    clean_voltage(df)
    clean_circuits(df)
    clean_cables(df)

    # analyse each row of voltage and frequency and match their content
    split_and_match_voltage_frequency_size(df)

    # fill the circuits column for explode
    fill_circuits(df)

    # Add under construction info
    if "under_construction" in df.columns:
        df["under_construction"] = df["under_construction"].fillna(False)
    else:
        df["under_construction"] = False

    # Add underground flag to check whether the line (cable) is underground
    # Simplified. If tag_type cable then underground is True
    df["underground"] = df["tag_type"] == "cable"

    # drop columns
    df.drop(columns=["tag_location", "cables"], errors="ignore", inplace=True)

    # explode rows
    df = explode_rows(df, ["tag_frequency", "voltage", "circuits"])

    return gpd.GeoDataFrame(df, crs=df_all_lines.crs)


def filter_lines_by_geometry(df_all_lines):
    if df_all_lines.empty:
        return df_all_lines
    # drop None geometries
    df_all_lines.dropna(subset=["geometry"], axis=0, inplace=True)

    idx_mls = df_all_lines.geometry.geom_type == "MultiLineString"
    for idx, row in df_all_lines[idx_mls].iterrows():
        df_all_lines.loc[idx, "geometry"] = linemerge(row.geometry)

    df_drop = df_all_lines[df_all_lines.geometry.geom_type != "LineString"]
    if not df_drop.empty:
        # remove lines represented as Polygons or multilinestrings
        logger.warning(
            f"Dropping {len(df_drop)} lines with unexpected geometry types:\n{df_drop} "
        )
        df_all_lines.drop(df_drop.index, axis=0, inplace=True)

    return df_all_lines


def prepare_generators_df(df_all_generators):
    """
    Prepare the dataframe for generators.
    """
    # reset index
    df_all_generators = df_all_generators.reset_index(drop=True)

    check_fields_for_generators = ["tags.generator:output:electricity"]
    for field_to_add in check_fields_for_generators:
        if field_to_add not in df_all_generators.columns.tolist():
            df_all_generators[field_to_add] = ""

    df_all_generators = df_all_generators.rename(
        columns={
            "tags.generator:output:electricity": "power_output_MW",
            "tags.name": "name",
        }
    )

    # convert electricity column from string to float value
    # TODO: this filtering can be improved
    df_all_generators = df_all_generators[
        df_all_generators["power_output_MW"].astype(str).str.contains("MW")
    ]
    df_all_generators["power_output_MW"] = (
        df_all_generators["power_output_MW"]
        .astype(str)
        .str.extract(r"(\d+)")
        .astype(float)
    )

    return df_all_generators


def find_first_overlap(geom, country_geoms, default_name):
    """
    Return the first index whose shape intersects the geometry.
    """
    for c_name, c_geom in country_geoms.items():
        if not geom.disjoint(c_geom):
            return c_name
    return default_name


def set_countryname_by_shape(
    df,
    ext_country_shapes,
    exclude_external=True,
    col_country="country",
):
    "Set the country name by the name shape"
    df[col_country] = [
        find_first_overlap(
            row["geometry"],
            ext_country_shapes,
            None if exclude_external else row[col_country],
        )
        for id, row in df.iterrows()
    ]
    df.dropna(subset=[col_country], inplace=True)
    return df


def create_extended_country_shapes(country_shapes, offshore_shapes, tolerance=0.01):
    """
    Obtain the extended country shape by merging on- and off-shore shapes.
    """

    merged_shapes = (
        gpd.GeoDataFrame(
            {
                "name": list(country_shapes.index),
                "geometry": [
                    (
                        c_geom.unary_union(offshore_shapes[c_code])
                        if c_code in offshore_shapes
                        else c_geom
                    )
                    for c_code, c_geom in country_shapes.items()
                ],
            },
            crs=country_shapes.crs,
        )
        .set_index("name")["geometry"]
        .buffer(tolerance)
    )

    return merged_shapes


def set_name_by_closestcity(df_all_generators, colname="name"):
    """
    Function to set the name column equal to the name of the closest city.
    """

    # get cities name
    list_cities = rg.search([g.coords[0] for g in df_all_generators.geometry])

    # replace name
    df_all_generators[colname] = [
        l["city"] + "_" + str(id) + " - " + c_code
        for (l, c_code, id) in zip(
            list_cities, df_all_generators.country, df_all_generators.index
        )
    ]

    return df_all_generators


def load_network_data(network_asset, input_files, data_options):
    """
    Function to check if OSM or custom data should be considered.

    The network_asset should be a string named "lines", "cables" or
    "substations".
    """

    # checks the options for loading data to be used based on the network_asset defined (lines/cables/substations)
    try:
        cleaning_data_options = data_options[f"use_custom_{network_asset}"]
        custom_path = data_options[f"path_custom_{network_asset}"]

    except:
        logger.error(
            f"Missing use_custom_{network_asset} or path_custom_{network_asset} options in the config file"
        )

    # creates a dataframe for the network_asset defined
    if cleaning_data_options == "custom_only":
        loaded_df = gpd.read_file(custom_path)

    elif cleaning_data_options == "add_custom":
        loaded_df1 = gpd.read_file(input_files[network_asset])
        loaded_df2 = gpd.read_file(custom_path)
        loaded_df = pd.concat([loaded_df1, loaded_df2], ignore_index=True)

    elif cleaning_data_options == "none":
        loaded_df1 = gpd.read_file(input_files[network_asset])
        loaded_df = gpd.GeoDataFrame(columns=loaded_df1.columns, crs=loaded_df1.crs)

    else:
        if cleaning_data_options != "OSM_only":
            logger.warning(
                f"Unrecognized option {data_options} for handling custom data of {network_asset}."
                + "Default OSM_only option used. Options available in clean_OSM_data_options configtable"
            )

        loaded_df = gpd.read_file(input_files[network_asset])

    # returns dataframe to be read in each section of the code depending on the component type (lines, substations or cables)
    return loaded_df


# =============================================================================
# Ported from PyPSA-Eur's scripts/clean_osm_data.py, verbatim
# =============================================================================


def _create_substations_poi(df_substations, tol):
    """
    Creates Pole of Inaccessibility (PoI) from geometries and keeps the original polygons.

    Parameters
    ----------
    df_substations (DataFrame): The input DataFrame containing the substations
    data.

    Returns
    -------
    df_substations (DataFrame): A new DataFrame with the PoI ["geometry"]
    and polygons ["polygon"] of the substations geometries.
    """
    df_substations = df_substations.copy()

    df_substations.loc[:, "polygon"] = df_substations["geometry"]

    def _poi(geom):
        if isinstance(geom, Point):
            return geom
        if isinstance(geom, MultiPolygon):
            geom = max(geom.geoms, key=lambda p: p.area)
        if isinstance(geom, Polygon):
            try:
                return polylabel(geom, tolerance=tol)
            except Exception:
                return geom.centroid
        return geom.centroid

    df_substations.loc[:, "geometry"] = df_substations["polygon"].apply(_poi)

    return df_substations


def _clean_wires(column):
    """
    Function to clean the raw wires column: manual fixing and drop nan values

    Args:
    - column: pandas Series, the column to be cleaned

    Returns:
    - column: pandas Series, the cleaned column
    """
    column = column.copy()
    column = (
        column.astype(str)
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
        .str.replace("<na>", "")
        .str.replace("nan", "")
    )

    return column


def _clean_rating(column):
    """
    Function to clean and sum the rating columns:

    Args:
    - column: pandas Series, the column to be cleaned

    Returns:
    - column: pandas Series, the cleaned column
    """
    column = column.copy()
    column = column.astype(str).str.replace("MW", "")

    return column


def _clean_date(column):
    """
    Function to clean the raw date column: manual fixing and drop nan values
    Args:
    - column: pandas Series, the column to be cleaned
    Returns:
    - column: pandas Series of datetime64, the cleaned column (with NaT for invalid dates)
    """
    column = column.copy()

    # Replace NaN/None with empty string first
    column = column.fillna("")
    column = column.replace({pd.NA: "", None: ""})

    # Clean text indicators of uncertainty
    column = (
        column.astype(str)
        .str.lower()
        .str.replace("unknown", "", regex=False)
        .str.replace("approx", "", regex=False)
        .str.replace("c.", "", regex=False)
        .str.replace("circa", "", regex=False)
        .str.replace("about", "", regex=False)
        .str.replace("?", "", regex=False)
        .str.replace("<na>", "", regex=False)
        .str.replace("nan", "", regex=False)
        .str.replace("none", "", regex=False)
        .str.strip()
    )

    # Replace empty strings with NaN before datetime conversion
    column = column.replace("", np.nan)

    # Convert to datetime (keeps NaT for invalid/missing dates)
    column = pd.to_datetime(column, errors="coerce", format="mixed")

    return column


def _treat_under_construction(df, decision, remove_after):  # decision is "keep" or "remove"
    """
    Keep or remove elements that are under construction based on the provided boolean flag.

    Parameters
    ----------
    df (pandas.DataFrame): The input DataFrame containing the data.
    decision (str): A string indicating whether to "keep" or "remove" elements under construction.
    remove_after (str): A date-time string in 'YYYY-MM-DD' format. Elements under construction with a date after this will be removed.

    Returns
    -------
    pandas.DataFrame: The DataFrame with under construction elements removed if the flag is True.
    """
    if decision == "keep":
        logger.info("Keeping elements under construction.")

    elif decision == "remove":
        logger.info("Removing elements under construction...")
        len_before = len(df)
        idx_remove = df.index[df["under_construction"]]
        df = df.drop(index=idx_remove)
        len_after = len(df)
        logger.info(f"Removed {len_before - len_after} elements under construction.")

    if remove_after is not None:
        logger.info(f"Removing elements with a start date after {remove_after}...")
        df["start_date"] = pd.to_datetime(df["start_date"], errors="coerce")
        len_before = len(df)
        idx_remove = df.index[df["start_date"] > pd.to_datetime(remove_after)]
        df = df.drop(index=idx_remove)
        len_after = len(df)
        logger.info(
            f"Removed {len_before - len_after} elements with start date after {remove_after}."
        )

    return df


def _drop_duplicate_lines(df_lines, id_col="line_id"):
    """
    Drop duplicate lines from the given dataframe. Duplicates are usually lines
    cross-border lines or slightly outside the country border of focus.

    Parameters
    ----------
    - df_lines (pandas.DataFrame): The dataframe containing lines data.
    - id_col (str): id column to dedupe on (generalized from Eur's hardcoded
      "id" so this also works for grid-builder's "line_id"/"dc_link_id").

    Returns
    -------
    - df_lines (pandas.DataFrame): The dataframe with duplicate lines removed
      and cleaned data.

    This function drops duplicate lines from the given dataframe based on the
    id column. It groups the duplicate rows by id and aggregates the
    'country' column to a string split by semicolon, as they appear in multiple
    country datasets. One example of the duplicates is kept, accordingly.
    Finally, the updated dataframe without multiple duplicates is returned.
    """
    duplicate_rows = df_lines[df_lines.duplicated(subset=[id_col], keep=False)].copy()
    if duplicate_rows.empty:
        return df_lines

    # Group rows by id and aggregate the country column to a string split by semicolon
    grouped_duplicates = (
        duplicate_rows.groupby(id_col)["country"]
        .agg(lambda x: ";".join(x))
        .reset_index()
    )
    duplicate_rows.drop_duplicates(subset=id_col, inplace=True)
    duplicate_rows.drop(columns=["country"], inplace=True)
    duplicate_rows = duplicate_rows.join(
        grouped_duplicates.set_index(id_col), on=id_col, how="left"
    )

    len_before = len(df_lines)
    # Drop duplicates and update the df_lines dataframe with the cleaned data
    df_lines = df_lines[~df_lines[id_col].isin(duplicate_rows[id_col])]
    df_lines = pd.concat([df_lines, duplicate_rows], axis="rows")
    len_after = len(df_lines)

    logger.info(
        f"Dropped {len_before - len_after} duplicate elements. "
        + f"Keeping {len_after} elements."
    )

    return df_lines


# =============================================================================
# Grid-builder-specific: DC/converter detection (a merge of Earth's `dc` flag
# and Eur's converter-tag detection, since neither original has this exact
# function — see the design plan)
# =============================================================================


def detect_dc_and_converters(buses, lines):
    """
    Flag converter-candidate substations from the ``tag_substation`` tag
    (Eur's approach: substation tagged "converter" or "switching"), on top of
    the ``dc`` flag ``filter_frequency`` already set on lines (Earth's
    approach). Returns the buses frame unchanged plus a subset of
    converter-candidate buses that build_network.add_converters can use.
    """
    tag = buses["tag_substation"].astype(str).str.lower()
    is_converter_candidate = tag.str.contains("converter", na=False) | tag.str.contains(
        "switching", na=False
    )
    return buses, buses[is_converter_candidate].copy()


# =============================================================================
# Orchestrator: grid-builder-specific glue. retrieve_osm fetches OSM data per
# country (unlike Earth's single already-aggregated fetch), so the
# per-country cleaning below runs in a loop over Earth's clean_data logic,
# then cross-border deduplication (Eur's _drop_duplicate_lines) runs once
# across the concatenated result.
# =============================================================================


def _empty(path: "str | Path | None") -> bool:
    return path is None or Path(path).stat().st_size == 0


def _stringify_dates(df):
    """Cast the datetime ``start_date`` column to plain strings before
    writing GeoJSON — pyogrio's datetime-to-GeoJSON path can raise on an
    all-NaT column (a serialization quirk unrelated to _treat_under_
    construction's own logic, which stays untouched)."""
    if "start_date" in df.columns:
        df = df.copy()
        df["start_date"] = df["start_date"].astype(str).replace("NaT", "")
    return df


def _with_start_date(df, raw):
    """Attach a cleaned ``start_date`` column (ported from Eur's
    ``_clean_date``) alongside Earth's own column set — Earth's
    ``prepare_lines_df``/``prepare_substation_df`` whitelist doesn't carry
    ``tags.start_date`` through, but the row order/index is unchanged at this
    point so it can be reattached directly."""
    if "tags.start_date" in raw.columns:
        df["start_date"] = _clean_date(raw["tags.start_date"]).values
    else:
        df["start_date"] = pd.NaT
    return df


def _clean_country_lines(raw_line_path, raw_cable_path, clean_config, crs_config):
    """One country's worth of Earth's clean_data() line-processing block."""
    df_lines = gpd.GeoDataFrame()
    if not _empty(raw_line_path):
        raw = gpd.read_file(raw_line_path)
        df_lines = _with_start_date(prepare_lines_df(raw), raw)
        df_lines = finalize_lines_type(df_lines)

    if not _empty(raw_cable_path):
        raw_c = gpd.read_file(raw_cable_path)
        df_cables = _with_start_date(prepare_lines_df(raw_c), raw_c)
        df_cables = finalize_lines_type(df_cables)
        df_lines = pd.concat([df_lines, df_cables], ignore_index=True)

    if df_lines.empty:
        return df_lines

    df_lines = integrate_lines_df(df_lines, crs_config["distance_crs"])

    df_lines = filter_voltage(df_lines, clean_config["min_voltage_ac"])
    df_lines = filter_frequency(df_lines)
    df_lines = filter_circuits(df_lines)
    df_lines = filter_lines_by_geometry(df_lines)

    df_lines = _treat_under_construction(
        df_lines,
        clean_config["under_construction"]["mode"],
        clean_config["under_construction"].get("remove_after"),
    )

    df_lines = gpd.GeoDataFrame(df_lines, geometry="geometry")

    if clean_config["country_assignment"] == "gadm_shape":
        df_lines = set_countryname_by_shape(df_lines, clean_config["_ext_country_shapes"])

    return df_lines


def _clean_country_buses(raw_substation_path, df_lines, clean_config, poi_tolerance):
    """One country's worth of Earth's clean_data() substation-processing
    block, upgraded with Eur's pole-of-inaccessibility geometry."""
    if _empty(raw_substation_path):
        return gpd.GeoDataFrame()

    raw = gpd.read_file(raw_substation_path)
    # Ported from Eur: compute the pole of inaccessibility on the raw polygon
    # before Earth's prepare_substation_df collapses "geometry" to a centroid
    # — since prepare_substation_df's .centroid on an already-Point geometry
    # is a no-op, this composes cleanly with Earth's unmodified function. The
    # extra "polygon" column _create_substations_poi adds is discarded a few
    # lines later by prepare_substation_df's own column whitelist.
    raw = _create_substations_poi(raw, poi_tolerance)

    df_all_buses = _with_start_date(prepare_substation_df(raw), raw)

    tag_substation = clean_config["tag_substation"]
    if tag_substation:
        df_all_buses = df_all_buses[df_all_buses["tag_substation"] == tag_substation]

    df_all_buses = clean_voltage(df_all_buses)

    df_all_buses = gpd.GeoDataFrame(
        split_cells(pd.DataFrame(df_all_buses)),
        crs=df_all_buses.crs,
    )

    if clean_config["add_line_endings"]:
        df_all_buses = add_line_endings_tosubstations(df_all_buses, df_lines)

    df_all_buses.dropna(subset=["geometry"], axis=0, inplace=True)
    df_all_buses = filter_voltage(df_all_buses, clean_config["min_voltage_ac"])
    df_all_buses = _treat_under_construction(
        df_all_buses,
        clean_config["under_construction"]["mode"],
        clean_config["under_construction"].get("remove_after"),
    )
    df_all_buses = finalize_substation_types(df_all_buses)
    df_all_buses = gpd.GeoDataFrame(df_all_buses, geometry="geometry")

    if clean_config["country_assignment"] == "gadm_shape":
        df_all_buses = set_countryname_by_shape(
            df_all_buses, clean_config["_ext_country_shapes"]
        )

    return df_all_buses


def _clean_country_generators(raw_generator_path, clean_config):
    """One country's worth of Earth's clean_data() generator-processing block."""
    if _empty(raw_generator_path):
        return gpd.GeoDataFrame()

    df_all_generators = prepare_generators_df(gpd.read_file(raw_generator_path))

    if clean_config["country_assignment"] == "gadm_shape":
        df_all_generators = set_countryname_by_shape(
            df_all_generators, clean_config["_ext_country_shapes"], col_country="Country"
        )

    if clean_config["generators"]["name_method"] == "closest_city":
        df_all_generators = set_name_by_closestcity(df_all_generators)

    return df_all_generators


def clean_data(
    input_files: dict[str, list[str]],
    output_files: dict[str, str],
    clean_config: dict[str, Any],
    crs_config: dict[str, Any],
) -> None:
    """Clean every configured country's raw OSM data (Earth's clean_data(),
    looped per country) and combine it into grid-builder's canonical,
    cross-border-deduplicated schema (Eur's _drop_duplicate_lines)."""
    if clean_config["country_assignment"] == "gadm_shape":
        raise NotImplementedError(
            "clean.country_assignment == 'gadm_shape' needs GADM country/offshore "
            "shapefiles wired into the clean_osm rule's input (Earth's "
            "create_extended_country_shapes/set_countryname_by_shape are ported "
            "and ready to use, but nothing supplies them shapes yet) — use "
            "'osm_tag' until that follow-up is done."
        )
    n_countries = len(input_files["substation"])
    cables = input_files.get("cable") or [None] * n_countries
    generators = input_files.get("generator") or [None] * n_countries

    poi_tolerance = clean_config.get("poi_tolerance", 1e-4)

    all_lines, all_buses, all_generators = [], [], []
    for i in range(n_countries):
        df_lines = _clean_country_lines(
            input_files["line"][i], cables[i], clean_config, crs_config
        )
        all_lines.append(df_lines)
        all_buses.append(
            _clean_country_buses(
                input_files["substation"][i], df_lines, clean_config, poi_tolerance
            )
        )
        if clean_config["generators"]["enabled"] and "generator" in input_files:
            all_generators.append(
                _clean_country_generators(generators[i], clean_config)
            )

    lines = pd.concat(all_lines, ignore_index=True) if all_lines else gpd.GeoDataFrame()
    buses = pd.concat(all_buses, ignore_index=True) if all_buses else gpd.GeoDataFrame()

    if clean_config["dedupe_cross_border"] and not lines.empty:
        lines = _drop_duplicate_lines(lines, "line_id")

    if not lines.empty:
        lines = set_unique_id(lines, "line_id")
    if not buses.empty:
        buses = set_unique_id(buses, "bus_id")

    geo_crs = crs_config["geo_crs"]
    gpd.GeoDataFrame(_stringify_dates(lines), geometry="geometry", crs=geo_crs).to_file(
        output_files["lines"], driver="GeoJSON"
    )
    gpd.GeoDataFrame(_stringify_dates(buses), geometry="geometry", crs=geo_crs).to_file(
        output_files["buses"], driver="GeoJSON"
    )

    if clean_config["generators"]["enabled"] and "generators" in output_files:
        generators_df = (
            pd.concat(all_generators, ignore_index=True)
            if all_generators
            else gpd.GeoDataFrame()
        )
        gpd.GeoDataFrame(generators_df, geometry="geometry", crs=geo_crs).to_file(
            output_files["generators"], driver="GeoJSON"
        )


if __name__ == "__main__":
    if "snakemake" not in globals():
        from workflow.scripts._helpers import mock_snakemake

        snakemake = mock_snakemake("clean_osm")

    logging.basicConfig(level=snakemake.config.get("loglevel", "INFO"))

    clean_data(
        input_files=dict(snakemake.input.items()),
        output_files=dict(snakemake.output.items()),
        clean_config=snakemake.params.clean,
        crs_config=snakemake.params.crs,
    )
