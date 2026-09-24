# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT


rule build_osm_network:
    input:
        substations=rules.clean_osm_data.output.substations,
        substations_polygon=rules.clean_osm_data.output.substations_polygon,
        lines=rules.clean_osm_data.output.lines,
    output:
        buses="<resources>/osm/build/csv/buses.csv",
        lines="<resources>/osm/build/csv/lines.csv",
        transformers="<resources>/osm/build/csv/transformers.csv",
        buses_geojson="<resources>/osm/build/geojson/buses.geojson",
        lines_geojson="<resources>/osm/build/geojson/lines.geojson",
        transformers_geojson="<resources>/osm/build/geojson/transformers.geojson",
        stations_polygon="<resources>/osm/build/geojson/stations_polygon.geojson",
        buses_polygon="<resources>/osm/build/geojson/buses_polygon.geojson",
    log:
        "<logs>/build_osm_network.log",
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
        "../scripts/build_osm_network.py"
