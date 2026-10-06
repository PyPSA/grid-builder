# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT


rule clean:
    input:
        lines_way=expand(
            workflow.pathvars.apply("<osm_lines_way>"),
            country=config["countries"],
        ),
        cables_way=expand(
            workflow.pathvars.apply("<osm_cables_way>"),
            country=config["countries"],
        ),
        substations_way=expand(
            workflow.pathvars.apply("<osm_substations_way>"),
            country=config["countries"],
        ),
        substations_node=expand(
            workflow.pathvars.apply("<osm_substations_node>"),
            country=config["countries"],
        ),
        substations_relation=expand(
            workflow.pathvars.apply("<osm_substations_relation>"),
            country=config["countries"],
        ),
        routes_relation=(
            expand(
                workflow.pathvars.apply("<osm_routes_relation>"),
                country=config["countries"],
            )
            if config["network"]["include_relations"]
            else []
        ),
    output:
        substations="<resources>/automatic/clean/substations.geojson",
        substations_polygon="<resources>/automatic/clean/substations_polygon.geojson",
        lines="<resources>/automatic/clean/lines.geojson",
    log:
        "<logs>/clean.log",
    conda:
        "../envs/module.yaml"
    threads: 1
    params:
        countries=[_schema.get_region_tuple(c).short for c in config["countries"]],
        network=config["network"],
        regions=config["regions"],
        crs=config["crs"],
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
        buses="<buses>",
        lines="<lines>",
        transformers="<transformers>",
        buses_geojson="<buses_geojson>",
        lines_geojson="<lines_geojson>",
        transformers_geojson="<transformers_geojson>",
        stations_polygon="<stations_polygon>",
        buses_polygon="<buses_polygon>",
    log:
        "<logs>/build_network.log",
    conda:
        "../envs/module.yaml"
    threads: 1
    params:
        station_merge_radius_m=config["network"]["station_merge_radius_m"],
        remove_under_construction=config["network"]["remove_under_construction"],
        remove_after=config["network"]["remove_after"],
        crs=config["crs"],
    message:
        "Building a connected generic OSM network."
    script:
        "../scripts/build_network.py"
