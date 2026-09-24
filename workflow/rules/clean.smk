# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT


rule clean_osm_data:
    input:
        lines_way=expand(
            "<resources>/osm/retrieve/{country}_lines_way.json",
            country=config["countries"],
        ),
        cables_way=expand(
            "<resources>/osm/retrieve/{country}_cables_way.json",
            country=config["countries"],
        ),
        substations_way=expand(
            "<resources>/osm/retrieve/{country}_substations_way.json",
            country=config["countries"],
        ),
        substations_node=expand(
            "<resources>/osm/retrieve/{country}_substations_node.json",
            country=config["countries"],
        ),
        substations_relation=expand(
            "<resources>/osm/retrieve/{country}_substations_relation.json",
            country=config["countries"],
        ),
        routes_relation=(
            expand(
                "<resources>/osm/retrieve/{country}_routes_relation.json",
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
        crs=config["crs"].model_dump(mode="json"),
    message:
        "Cleaning retrieved OSM power features."
    script:
        "../scripts/clean_osm_data.py"
