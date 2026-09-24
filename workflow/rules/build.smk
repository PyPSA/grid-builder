# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT


rule build_osm_network:
    input:
        substations=rules.clean_osm_data.output.substations,
        substations_polygon=rules.clean_osm_data.output.substations_polygon,
        lines=rules.clean_osm_data.output.lines,
    output:
        buses="<resources>/osm/build/buses.csv",
        lines="<resources>/osm/build/lines.csv",
        transformers="<resources>/osm/build/transformers.csv",
        buses_geojson="<resources>/osm/build/geojson/buses.geojson",
        lines_geojson="<resources>/osm/build/geojson/lines.geojson",
        transformers_geojson="<resources>/osm/build/geojson/transformers.geojson",
    log:
        "<logs>/build_osm_network.log",
    conda:
        "../envs/network.yaml"
    threads: 1
    params:
        station_merge_distance_m=config["network"]["station_merge_distance_m"],
        under_construction=config["network"]["under_construction"],
        remove_after=config["network"]["remove_after"],
    message:
        "Building a connected generic OSM network."
    script:
        "../scripts/build_osm_network.py"
