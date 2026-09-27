# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT

from scripts._helpers import custom_files


rule clean:
    input:
        lines_way=expand(
            "<resources>/retrieve/{country}_lines_way.json",
            country=config["countries"],
        )
        + custom_files(config["custom_data"]["files"], "lines_way"),
        cables_way=expand(
            "<resources>/retrieve/{country}_cables_way.json",
            country=config["countries"],
        )
        + custom_files(config["custom_data"]["files"], "cables_way"),
        substations_way=expand(
            "<resources>/retrieve/{country}_substations_way.json",
            country=config["countries"],
        )
        + custom_files(config["custom_data"]["files"], "substations_way"),
        substations_node=expand(
            "<resources>/retrieve/{country}_substations_node.json",
            country=config["countries"],
        )
        + custom_files(config["custom_data"]["files"], "substations_node"),
        substations_relation=expand(
            "<resources>/retrieve/{country}_substations_relation.json",
            country=config["countries"],
        )
        + custom_files(config["custom_data"]["files"], "substations_relation"),
        routes_relation=(
            expand(
                "<resources>/retrieve/{country}_routes_relation.json",
                country=config["countries"],
            )
            + custom_files(config["custom_data"]["files"], "routes_relation")
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
        converters="<resources>/build/csv/converters.csv",
        buses_geojson="<resources>/build/geojson/buses.geojson",
        lines_geojson="<resources>/build/geojson/lines.geojson",
        transformers_geojson="<resources>/build/geojson/transformers.geojson",
        converters_geojson="<resources>/build/geojson/converters.geojson",
        stations_polygon="<resources>/build/geojson/stations_polygon.geojson",
        buses_polygon="<resources>/build/geojson/buses_polygon.geojson",
    log:
        "<logs>/build_network.log",
    conda:
        "../envs/network.yaml"
    threads: 1
    params:
        station_merge_radius_m=config["network"]["station_merge_radius_m"],
        station_bus_offset_m=config["network"]["station_bus_offset_m"],
        overpassing_lines_tolerance_m=config["network"]["overpassing_lines_tolerance_m"],
        converter_search_radius_m=config["network"]["converter_search_radius_m"],
        remove_under_construction=config["network"]["remove_under_construction"],
        remove_after=config["network"]["remove_after"],
        crs=config["crs"].model_dump(mode="json"),
    message:
        "Building a connected generic OSM network."
    script:
        "../scripts/build_network.py"
