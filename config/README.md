Set `countries` to the ISO country codes that define the retrieval scope. The workflow first loads the default `config/config.yaml`, then an optional `config/regions/config.<ISO>.yaml` file for every selected country. A `regions` mapping in the calling configuration overrides values from those regional files.

`retrieve.source` picks the retrieval backend: `geofabrik` reads a cached local PBF extract (`retrieve_osm_pbf.py`), `overpass` queries the live Overpass API (`retrieve_osm_overpass.py`). Retrieven data are transferred to the cleaning phase and after that are used to build a topologically-clean network model.

A parameter `network.include_relations` defines whether the network should consider OSM relations `route=power`/`power=circuit` , grouping their member ways into one line per real-world circuit. In the network-building phase, `network` scripts accepts custom values the minimum retained AC voltage, station merge buffer radius and a construction status.

`network.station_merge_radius_m` is a buffer radius with the merge distance being *twice* as high.E.g.the default value of 500 m merges substations up to one kilometre apart.

The [BE+NL example](./examples/config.BE-NL.yaml) is a small European development scope. Country files under `config/regions` are intentionally small defaults for now; community-maintained local corrections belong there rather than in workflow code.

### Adding custom data

The workflow provides an option to add custom data which can be handy to deal with inputs which are out of scope for OpenStreetMap, such as planned lines. To inject custom files into the worklow, a filed `custom_data.files` can be used:

```yaml
custom_data:
  files:
    - data/custom/BE_lines_way.json
```

Enabling this functionality makes `clean` read the custom files alongside the retrieved ones. The expected format correspond to raw elements and must be named `{country}_{feature}.json`. The feature has to be one the retrieval step produces: `lines_way`, `cables_way`, `substations_way`, `substations_node`, `substations_relation`, or `routes_relation`. 

`interactive_map` controls the parameters of `map.html` with `coordinate_decimals` for displayed precision of coordinates, and `simplify_geometries` in meteres applied to station polygons, bus polygons, and lines.

### Personal settings and Overpass fair use

Keep a git-tracked `config/config.yaml` as pure defaults. A test enforces that this file matches the schema. For local overrides, such as a custom Overpass endpoint, contact details, a smaller `countries` scope, please create an untracked `config/config.local.yaml` and pass it alongside the default:

```shell
snakemake --configfile config/config.local.yaml ...
```

Snakemake deep-merges it on top of `config/config.yaml`, so you only need to list the keys you're overriding.

If you use `retrieve.source: overpass`, set `retrieve.overpass_api.user_agent` to your own project name, contact email, and website. The [Overpass API fair use policy](https://wiki.openstreetmap.org/wiki/Overpass_API#Fair_use_policy) expects automated queries to be identifiable and reachable; a generic or missing user agent risks being rate-limited or blocked. `retrieve.overpass_api.url` also lets you point at your own or a faster mirror instance instead of the shared public endpoint, without touching the checked-in default.

The generated [schema](./config.schema.json) describes every option.
