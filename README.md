# grid-builder

A modular Snakemake workflow for retrieving OpenStreetMap power infrastructure.

<p align="center">
  <img src="./figures/map_europe.png" width="50%">
</p>

## About

`grid-builder` is a modular `snakemake` workflow that retrieves OpenStreetMap power infrastructure with earth-osm and builds a generic high-voltage network. It can be imported into another `snakemake` workflow.

The workflow retains AC substations, overhead lines, and cables at configured voltage levels, then creates generic buses, connected line segments, and voltage-pair transformers. The outputs preserve OSM provenance and geometry but contain no PyPSA-specific line types, capacities, or electrical-component assumptions.

This module follows the Modelblocks conventions (https://www.modelblocks.org). For more information, consult the [integration example](./tests/integration/Snakefile) and the `snakemake` [modularisation documentation](https://snakemake.readthedocs.io/en/stable/snakefiles/modularization.html).

## Overview

Currently implemented:

1. Retrieve OSM substations, lines, and cables by country with earth-osm.
2. Clean native earth-osm CSV exports, filtering voltage, frequency, construction status, and future assets.
3. Merge nearby stations and line endpoints into generic buses, AC lines, and transformers.

## Configuration

Set `countries` in the main configuration to select the scope. For every selected ISO code, the workflow loads an optional `config/regions/config.<ISO>.yaml` file; values placed directly in `regions` in the main configuration take precedence. The [BE+NL example](./config/examples/config.BE-NL.yaml) is a runnable development scope.

Please consult the configuration [README](./config/README.md) and the [configuration example](./config/config.yaml) for the available controls.

## Input / output structure

Please consult the [interface file](./INTERFACE.yaml) for more information.

Raw retrieval outputs use `<resources>/osm/out/{country}_{feature}.{csv,geojson}`.
Clean features use `<resources>/osm/clean/*.geojson`; generic network components
use `<resources>/osm/build/{buses,lines,transformers}.csv` and matching GeoJSON
files. Country logs use `<logs>/retrieve_osm/{country}.log`. The integration
example sets these roots to `resources/grid-builder` and `logs/grid-builder`.
Downloaded PBF files are cached in `data/earth-osm` in this checkout.

The initial topology path uses earth-osm's native node and way records. Support
for relation-based and DC assets is a subsequent extension of the same retrieval
interface.

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

The integration test uses a fresh temporary output directory, runs the retrieval,
cleaning, and generic network Conda environments, and checks the resulting
components and country logging. It needs internet access on the first run to
install dependencies and download Benin's OSM extract; subsequent runs can reuse
those caches. Test logs are retained in `tests/integration/logs`.

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
