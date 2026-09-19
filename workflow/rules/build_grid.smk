# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT

# Following PyPSA-Earth/PyPSA-Eur convention: each Snakemake rule name here
# matches the name of the script it runs.


rule clean_osm:
    input:
        # clean_osm has no {country} wildcard — it cleans every country in
        # config["countries"] in one call, matching PyPSA-Earth's own
        # clean_osm_data rule (a single rule with no wildcard, since Earth's
        # retrieval step already aggregates every country before cleaning
        # runs). Optional inputs are inlined as plain if/else expressions,
        # the same idiom PyPSA-Earth/Eur use for conditional rule inputs —
        # no separate input-building variable or function is needed.
        substation=expand(
            "<resources>/osm/out/{country}_substation.geojson",
            country=config["countries"],
        ),
        line=expand(
            "<resources>/osm/out/{country}_line.geojson", country=config["countries"]
        ),
        cable=(
            expand(
                "<resources>/osm/out/{country}_cable.geojson",
                country=config["countries"],
            )
            if "cable" in config["retrieve"]["features"]
            else []
        ),
        generator=(
            expand(
                "<resources>/osm/out/{country}_generator.geojson",
                country=config["countries"],
            )
            if config["clean"]["generators"]["enabled"]
            and "generator" in config["retrieve"]["features"]
            else []
        ),
    output:
        buses="<resources>/osm/clean/all_buses.geojson",
        lines="<resources>/osm/clean/all_lines.geojson",
        generators=(
            "<resources>/osm/clean/all_generators.geojson"
            if config["clean"]["generators"]["enabled"]
            else []
        ),
    log:
        "<logs>/clean_osm.log",
    conda:
        "../envs/shell.yaml"
    params:
        clean=config["clean"],
        crs=config["crs"],
    message:
        "Clean and cross-border-deduplicate OSM data for all countries."
    script:
        "../scripts/clean_osm.py"


rule build_network:
    input:
        buses=rules.clean_osm.output.buses,
        lines=rules.clean_osm.output.lines,
    output:
        buses="<resources>/osm/build/buses.csv",
        lines="<resources>/osm/build/lines.csv",
        converters="<resources>/osm/build/converters.csv",
        transformers="<resources>/osm/build/transformers.csv",
    log:
        "<logs>/build_network.log",
    conda:
        "../envs/shell.yaml"
    params:
        build_network=config["build_network"],
        crs=config["crs"],
    message:
        "Build final network topology from cleaned OSM data."
    script:
        "../scripts/build_network.py"
