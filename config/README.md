Set `countries` to the ISO country codes that define the retrieval scope. The workflow first loads the default `config/config.yaml`, then an optional `config/regions/config.<ISO>.yaml` file for every selected country. A `regions` mapping in the calling configuration overrides values from those regional files.

`retrieve.source` picks the retrieval backend: `geofabrik` reads a cached local PBF extract (`retrieve_osm_pbf.py`), `overpass` queries the live Overpass API (`retrieve_osm_overpass.py`). Both produce the same output shape, so `clean` doesn't need to know which one ran. `network.include_relations` decides whether the network should consider `route=power`/`power=circuit` relations, grouping their member ways into one line per real-world circuit; retrieval respects this too, so relations aren't fetched at all when it's off. `network` also controls the minimum retained AC voltage, station merge buffer radius, construction filtering, and planned-asset cutoff date.

`network.station_merge_radius_m` is a buffer radius, not a merge distance: both sides of a pair are buffered by it before the buffers are dissolved, so two elements merge once they are within *twice* the value of each other. The default of 500 therefore merges substations up to a kilometre apart.

`network.max_station_voltage_ratio` guards against the over-merging that radius can cause. Buffer-and-union clustering is transitive, so a dense corridor of buses can chain into one oversized station, and an implausibly wide voltage span is the usual symptom. Setting it splits any station whose highest and lowest bus voltage differ by more than that factor into one station per voltage. It is null by default, which disables the guard.

Choose the value above the largest step a real substation in your grid steps down, and verify it against your own countries before enabling it. Too low a value splits legitimate multi-voltage yards, and because transformers are only created between buses of the same station, every wrongly split yard silently loses its transformer. Colombia is a worked example: its backbone pairs 500 kV with 230 kV, a ratio of 2.17, so `2.0` splits 14 perfectly normal stations and drops the country from 20 transformers to 4, while `3.0` changes nothing.

The [BE+NL example](./examples/config.BE-NL.yaml) is a small European development scope. Country files under `config/regions` are intentionally small defaults for now; community-maintained local corrections belong there rather than in workflow code.

### Adding custom data

OSM's high-voltage coverage is uneven, and editing OSM upstream and waiting for the next extract is a slow way to correct a missing or mistagged asset. It also makes a study hard to reproduce, since upstream keeps changing. List extra raw files under `custom_data.files` to have `clean` read them alongside the retrieved ones:

```yaml
custom_data:
  files:
    - data/custom/BE_lines_way.json
```

Each file holds raw elements in the same `{"elements": [...]}` shape both retrieval backends write, and must be named `{country}_{feature}.json`, because `clean` reads the country and feature straight back out of the filename. The feature has to be one the retrieval step produces: `lines_way`, `cables_way`, `substations_way`, `substations_node`, `substations_relation`, or `routes_relation`. Their elements are added to the retrieved ones, and the files are declared inputs of the `clean` rule, so a missing one fails the run instead of being skipped silently.

`interactive_map` controls the size of `map.html`: `coordinate_decimals` rounds embedded coordinates, and `simplify_geometries` sets per-geometry-type Douglas-Peucker tolerances (in metres) for station polygons, bus polygons, and lines, or disables simplification entirely via `simplify_geometries.enable`.

### Personal settings and Overpass fair use

Keep `config/config.yaml` as pure defaults — a test enforces that it matches the schema, and it's tracked in git, so it's not the place for anything environment- or person-specific. For local overrides (a custom Overpass endpoint, contact details, a smaller `countries` scope for development), create an untracked `config/config.local.yaml` and pass it alongside the default:

```shell
snakemake --configfile config/config.local.yaml ...
```

Snakemake deep-merges it on top of `config/config.yaml`, so you only need to list the keys you're overriding.

If you use `retrieve.source: overpass`, set `retrieve.overpass_api.user_agent` to your own project name, contact email, and website. The [Overpass API fair use policy](https://wiki.openstreetmap.org/wiki/Overpass_API#Fair_use_policy) expects automated queries to be identifiable and reachable; a generic or missing user agent risks being rate-limited or blocked. `retrieve.overpass_api.url` also lets you point at your own or a faster mirror instance instead of the shared public endpoint, without touching the checked-in default.

The generated [schema](./config.schema.json) describes every option.
