# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT

"""Retrieve OSM power features for one country from a local PBF file.

Used for ``retrieve.source: geofabrik``. earth-osm is kept only for region
resolution and PBF download/caching (``get_region_tuple``,
``download_region_pbf``) — reading the file itself is done directly with
osmium, since earth-osm's own PBF parser (and its Overpass client) parses
relations internally but never exports them, and this way clean_osm_data.py
gets the exact same file shape regardless of whether the data came from a
local PBF (this script) or a live Overpass query (retrieve_osm_overpass.py).

Two stages, both there for the same reason: a country's full PBF extract can
contain tens of millions of nodes/ways for roads, buildings, etc. unrelated
to power infrastructure, and touching all of it is what makes this slow (or,
if done carelessly, memory-hungry).

1. ``osmium tags-filter`` (the CLI tool, run as a subprocess) shrinks the
   PBF down to only power-tagged elements plus their referenced members
   (included by default) before any Python code runs — a ~700MB country
   extract becomes well under 2MB. This is what actually keeps this fast:
   without it, pass 2 below still has to tag-check every way in the country
   even though it only stores geometry for a few thousand of them, and that
   check alone was taking 10+ minutes on a full-size PBF.
2. The filtered file is small enough that a plain single/two-pass pyosmium
   read of it is both fast and safe. Two passes: pass 1 scans relations only
   (cheap) to find which route/substation relations match, and which way
   IDs they reference; pass 2 resolves geometry only for ways that are
   either directly tagged as a power feature or were flagged as a relation
   member in pass 1.
"""

import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import osmium
from earth_osm.regions import download_region_pbf, get_region_tuple
from scripts._helpers import configure_logging

if TYPE_CHECKING:
    snakemake: Any

logger = logging.getLogger(__name__)

_WAY_FEATURES = ("line", "cable", "substation")


def _classify_feature(tags: dict[str, str]) -> str | None:
    """Return "line"/"cable"/"substation" if ``tags`` match that feature.

    A way matches a feature if it's tagged ``power=<feature>``,
    ``construction:power=<feature>``, or ``power=construction`` together
    with ``construction=<feature>``.
    """
    power = tags.get("power")
    construction_power = tags.get("construction:power")
    construction = tags.get("construction")
    for feature in _WAY_FEATURES:
        if power == feature or construction_power == feature:
            return feature
        if power == "construction" and construction == feature:
            return feature
    return None


def _is_route_relation(tags: dict[str, str]) -> bool:
    """True for a route relation: route=power, power=circuit, or a line/cable under construction."""
    return (
        tags.get("route") == "power"
        or tags.get("power") == "circuit"
        or tags.get("construction:power") in ("line", "cable")
        or tags.get("power") == "construction"
    )


def _is_substation_relation(tags: dict[str, str]) -> bool:
    """True for a multipolygon-style substation relation."""
    return (
        tags.get("power") == "substation"
        or tags.get("construction:power") == "substation"
        or (
            tags.get("power") == "construction"
            and tags.get("construction") == "substation"
        )
    )


class _RelationScanner(osmium.SimpleHandler):  # type: ignore[misc]
    """Pass 1: find matching relations and the way IDs they reference.

    Deliberately does not touch node/way geometry, so this pass is cheap
    regardless of country size.
    """

    def __init__(self, include_route_relations: bool) -> None:
        super().__init__()
        self._include_route_relations = include_route_relations
        self.route_relations: list[dict[str, Any]] = []
        self.substation_relations: list[dict[str, Any]] = []
        self.needed_way_ids: set[int] = set()

    def relation(self, r: Any) -> None:
        """Record a matching route/substation relation and its member way ids."""
        tags = dict(r.tags)
        is_route = self._include_route_relations and _is_route_relation(tags)
        is_substation = _is_substation_relation(tags)
        if not is_route and not is_substation:
            return
        members = [
            {
                "type": {"n": "node", "w": "way", "r": "relation"}[member.type],
                "ref": member.ref,
                "role": member.role,
            }
            for member in r.members
        ]
        element = {"type": "relation", "id": r.id, "tags": tags, "members": members}
        if is_route:
            self.route_relations.append(element)
        if is_substation:
            self.substation_relations.append(element)
        self.needed_way_ids.update(
            member.ref for member in r.members if member.type == "w"
        )


