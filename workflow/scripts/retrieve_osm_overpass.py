# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT

"""Retrieve OSM power features for one country from the Overpass API.

Used for ``retrieve.source: overpass``. Runs six queries per country
(``lines_way``, ``cables_way``, ``routes_relation``, ``substations_way``,
``substations_relation``, ``substations_node``), each with ``out body
geom;`` so tags and member geometry come back inline in one response. The
Overpass endpoint/timeout/retries/user-agent are read from
``retrieve.overpass_api`` config, and retries use ``urllib3``'s
backoff/retry machinery. ``substations_node`` exists for parity with the
geofabrik/pyosmium retrieval path (retrieve_osm_pbf.py), which also sees
plain-node substations.

Output is raw Overpass JSON (``{"elements": [...]}``) — the same shape
retrieve_osm_pbf.py produces from a local PBF file, so clean.py's
importers don't need to know which source produced their input.
"""

import json
import logging
import sys
from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING, Any

import requests
from earth_osm.regions import get_region_tuple
from requests.adapters import HTTPAdapter
from scripts._helpers import configure_logging
from urllib3.util.retry import Retry

if TYPE_CHECKING:
    snakemake: Any

logger = logging.getLogger(__name__)

# Overpass filters per feature; substations_node is this project's own
# addition, for parity with the geofabrik/pyosmium path (see module docstring).
_FEATURE_FILTERS: dict[str, tuple[str, ...]] = {
    "cables_way": (
        'way["power"="cable"]',
        'way["construction:power"="cable"]',
        'way["power"="construction"]["construction"="cable"]',
    ),
    "lines_way": (
        'way["power"="line"]',
        'way["construction:power"="line"]',
        'way["power"="construction"]["construction"="line"]',
    ),
    "routes_relation": (
        'relation["route"="power"]',
        'relation["power"="circuit"]',
        'relation["construction:power"="line"]',
        'relation["construction:power"="cable"]',
        'relation["power"="construction"]',
    ),
    "substations_way": (
        'way["power"="substation"]',
        'way["construction:power"="substation"]',
        'way["power"="construction"]["construction"="substation"]',
    ),
    "substations_relation": (
        'relation["power"="substation"]',
        'relation["construction:power"="substation"]',
        'relation["power"="construction"]["construction"="substation"]',
    ),
    "substations_node": (
        'node["power"="substation"]',
        'node["construction:power"="substation"]',
        'node["power"="construction"]["construction"="substation"]',
    ),
}


def _build_query(iso_code: str, feature: str, timeout: int) -> str:
    """Build an Overpass QL query for one feature, scoped to the country's ISO3166-1 area."""
    filters = " ".join(
        f"{filter_}(area.searchArea);" for filter_ in _FEATURE_FILTERS[feature]
    )
    return dedent(
        f"""
        [out:json][timeout:{timeout}];
        area["ISO3166-1"="{iso_code}"]->.searchArea;
        (
            {filters}
        );
        out body geom;
        """
    ).strip()


def _normalise_node_geometry(payload: dict[str, Any]) -> dict[str, Any]:
    """Give plain nodes the same ``geometry: [{lat, lon}]`` shape ways use.

    Overpass's own JSON puts a node's location directly on ``lat``/``lon``
    (there's no member/way to have a "geometry" list of), but
    retrieve_osm_pbf.py normalises nodes to the same list-of-points shape as
    everything else, so clean.py can treat all three element types
    uniformly regardless of source.
    """
    elements = []
    for element in payload.get("elements", []):
        if element.get("type") == "node" and "lat" in element and "lon" in element:
            element = {
                **element,
                "geometry": [{"lat": element["lat"], "lon": element["lon"]}],
            }
        elements.append(element)
    return {**payload, "elements": elements}


def _session(max_tries: int, user_agent: str) -> requests.Session:
    """Build a ``requests`` session that retries transient errors with backoff."""
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})
    retry = Retry(
        total=max_tries,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=False,  # type: ignore[arg-type]  # retry POST too; urllib3 stubs miss this value
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def retrieve_from_overpass(
    country: str,
    include_relations: bool,
    url: str,
    max_tries: int,
    timeout: int,
    user_agent: str,
) -> dict[str, dict[str, Any]]:
    """Fetch all power features for a country from the Overpass API.

    Always returns all six feature names, even when include_relations is
    off — routes_relation just maps to an empty payload then, skipping the
    query itself (no point spending an API call on a result that won't be
    used) while still keeping every retrieval script's output set uniform.
    """
    iso_code = get_region_tuple(country).short
    features = list(_FEATURE_FILTERS)
    if not include_relations:
        features.remove("routes_relation")

    session = _session(max_tries, user_agent)
    payloads: dict[str, dict[str, Any]] = {"routes_relation": {"elements": []}}
    for feature in features:
        logger.info("Querying Overpass for %s in %s", feature, iso_code)
        response = session.post(
            url, data=_build_query(iso_code, feature, timeout), timeout=timeout
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        if feature == "substations_node":
            payload = _normalise_node_geometry(payload)
        payloads[feature] = payload
        logger.info(
            "Retrieved %d elements for %s in %s",
            len(payload.get("elements", [])),
            feature,
            iso_code,
        )
    return payloads


if __name__ == "__main__":
    if "snakemake" not in globals():
        from scripts._helpers import mock_snakemake

        snakemake = mock_snakemake("retrieve_osm_overpass", country="BE")

    configure_logging(snakemake.log[0])
    logger.info("Python executable: %s", sys.executable)

    country = snakemake.wildcards.country
    include_relations = snakemake.params.include_relations
    overpass_api = snakemake.params.overpass_api
    user_agent_cfg = overpass_api["user_agent"]
    user_agent = (
        f"{user_agent_cfg['project_name']} "
        f"(Contact: {user_agent_cfg['email']}; Website: {user_agent_cfg['website']})"
    )

    try:
        payloads = retrieve_from_overpass(
            country,
            include_relations,
            url=overpass_api["url"],
            max_tries=overpass_api["max_tries"],
            timeout=overpass_api["timeout"],
            user_agent=user_agent,
        )

        outputs: dict[str, str] = dict(snakemake.output.items())
        for name, payload in payloads.items():
            output_path = outputs[name]
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w") as handle:
                json.dump(payload, handle)
    except Exception:
        logger.exception("Overpass retrieval failed for %s", country)
        raise
