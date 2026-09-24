# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT


rule clean_osm_data:
    input:
        raw=expand(
            "<resources>/osm/out/{country}_{feature}.csv",
            country=config["countries"],
            feature=config["retrieve"]["features"],
        ),
        relations=(
            expand(
                "<resources>/osm/out/{country}_relation.json",
                country=config["countries"],
            )
            if config["retrieve"]["include_relations"]
            else []
        ),
    output:
        substations="<resources>/osm/clean/substations.geojson",
        substations_polygon="<resources>/osm/clean/substations_polygon.geojson",
        lines="<resources>/osm/clean/lines.geojson",
    log:
        "<logs>/clean_osm_data.log",
    conda:
        "../envs/network.yaml"
    threads: 1
    params:
        network=config["network"].model_dump(mode="json"),
        regions={
            code: value.model_dump(mode="json")
            for code, value in config["regions"].items()
        },
    message:
        "Cleaning retrieved OSM power features."
    script:
        "../scripts/clean_osm_data.py"
