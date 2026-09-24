# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT

"""Retrieve OSM route=power relations for a single country.

earth-osm's exporters (both the Geofabrik streaming backend and its own
Overpass client) parse OSM relations internally but never convert them into
output rows, since power lines are otherwise fully described by nodes and
ways. Route relations are the only way to recover which individual line ways
form one continuous, real-world circuit, so they are fetched directly here,
matching whichever ``retrieve.source`` the main retrieval uses:

- ``geofabrik``: read relations straight out of the same ``.osm.pbf`` file
  earth-osm already downloaded and cached, via a memory-safe two-pass
  pyosmium read (pass 1 finds matching relations and the way IDs they
  reference; pass 2 resolves geometry only for those specific ways, not
  every way in the country). No extra network call, and guaranteed to be
  the same OSM snapshot as the substations/lines/cables data.
- ``overpass``: query the Overpass API directly, since earth-osm's own
  Overpass client has the same relation gap as its Geofabrik backend.

Either way, the output is raw ``{"elements": [...]}`` JSON in Overpass's own
shape (relation id, tags, members with embedded way geometry), matching what
PyPSA-Eur's ``_import_routes_relation`` expects — clean_osm_data.py ports
that function directly, so relation geometry is assembled identically
regardless of which path produced this file.
"""

import json
import logging
import sys
from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING, Any

import osmium
import requests
from earth_osm.regions import download_region_pbf, get_region_tuple
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

if TYPE_CHECKING:
    snakemake: Any

logger = logging.getLogger(__name__)

OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
REQUEST_TIMEOUT = 600
QUERY_TIMEOUT = 300

# Matches PyPSA-Eur's routes_relation query (scripts/retrieve_osm_data.py):
# route=power is the common convention, but power=circuit is a distinct,
# also-used tagging scheme, and the construction:power/power=construction
# variants catch relations for lines still being built.
_RELATION_TAG_FILTERS = (
    ("route", "power"),
    ("power", "circuit"),
    ("construction:power", "line"),
    ("construction:power", "cable"),
    ("power", "construction"),
)


def configure_logging(log_path: str) -> None:
    """Send rule and dependency logging to the Snakemake log file."""
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# source: geofabrik -- read relations from the already-cached PBF via pyosmium
# ---------------------------------------------------------------------------


def _matches_relation_tags(tags: dict[str, str]) -> bool:
    return any(tags.get(key) == value for key, value in _RELATION_TAG_FILTERS)


class _RelationScanner(osmium.SimpleHandler):  # type: ignore[misc]
    """Pass 1: find matching relations and the way IDs they reference.

    Deliberately does not touch node/way geometry, so this pass is cheap
    regardless of country size.
    """

    def __init__(self) -> None:
        super().__init__()
        self.relations: list[dict[str, Any]] = []
        self.needed_way_ids: set[int] = set()

    def relation(self, r: Any) -> None:
        tags = dict(r.tags)
        if not _matches_relation_tags(tags):
            return
        members = [
            {
                "type": {"n": "node", "w": "way", "r": "relation"}[member.type],
                "ref": member.ref,
                "role": member.role,
            }
            for member in r.members
        ]
        self.relations.append(
            {"type": "relation", "id": r.id, "tags": tags, "members": members}
        )
        self.needed_way_ids.update(
            member.ref for member in r.members if member.type == "w"
        )


class _WayGeometryResolver(osmium.SimpleHandler):  # type: ignore[misc]
    """Pass 2: resolve geometry only for the specific way IDs pass 1 needs.

    Storing every way in a country extract (there can be millions, for
    roads/buildings/etc. unrelated to power infrastructure) is what makes a
    naive single-pass read memory-hungry; filtering to the few thousand
    power-line ways keeps this proportional to the actual power network.
    """

    def __init__(self, needed_way_ids: set[int]) -> None:
        super().__init__()
        self._needed_way_ids = needed_way_ids
        self.way_geometries: dict[int, list[dict[str, float]]] = {}

    def way(self, w: Any) -> None:
        if w.id not in self._needed_way_ids:
            return
        self.way_geometries[w.id] = [
            {"lon": node.lon, "lat": node.lat}
            for node in w.nodes
            if node.location.valid()
        ]


