# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT

"""Retrieve OSM data for a single country."""

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pyproj import datadir

os.environ.setdefault("PROJ_LIB", datadir.get_data_dir())

import earth_osm.eo as eo

if TYPE_CHECKING:
    snakemake: Any

logger = logging.getLogger(__name__)


def retrieve_osm_data(
    country: str,
    features: list[str],
    primary_name: str,
    source: str,
    base_dir: str,
    force_redownload: bool = False,
    mp: bool = True,
    stream_backend: bool = True,
    cache_primary: bool = False,
    target_date: str | None = None,
) -> None:
    """
    Retrieve OSM data for a single country using earth_osm.

    Parameters
    ----------
    country : str
        Country code (e.g., "BE", "DE").
    features : list[str]
        Feature types (e.g., ["substation", "line"]).
    primary_name : str
        Primary tag name for querying (e.g., "power").
    source : str
        Data source ("geofabrik" or "overpass").
    base_dir : str
        Base directory for output files.
    force_redownload : bool, optional
        Force re-download even if cached. Default is False.
    mp : bool, optional
        Enable multiprocessing. Default is True.
    stream_backend : bool, optional
        Enable streaming backend. Default is True.
    cache_primary : bool, optional
        Cache primary data. Default is False.
    target_date : str | None, optional
        Target date for historical data. Default is None.
    """
    logger.info(f"Retrieving OSM data for {country} with features: {features}")

    project_root = Path(__file__).resolve().parents[2]

    eo.save_osm_data(
        primary_name=primary_name,
        region_list=[country],
        feature_list=features,
        data_source=source,
        out_dir=str(base_dir),
        data_dir=str(project_root / "data" / "earth-osm"),
        out_format=["csv", "geojson"],
        update=force_redownload,
        mp=mp,
        stream_backend=stream_backend,
        cache_primary=cache_primary,
        target_date=target_date,
        out_aggregate=False,
    )

    logger.info(f"Successfully retrieved OSM data for {country}")


if __name__ == "__main__":
    if "snakemake" not in globals():
        from workflow.scripts._helpers import mock_snakemake

        snakemake = mock_snakemake(
            "retrieve_osm",
            country="benin",
        )

    # Extract parameters
    country = snakemake.wildcards.country
    features = list(snakemake.params.features)
    base_dir = str(Path(snakemake.output.geojson[0]).parent.parent)

    # Call main function
    retrieve_osm_data(
        country=country,
        features=features,
        primary_name=snakemake.params.primary_name,
        source=snakemake.params.source,
        base_dir=base_dir,
        force_redownload=snakemake.params.force_redownload,
        mp=snakemake.params.mp,
        stream_backend=snakemake.params.stream_backend,
        cache_primary=snakemake.params.cache_primary,
        target_date=snakemake.params.target_date,
    )