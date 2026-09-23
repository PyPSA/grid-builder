# grid-builder

A modular Snakemake workflow for retrieving OpenStreetMap power infrastructure.

<p align="center">
  <img src="./figures/map_europe.png" width="50%">
</p>

## About

`grid-builder` is a small, modular `snakemake` workflow that retrieves OpenStreetMap power infrastructure using earth-osm. It can be imported into another `snakemake` workflow.

The current implementation retrieves substations and lines by default and exports raw CSV and GeoJSON files for each configured country. It does not yet filter for high voltage, infer electrical parameters, build network topology, or validate a power grid model. Those are planned extensions toward use in energy system modelling.

This module follows the Modelblocks conventions (https://www.modelblocks.org). For more information, consult the [integration example](./tests/integration/Snakefile) and the `snakemake` [modularisation documentation](https://snakemake.readthedocs.io/en/stable/snakefiles/modularization.html).

## Overview

Currently implemented:

1. Retrieve OSM power infrastructure by country and feature.
2. Export CSV and GeoJSON files for downstream processing.

## Configuration

Please consult the configuration [README](./config/README.md) and the [configuration example](./config/config.yaml) for a general overview on the configuration options of this module.

## Input / output structure

Please consult the [interface file](./INTERFACE.yaml) for more information.

Outputs use `<resources>/osm/out/{country}_{feature}.{csv,geojson}`;
country logs use `<logs>/retrieve_osm/{country}.log`. The integration example
sets these roots to `resources/grid-builder` and `logs/grid-builder`.
Downloaded PBF files are cached in `data/earth-osm` in this checkout.

## Development

We use [`pixi`](https://pixi.sh/) as our package manager for development.
Once installed, run the following to clone this repository and install all dependencies.

```shell
git clone git@github.com:PyPSA/grid-builder.git
cd grid-builder
pixi install --locked
```

For testing, simply run:

```shell
pixi run --locked lint
pixi run --locked test
```

To test a minimal example of a workflow using this module:

```shell
pixi shell                          # activate this project's environment
cd tests/integration/               # navigate to the integration example
snakemake --use-conda --cores 2      # run the workflow!
```

The Pixi environment supplies Snakemake and the configuration-validation
dependencies. Snakemake installs the retrieval script's dependencies from
`workflow/envs/retrieve.yaml` when `--use-conda` is enabled. A consuming workflow
must also provide the host dependencies from `pixi.toml`; importing the module
does not activate its Pixi environment automatically.

The integration test uses a fresh temporary output directory, runs the retrieval
Conda environment, and checks CSV/GeoJSON contents and country logging. It needs
internet access on the first run to install dependencies and download Benin's
OSM extract; subsequent runs can reuse those caches. Test logs are retained in
`tests/integration/logs`.

Each country job runs with one worker. Keep `retrieve.mp: false`: earth-osm 3.0.2
does not expose a worker limit, so enabling its multiprocessing would bypass
Snakemake's CPU allocation. Snakemake can still run multiple country jobs in
parallel using `--cores`.

If this checkout is moved and commands fail with a `bad interpreter` error,
rebuild the installed environment with `pixi reinstall --locked`.

## License

`grid-builder` is released as free software under the [MIT](LICENSE) license. Different licenses and terms of use may apply to input data, e.g. OpenStreetMap data is subject to the [Open Database License](https://opendatacommons.org/licenses/odbl).

## References & related work

* Jonas Hörsch et al. 2018. PyPSA-Eur: An open optimisation model of the European transmission system, *Energy Strategy Reviews*, Volume 22. https://doi.org/10.1016/j.esr.2018.08.012
* Maximilian Parzen et al. 2023. PyPSA-Earth: A new global open energy system optimization model demonstrated in Africa, *Applied Energy*, Volume 341. https://doi.org/10.1016/j.apenergy.2023.121096
* Bobby Xiong et al. 2025. Modelling the high-voltage grid using open data for Europe and beyond. *Sci Data* 12, 277. https://doi.org/10.1038/s41597-025-04550-7
