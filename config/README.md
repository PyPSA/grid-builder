Set `countries` to the ISO country codes that define the retrieval scope. The
workflow first loads the default `config/config.yaml`, then an optional
`config/regions/config.<ISO>.yaml` file for every selected country. A `regions`
mapping in the calling configuration overrides values from those regional files.

`retrieve.features` controls the native earth-osm exports. Keep `substation`,
`line`, and `cable` for the generic network workflow. `network` controls the
minimum retained AC voltage, station merge distance, construction filtering, and
planned-asset cutoff date.

The [BE+NL example](./examples/config.BE-NL.yaml) is a small European development
scope. Country files under `config/regions` are intentionally small defaults for
now; community-maintained local corrections belong there rather than in workflow
code.

The generated [schema](./config.schema.json) describes every option. The
[integration workflow](../tests/integration/Snakefile) shows how a consuming
Snakemake project imports this module.
