# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT


rule build_interactive_map:
    input:
        buses=rules.build_network.output.buses_geojson,
        lines=rules.build_network.output.lines_geojson,
        transformers=rules.build_network.output.transformers_geojson,
        stations_polygon=rules.build_network.output.stations_polygon,
        buses_polygon=rules.build_network.output.buses_polygon,
    output:
        map="<resources>/map.html",
    log:
        "<logs>/build_interactive_map.log",
    conda:
        "../envs/network.yaml"
    threads: 1
    params:
        crs=config["crs"],
        interactive_map=config["interactive_map"],
    message:
        "Building an interactive OSM network map."
    script:
        "../scripts/build_interactive_map.py"
