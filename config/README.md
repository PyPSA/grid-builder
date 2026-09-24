Set `countries` to the ISO country codes that define the retrieval scope. The
workflow first loads the default `config/config.yaml`, then an optional
`config/regions/config.<ISO>.yaml` file for every selected country. A `regions`
mapping in the calling configuration overrides values from those regional files.

`retrieve.source` picks the retrieval backend: `geofabrik` reads a cached local
PBF extract (`retrieve_osm_pbf.py`), `overpass` queries the live Overpass API
(`retrieve_osm_overpass.py`). Both produce the same output shape, so
`clean_osm_data` doesn't need to know which one ran. `retrieve.include_relations`
additionally retrieves `route=power`/`power=circuit` relations, so member ways
are grouped into one line per real-world circuit. `network` controls the
minimum retained AC voltage, station merge buffer radius, construction filtering, and
planned-asset cutoff date.

The [BE+NL example](./examples/config.BE-NL.yaml) is a small European development
scope. Country files under `config/regions` are intentionally small defaults for
now; community-maintained local corrections belong there rather than in workflow
code.

### Personal settings and Overpass fair use

Keep `config/config.yaml` as pure defaults — a test enforces that it matches the
schema, and it's tracked in git, so it's not the place for anything
environment- or person-specific. For local overrides (a custom Overpass
endpoint, contact details, a smaller `countries` scope for development), create
an untracked `config/config.local.yaml` and pass it alongside the default:

```shell
snakemake --configfile config/config.local.yaml ...
```

Snakemake deep-merges it on top of `config/config.yaml`, so you only need to
list the keys you're overriding.

If you use `retrieve.source: overpass`, set `retrieve.overpass_api.user_agent`
to your own project name, contact email, and website. The [Overpass API fair
use policy](https://wiki.openstreetmap.org/wiki/Overpass_API#Fair_use_policy)
expects automated queries to be identifiable and reachable; a generic or
missing user agent risks being rate-limited or blocked. `retrieve.overpass_api.url`
also lets you point at your own or a faster mirror instance instead of the
shared public endpoint, without touching the checked-in default.

The generated [schema](./config.schema.json) describes every option.
