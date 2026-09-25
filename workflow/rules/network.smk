# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT


rule clean:
    input:
        lines_way=expand(
            "<resources>/retrieve/{country}_lines_way.json",
            country=config["countries"],
        ),
        cables_way=expand(
            "<resources>/retrieve/{country}_cables_way.json",
            country=config["countries"],
        ),
        substations_way=expand(
            "<resources>/retrieve/{country}_substations_way.json",
            country=config["countries"],
        ),
        substations_node=expand(
            "<resources>/retrieve/{country}_substations_node.json",
            country=config["countries"],
        ),
        substations_relation=expand(
            "<resources>/retrieve/{country}_substations_relation.json",
            country=config["countries"],
        ),
        routes_relation=(
            expand(
                "<resources>/retrieve/{country}_routes_relation.json",
                country=config["countries"],
            )
            if config["network"]["include_relations"]
            else []
        ),
    output:
        substations="<resources>/clean/substations.geojson",
        substations_polygon="<resources>/clean/substations_polygon.geojson",
        lines="<resources>/clean/lines.geojson",
    log:
        "<logs>/clean.log",
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
        "../scripts/clean.py"


rule build_network:
    input:
        substations=rules.clean.output.substations,
        substations_polygon=rules.clean.output.substations_polygon,
        lines=rules.clean.output.lines,
    output:
        buses="<resources>/build/csv/buses.csv",
        lines="<resources>/build/csv/lines.csv",
        transformers="<resources>/build/csv/transformers.csv",
        buses_geojson="<resources>/build/geojson/buses.geojson",
        lines_geojson="<resources>/build/geojson/lines.geojson",
        transformers_geojson="<resources>/build/geojson/transformers.geojson",
        stations_polygon="<resources>/build/geojson/stations_polygon.geojson",
        buses_polygon="<resources>/build/geojson/buses_polygon.geojson",
    log:
        "<logs>/build_network.log",
    conda:
        "../envs/network.yaml"
    threads: 1
    params:
        station_merge_radius_m=config["network"]["station_merge_radius_m"],
        remove_under_construction=config["network"]["remove_under_construction"],
        remove_after=config["network"]["remove_after"],
        crs=config["crs"].model_dump(mode="json"),
    message:
        "Building a connected generic OSM network."
    script:
        "../scripts/build_network.py"
