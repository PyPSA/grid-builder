# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT

from pathlib import Path

# Both retrieve_osm_pbf and retrieve_osm_overpass produce this same fixed
# set of six files per country (routes_relation included even when
# retrieve.include_relations is off, just empty) — see either script's
# module docstring for why relations aren't optional at the retrieval layer
# even though clean_osm_data only ever reads routes_relation.json when the
# config flag is on. Only one of the two rules below is ever defined, since
# retrieve.source picks exactly one implementation for the same output
# paths — defining both unconditionally would make Snakemake's DAG
# ambiguous about which one produces a given {country}_{feature}.json.
_OSM_FEATURES = [
    "lines_way",
    "cables_way",
    "substations_way",
    "substations_node",
    "substations_relation",
    "routes_relation",
]
_OSM_OUTPUTS = {
    feature: f"<resources>/osm/retrieve/{{country}}_{feature}.json"
    for feature in _OSM_FEATURES
}


if config["retrieve"]["source"] == "geofabrik":

    rule retrieve_osm_pbf:
        output:
            **_OSM_OUTPUTS,
        log:
            "<logs>/retrieve_osm_pbf/{country}.log",
        conda:
            "../envs/retrieve.yaml"
        threads: 1
        params:
            include_relations=config["retrieve"]["include_relations"],
            force_redownload=config["retrieve"]["force_redownload"],
            data_dir=str(Path(workflow.basedir).parent / "data" / "earth-osm"),
        message:
            "Retrieve OSM power features for one country from a local PBF file."
        script:
            "../scripts/retrieve_osm_pbf.py"

elif config["retrieve"]["source"] == "overpass":

    rule retrieve_osm_overpass:
        output:
            **_OSM_OUTPUTS,
        log:
            "<logs>/retrieve_osm_overpass/{country}.log",
        conda:
            "../envs/retrieve.yaml"
        threads: 1
        params:
            include_relations=config["retrieve"]["include_relations"],
            overpass_api=config["retrieve"]["overpass_api"].model_dump(mode="json"),
        message:
            "Retrieve OSM power features for one country from the Overpass API."
        script:
            "../scripts/retrieve_osm_overpass.py"


rule retrieve_osm_all:
    input:
        expand(
            "<resources>/osm/retrieve/{country}_{feature}.json",
            country=config["countries"],
            feature=_OSM_FEATURES,
        ),
