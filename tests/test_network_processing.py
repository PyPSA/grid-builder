"""Focused tests for generic OSM cleaning and topology construction."""

import json

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString

from workflow.scripts.build_network import build_network
from workflow.scripts.clean import (
    _apply_corrections,
    _filter_by_voltage,
    _region_ac_hz,
    clean,
)


def _write(path, elements):
    path.write_text(json.dumps({"elements": elements}))


def _ring(lon0, lat0, lon1, lat1):
    return [
        {"lon": lon0, "lat": lat0},
        {"lon": lon1, "lat": lat0},
        {"lon": lon1, "lat": lat1},
        {"lon": lon0, "lat": lat1},
        {"lon": lon0, "lat": lat0},
    ]


_NETWORK = {"minimum_voltage_kv": 220, "frequency_hz": {"AC": 50.0, "DC": 0.0}}
_GEO_CRS = "EPSG:4326"
_DISTANCE_CRS = "EPSG:3035"


def test_region_ac_hz_handles_null_frequency_override():
    """A country with no override still resolves.

    Its ``regions`` entry carries an explicit ``frequency_hz: None`` key
    (not a missing key) — the real shape
    ``RegionalNetworkConfig.model_dump()`` produces for any country without
    a regional frequency override.
    """
    regions = {"BE": {"minimum_voltage_kv": None, "frequency_hz": None}}
    assert _region_ac_hz("BE", _NETWORK, regions) == "50"

    regions_us = {
        "US": {"minimum_voltage_kv": None, "frequency_hz": {"AC": 60.0, "DC": None}}
    }
    assert _region_ac_hz("US", _NETWORK, regions_us) == "60"


def test_apply_corrections_exact_matches_whole_value_only():
    """An ``exact`` step only fires on a value equal to the pattern, unlike ``replace``.

    A freeform voltage tag containing an unrelated "m" (e.g. "amperes")
    must survive untouched, where a substring "replace" would corrupt it.
    """
    column = pd.Series(["m", "amperes", "medium"])
    steps = [{"exact": ["m", "33000"]}, {"exact": ["medium", "99000"]}]
    result = _apply_corrections(column, steps)
    assert list(result) == ["33000", "amperes", "99000"]


def test_filter_by_voltage_drops_oversized_garbage_without_crashing():
    """A voltage tag that cleans up into an implausibly long digit string.

    (e.g. freeform text misusing the voltage key) is dropped as noise
    instead of overflowing ``astype(int)``'s fixed-width C long.
    """
    df = pd.DataFrame({"voltage": ["230000", "9" * 15]})
    filtered, list_voltages = _filter_by_voltage(df, min_voltage=220000)
    assert list(list_voltages) == ["230000"]
    assert list(filtered["voltage"]) == ["230000"]


def test_cleaner_removes_line_in_overlapping_substation_polygons(tmp_path):
    """Containment filtering remains index-safe when polygons overlap."""
    square = _ring(4.0, 50.0, 4.1, 50.1)
    substations_path = tmp_path / "BE_substations_way.json"
    _write(
        substations_path,
        [
            {
                "type": "way",
                "id": 1,
                "tags": {"power": "substation", "voltage": "220000"},
                "geometry": square,
            },
            {
                "type": "way",
                "id": 2,
                "tags": {"power": "substation", "voltage": "220000"},
                "geometry": square,
            },
        ],
    )

    lines_path = tmp_path / "BE_lines_way.json"
    _write(
        lines_path,
        [
            {
                "type": "way",
                "id": 3,
                "tags": {"power": "line", "voltage": "220000"},
                "geometry": [{"lon": 4.02, "lat": 50.02}, {"lon": 4.08, "lat": 50.08}],
            }
        ],
    )

    inputs = {
        "substations_way": [str(substations_path)],
        "lines_way": [str(lines_path)],
    }
    buses, polygons, lines = clean(inputs, _NETWORK, {}, _GEO_CRS)

    assert set(buses["bus_id"]) == {"way/1", "way/2"}
    assert len(polygons) == 2
    assert lines.empty