class _FeatureResolver(osmium.SimpleHandler):  # type: ignore[misc]
    """Pass 2: resolve nodes/ways/relations in one combined, filtered read.

    Only stores way geometry for ways that are themselves a power feature or
    were flagged as a relation member in pass 1 — not every way in the file.
    """

    def __init__(
        self,
        needed_way_ids: set[int],
        route_relation_ids: set[int],
        substation_relation_ids: set[int],
    ) -> None:
        super().__init__()
        self._needed_way_ids = needed_way_ids
        self._route_relation_ids = route_relation_ids
        self._substation_relation_ids = substation_relation_ids
        self.substation_nodes: list[dict[str, Any]] = []
        self.ways_by_feature: dict[str, list[dict[str, Any]]] = {
            feature: [] for feature in _WAY_FEATURES
        }
        self._way_geometries: dict[int, list[dict[str, float]]] = {}
        self.route_relations: list[dict[str, Any]] = []
        self.substation_relations: list[dict[str, Any]] = []

    def node(self, n: Any) -> None:
        """Record a substation node with valid coordinates."""
        tags = dict(n.tags)
        if _classify_feature(tags) != "substation":
            return
        if not n.location.valid():
            return
        self.substation_nodes.append(
            {
                "type": "node",
                "id": n.id,
                "tags": tags,
                "geometry": [{"lon": n.location.lon, "lat": n.location.lat}],
            }
        )

    def way(self, w: Any) -> None:
        """Resolve geometry for a power-feature way or a needed relation member; store both as applicable."""
        feature = _classify_feature(dict(w.tags))
        is_needed_member = w.id in self._needed_way_ids
        if feature is None and not is_needed_member:
            return
        geometry = [
            {"lon": node.lon, "lat": node.lat}
            for node in w.nodes
            if node.location.valid()
        ]
        if is_needed_member:
            self._way_geometries[w.id] = geometry
        if feature is not None:
            self.ways_by_feature[feature].append(
                {"type": "way", "id": w.id, "tags": dict(w.tags), "geometry": geometry}
            )

    def relation(self, r: Any) -> None:
        """Attach resolved member-way geometry to a matched relation and record it."""
        if (
            r.id not in self._route_relation_ids
            and r.id not in self._substation_relation_ids
        ):
            return
        members = []
        for member in r.members:
            entry = {
                "type": {"n": "node", "w": "way", "r": "relation"}[member.type],
                "ref": member.ref,
                "role": member.role,
            }
            if member.type == "w" and member.ref in self._way_geometries:
                entry["geometry"] = self._way_geometries[member.ref]
            members.append(entry)
        element = {
            "type": "relation",
            "id": r.id,
            "tags": dict(r.tags),
            "members": members,
        }
        if r.id in self._route_relation_ids:
            self.route_relations.append(element)
        if r.id in self._substation_relation_ids:
            self.substation_relations.append(element)


# Always the full set regardless of include_relations: route-relation tags
# (route=power, power=circuit, ...) cost nothing extra to keep in the
# filtered file, and power=construction is ambiguous between route and
# substation relations, so excluding it here would risk losing a genuine
# substation-under-construction. Whether a route relation actually gets
# used is still decided in Python by _RelationScanner's own flag.
_FILTER_EXPRESSIONS = (
    "w/power=line",
    "w/power=cable",
    "w/power=substation",
    "w/construction:power=line",
    "w/construction:power=cable",
    "w/construction:power=substation",
    "w/power=construction",
    "n/power=substation",
    "n/construction:power=substation",
    "r/route=power",
    "r/power=circuit",
    "r/power=substation",
    "r/construction:power=line",
    "r/construction:power=cable",
    "r/construction:power=substation",
    "r/power=construction",
)