def retrieve_relations_from_pbf(pbf_path: str) -> dict[str, Any]:
    """Extract power route relations from a local PBF file via pyosmium."""
    scanner = _RelationScanner()
    scanner.apply_file(pbf_path)
    logger.info(
        "Found %d matching relations referencing %d ways in %s",
        len(scanner.relations),
        len(scanner.needed_way_ids),
        pbf_path,
    )

    # A disk-backed (memory-mapped) location index bounds memory to roughly
    # the OS page cache rather than the whole country's node count, which
    # can otherwise reach the tens of millions for a Geofabrik extract.
    resolver = _WayGeometryResolver(scanner.needed_way_ids)
    resolver.apply_file(pbf_path, locations=True, idx="sparse_file_array")
    logger.info(
        "Resolved geometry for %d of %d needed ways",
        len(resolver.way_geometries),
        len(scanner.needed_way_ids),
    )

    for relation in scanner.relations:
        for member in relation["members"]:
            if member["type"] == "way" and member["ref"] in resolver.way_geometries:
                member["geometry"] = resolver.way_geometries[member["ref"]]

    return {"elements": scanner.relations}


# ---------------------------------------------------------------------------
# source: overpass -- query the Overpass API directly
# ---------------------------------------------------------------------------


def _build_overpass_query(iso_code: str) -> str:
    filters = " ".join(
        f"{key_value}(area.searchArea);"
        for key_value in (
            f'relation["{key}"="{value}"]' for key, value in _RELATION_TAG_FILTERS
        )
    )
    return dedent(
        f"""
        [out:json][timeout:{QUERY_TIMEOUT}];
        area["ISO3166-1"="{iso_code}"]->.searchArea;
        (
            {filters}
        );
        out body geom;
        """
    ).strip()


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {"User-Agent": "grid-builder/0.1 (+https://github.com/pypsa/grid-builder)"}
    )
    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=False,  # type: ignore[arg-type]  # retry POST too; urllib3 stubs miss this value
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def retrieve_relations_from_overpass(iso_code: str) -> dict[str, Any]:
    """Fetch power route relations for a country as raw Overpass JSON."""
    logger.info("Querying Overpass for power route relations in %s", iso_code)
    response = _session().post(
        OVERPASS_ENDPOINT, data=_build_overpass_query(iso_code), timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    return payload


# ---------------------------------------------------------------------------


def retrieve_relations(
    country: str, source: str, data_dir: str, force_redownload: bool = False
) -> dict[str, Any]:
    """Retrieve power route relations for ``country`` via ``source``."""
    region = get_region_tuple(country)
    if source == "geofabrik":
        pbf_path = download_region_pbf(
            region, update=force_redownload, data_dir=data_dir
        )
        return retrieve_relations_from_pbf(pbf_path)
    if source == "overpass":
        return retrieve_relations_from_overpass(region.short)
    raise ValueError(f"Unknown retrieve.source: {source!r}")


if __name__ == "__main__":
    if "snakemake" not in globals():
        from workflow.scripts._helpers import mock_snakemake

        snakemake = mock_snakemake("retrieve_osm_relations", country="BE")

    configure_logging(snakemake.log[0])
    logger.info("Python executable: %s", sys.executable)

    country = snakemake.wildcards.country
    source = snakemake.params.source
    project_root = Path(__file__).resolve().parents[2]
    data_dir = str(project_root / "data" / "earth-osm")

    try:
        payload = retrieve_relations(
            country,
            source,
            data_dir,
            force_redownload=snakemake.params.force_redownload,
        )
        elements = [
            e for e in payload.get("elements", []) if e.get("type") == "relation"
        ]
        Path(snakemake.output.json).parent.mkdir(parents=True, exist_ok=True)
        with open(snakemake.output.json, "w") as handle:
            json.dump(payload, handle)
        logger.info(
            "Retrieved %d power route relations for %s via %s",
            len(elements),
            country,
            source,
        )
    except Exception:
        logger.exception("OSM relation retrieval failed for %s", country)
        raise