def test_cleaner_groups_relation_member_ways_into_one_line(tmp_path):
    """A route=power relation collapses its member ways into one circuit."""

    def _member(ref: int, lon0: float, lon1: float) -> dict:
        return {
            "type": "way",
            "ref": ref,
            "role": "line",
            "geometry": [{"lat": 50.0, "lon": lon0}, {"lat": 50.0, "lon": lon1}],
        }

    lines_path = tmp_path / "BE_lines_way.json"
    _write(
        lines_path,
        [
            {
                "type": "way",
                "id": 10,
                "tags": {"power": "line", "voltage": "380000"},
                "geometry": [{"lon": 4.0, "lat": 50.0}, {"lon": 4.1, "lat": 50.0}],
            },
            {
                "type": "way",
                "id": 11,
                "tags": {"power": "line", "voltage": "380000"},
                "geometry": [{"lon": 4.1, "lat": 50.0}, {"lon": 4.2, "lat": 50.0}],
            },
        ],
    )

    relations_path = tmp_path / "BE_routes_relation.json"
    _write(
        relations_path,
        [
            {
                "type": "relation",
                "id": 99,
                "tags": {"route": "power", "voltage": "380000", "cables": "3"},
                "members": [_member(10, 4.0, 4.1), _member(11, 4.1, 4.2)],
            }
        ],
    )

    inputs = {"lines_way": [str(lines_path)], "routes_relation": [str(relations_path)]}
    _, _, lines = clean(inputs, _NETWORK, {}, _GEO_CRS)

    assert len(lines) == 1
    assert lines.iloc[0]["line_id"] == "relation/99"
    assert lines.iloc[0].geometry.length > 0


def test_builder_merges_compatible_segments_through_virtual_bus(monkeypatch):
    """A degree-two endpoint that isn't a real substation does not remain a bus."""
    substations = gpd.GeoDataFrame(
        {
            "bus_id": pd.Series(dtype=str),
            "voltage": pd.Series(dtype=int),
            "country": pd.Series(dtype=str),
            "under_construction": pd.Series(dtype=bool),
            "start_date": pd.Series(dtype="datetime64[ns]"),
            "contains": pd.Series(dtype=str),
        },
        geometry=gpd.GeoSeries([], crs="EPSG:4326"),
        crs="EPSG:4326",
    )
    substations_polygon = gpd.GeoDataFrame(
        {"bus_id": pd.Series(dtype=str)},
        geometry=gpd.GeoSeries([], crs="EPSG:4326"),
        crs="EPSG:4326",
    )
    lines = gpd.GeoDataFrame(
        [
            {
                "line_id": "way/1-220",
                "circuits": 1,
                "voltage": 220000,
                "underground": False,
                "under_construction": False,
                "start_date": pd.NaT,
                "contains": ["way/1"],
                "geometry": LineString([(4.0, 50.0), (4.1, 50.0)]),
            },
            {
                "line_id": "way/2-220",
                "circuits": 1,
                "voltage": 220000,
                "underground": False,
                "under_construction": False,
                "start_date": pd.NaT,
                "contains": ["way/2"],
                "geometry": LineString([(4.1, 50.0), (4.2, 50.0)]),
            },
        ],
        crs="EPSG:4326",
    )

    station_seeds = build_network.__globals__["_create_station_seeds"]
    captured = {}

    def capture_station_merge_radius(*args, **kwargs):
        captured["tol"] = kwargs["tol"]
        return station_seeds(*args, **kwargs)

    monkeypatch.setitem(
        build_network.__globals__, "_create_station_seeds", capture_station_merge_radius
    )

    buses, built_lines, transformers, stations_polygon, buses_polygon = build_network(
        substations,
        substations_polygon,
        lines,
        False,
        None,
        _GEO_CRS,
        _DISTANCE_CRS,
        station_merge_radius_m=1,
    )

    assert len(buses) == 2
    assert len(built_lines) == 1
    assert len(transformers) == 0
    assert captured["tol"] == 1
    assert list(stations_polygon.columns) == ["station_id", "geometry"]
    assert len(stations_polygon) == 2
    assert list(buses_polygon.columns) == ["bus_id", "geometry"]
    assert buses_polygon.empty