def _filter_pbf(pbf_path: str, output_path: str) -> None:
    """Shrink a PBF to power-tagged elements (and their referenced members).

    ``osmium tags-filter`` includes referenced objects (a matched way's
    nodes, a matched relation's members) by default — no extra flag needed —
    which is exactly the geometry pass 2 below needs to resolve.
    """
    try:
        subprocess.run(
            [
                "osmium",
                "tags-filter",
                pbf_path,
                *_FILTER_EXPRESSIONS,
                "-o",
                output_path,
                "--overwrite",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"osmium tags-filter failed: {error.stderr}") from error


def retrieve_from_pbf(
    pbf_path: str, include_relations: bool
) -> dict[str, dict[str, Any]]:
    """Extract all power features for a country from a local PBF file.

    Returns a mapping of output name (``lines_way``, ``cables_way``,
    ``substations_way``, ``substations_node``, ``substations_relation``,
    ``routes_relation``) to raw Overpass-JSON-shaped payloads.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        filtered_path = str(Path(tmp_dir) / "filtered.osm.pbf")
        logger.info("Filtering %s to power-tagged elements only", pbf_path)
        _filter_pbf(pbf_path, filtered_path)
        return _extract_from_filtered_pbf(filtered_path, include_relations)


def _extract_from_filtered_pbf(
    pbf_path: str, include_relations: bool
) -> dict[str, dict[str, Any]]:
    """Run both pyosmium passes over an already-filtered PBF and assemble the per-feature payloads."""
    scanner = _RelationScanner(include_route_relations=include_relations)
    scanner.apply_file(pbf_path)
    logger.info(
        "Pass 1: %d route relations, %d substation relations, %d way ids needed",
        len(scanner.route_relations),
        len(scanner.substation_relations),
        len(scanner.needed_way_ids),
    )

    resolver = _FeatureResolver(
        needed_way_ids=scanner.needed_way_ids,
        route_relation_ids={r["id"] for r in scanner.route_relations},
        substation_relation_ids={r["id"] for r in scanner.substation_relations},
    )
    # A sparse in-memory index (not "dense", which allocates by max node ID
    # rather than actual node count) is fast; way geometry is only stored
    # for the small "needed" subset above (not every way in the country),
    # which is what keeps this safe rather than the index type itself.
    resolver.apply_file(pbf_path, locations=True, idx="sparse_mem_array")
    logger.info(
        "Pass 2: %d substation nodes, %d lines, %d cables, %d substation ways",
        len(resolver.substation_nodes),
        len(resolver.ways_by_feature["line"]),
        len(resolver.ways_by_feature["cable"]),
        len(resolver.ways_by_feature["substation"]),
    )

    # Always written, even empty when include_relations is off: this keeps
    # every retrieval rule producing the same fixed six files per country,
    # so whether a routes_relation.json is actually read is decided in one
    # place (clean_osm_data's rule input), not duplicated into every writer.
    return {
        "lines_way": {"elements": resolver.ways_by_feature["line"]},
        "cables_way": {"elements": resolver.ways_by_feature["cable"]},
        "substations_way": {"elements": resolver.ways_by_feature["substation"]},
        "substations_node": {"elements": resolver.substation_nodes},
        "substations_relation": {"elements": resolver.substation_relations},
        "routes_relation": {"elements": resolver.route_relations},
    }


if __name__ == "__main__":
    if "snakemake" not in globals():
        from scripts._helpers import mock_snakemake

        snakemake = mock_snakemake("retrieve_osm_pbf", country="BE")

    configure_logging(snakemake.log[0])
    logger.info("Python executable: %s", sys.executable)

    country = snakemake.wildcards.country
    include_relations = snakemake.params.include_relations
    force_redownload = snakemake.params.force_redownload
    data_dir = snakemake.params.data_dir

    try:
        region = get_region_tuple(country)
        pbf_path = download_region_pbf(
            region, update=force_redownload, data_dir=data_dir
        )
        payloads = retrieve_from_pbf(pbf_path, include_relations)

        outputs: dict[str, str] = dict(snakemake.output.items())
        for name, payload in payloads.items():
            output_path = outputs[name]
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w") as handle:
                json.dump(payload, handle)
            logger.info(
                "Wrote %d elements to %s", len(payload["elements"]), output_path
            )
    except Exception:
        logger.exception("PBF retrieval failed for %s", country)
        raise
