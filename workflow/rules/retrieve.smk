# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT


rule retrieve_osm:
    output:
        csv=expand(
            "<resources>/automatic/osm/out/{country}_{feature}.csv",
            country="{country}",
            feature=config["retrieve"]["features"],
        ),
        geojson=expand(
            "<resources>/automatic/osm/out/{country}_{feature}.geojson",
            country="{country}",
            feature=config["retrieve"]["features"],
        ),
    log:
        "<logs>/retrieve_osm/{country}.log",
    conda:
        "../envs/shell.yaml"
    params:
        primary_name=config["retrieve"]["primary_name"],
        features=config["retrieve"]["features"],
        source=config["retrieve"]["source"],
        force_redownload=config["retrieve"]["force_redownload"],
        mp=config["retrieve"]["mp"],
        stream_backend=config["retrieve"]["stream_backend"],
        cache_primary=config["retrieve"]["cache_primary"],
        target_date=config["retrieve"]["target_date"],
    message:
        "Retrieve OSM data for one country."
    script:
        "../scripts/retrieve_osm.py"


rule retrieve_osm_all:
    input:
        csv=expand(
            "<resources>/automatic/osm/out/{country}_{feature}.csv",
            country=config["countries"],
            feature=config["retrieve"]["features"],
        ),
        geojson=expand(
            "<resources>/automatic/osm/out/{country}_{feature}.geojson",
            country=config["countries"],
            feature=config["retrieve"]["features"],
        ),
