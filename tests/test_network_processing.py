"""Focused tests for generic OSM cleaning and topology construction."""

import json
import logging

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString

from workflow.scripts.build_network import build_network
from workflow.scripts.clean import (
    _apply_corrections,
    _clean_cables,
    _clean_circuits,
    _clean_lines,
    _clean_rating,
    _clean_voltage,
    _create_single_link,
    _filter_by_voltage,
    _region_ac_hz,
    _region_min_voltage,
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


_NETWORK = {
    "minimum_voltage_kv": 220,
    "minimum_voltage_dc_kv": 150,
    "frequency_hz": {"AC": 50.0, "DC": 0.0},
    "accepted_ac_frequencies_hz": [50.0, 60.0],
    "frequency_tolerance_hz": 0.1,
    "dc_lines": "keep",
    "station_merge_radius_m": 500.0,
}
_FREQUENCY = {"accepted_ac_hz": [50.0, 60.0], "tolerance_hz": 0.1}
# build_network's config-driven settings that have no default.
_BUILD = {"station_bus_offset_m": 15.0, "overpassing_lines_tolerance_m": 1.0}
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
                "country": "BE",
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
                "country": "BE",
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

    (buses, built_lines, transformers, _converters, stations_polygon, buses_polygon) = (
        build_network(
            substations,
            substations_polygon,
            lines,
            False,
            None,
            _GEO_CRS,
            _DISTANCE_CRS,
            station_merge_radius_m=1,
            **_BUILD,
        )
    )

    assert len(buses) == 2
    assert len(built_lines) == 1
    assert len(transformers) == 0
    assert captured["tol"] == 1
    assert list(stations_polygon.columns) == ["station_id", "geometry"]
    assert len(stations_polygon) == 2
    assert list(buses_polygon.columns) == ["bus_id", "geometry"]
    assert buses_polygon.empty


def test_clean_keeps_country_on_lines():
    """Lines carry their country out of cleaning, as substations already did.

    A consumer resolves line types per country, so dropping it here would
    leave no way to recover it downstream.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        lines_path = tmp_path / "BE_lines_way.json"
        _write(
            lines_path,
            [
                {
                    "type": "way",
                    "id": 10,
                    "tags": {"power": "line", "voltage": "380000", "circuits": "1"},
                    "geometry": [{"lon": 4.0, "lat": 50.0}, {"lon": 4.1, "lat": 50.0}],
                }
            ],
        )
        _, _, lines = clean({"lines_way": [str(lines_path)]}, _NETWORK, {}, _GEO_CRS)

    assert "country" in lines.columns
    assert lines.iloc[0]["country"] == "BE"


def test_builder_carries_provenance_columns_to_output():
    """country/under_construction/start_date reach the built components.

    Without them a consumer cannot resolve line types, and
    ``remove_under_construction: false`` produces output in which a planned
    asset is indistinguishable from a commissioned one.
    """
    substations = gpd.GeoDataFrame(
        {
            "bus_id": pd.Series(dtype=str),
            "voltage": pd.Series(dtype=int),
            "country": pd.Series(dtype=str),
            "under_construction": pd.Series(dtype=bool),
            "start_date": pd.Series(dtype="datetime64[ns]"),
            "contains": pd.Series(dtype=str),
        },
        geometry=gpd.GeoSeries([], crs=_GEO_CRS),
        crs=_GEO_CRS,
    )
    substations_polygon = gpd.GeoDataFrame(
        {"bus_id": pd.Series(dtype=str)},
        geometry=gpd.GeoSeries([], crs=_GEO_CRS),
        crs=_GEO_CRS,
    )
    lines = gpd.GeoDataFrame(
        [
            {
                "line_id": "way/1-380",
                "circuits": 1,
                "voltage": 380000,
                "country": "BE;NL",
                "underground": False,
                "under_construction": True,
                "start_date": pd.Timestamp("2030-01-01"),
                "contains": ["way/1"],
                "geometry": LineString([(4.0, 50.0), (4.3, 50.0)]),
            }
        ],
        crs=_GEO_CRS,
    )

    buses, built_lines, _, _, _, _ = build_network(
        substations,
        substations_polygon,
        lines,
        remove_under_construction=False,
        remove_after=None,
        geo_crs=_GEO_CRS,
        distance_crs=_DISTANCE_CRS,
        station_merge_radius_m=1,
        **_BUILD,
    )

    for frame in (buses, built_lines):
        for column in ("country", "under_construction", "start_date"):
            assert column in frame.columns

    assert built_lines.iloc[0]["under_construction"]
    assert built_lines.iloc[0]["country"] == "BE;NL"
    # Virtual buses are created from the line, so they inherit its country
    # rather than arriving with a null a consumer would later drop.
    assert set(buses["country"]) == {"BE;NL"}
    assert buses["under_construction"].all()


def test_region_lookups_resolve_cross_border_country_values():
    """A ";"-joined country still resolves its regional overrides.

    ``_drop_duplicate_lines`` merges the codes of an element seen by two
    countries, and looking that value up as a single key used to miss every
    override and fall back to the global defaults. For a Brazilian
    cross-border line that silently meant a 220 kV threshold instead of 60,
    deleting a real interconnector, and 50 Hz instead of 60.
    """
    regions = {
        "BR": {"minimum_voltage_kv": 60.0, "frequency_hz": {"AC": 60.0, "DC": None}},
        "PY": {"minimum_voltage_kv": 60.0, "frequency_hz": {"AC": 60.0, "DC": None}},
    }

    assert _region_min_voltage("BR;PY", _NETWORK, regions) == 60_000
    assert _region_ac_hz("BR;PY", _NETWORK, regions) == "60"

    # Single-country values behave exactly as before.
    assert _region_min_voltage("BR", _NETWORK, regions) == 60_000
    assert _region_min_voltage("BE", _NETWORK, regions) == 220_000


def test_region_min_voltage_takes_the_most_permissive_side():
    """An interconnector survives if either country would keep it."""
    regions = {"BR": {"minimum_voltage_kv": 60.0, "frequency_hz": None}}
    assert _region_min_voltage("BR;BE", _NETWORK, regions) == 60_000


def test_clean_merges_custom_elements_with_retrieved_ones(tmp_path):
    """A custom raw file is cleaned exactly like a retrieved one.

    Both retrieval backends already write the same shape, and the loader
    takes a list of paths per feature while reading the country back out of
    each filename, so custom data needs no special handling here.
    """
    retrieved = tmp_path / "BE_lines_way.json"
    _write(
        retrieved,
        [
            {
                "type": "way",
                "id": 10,
                "tags": {"power": "line", "voltage": "380000", "circuits": "1"},
                "geometry": [{"lon": 4.0, "lat": 50.0}, {"lon": 4.1, "lat": 50.0}],
            }
        ],
    )
    custom = tmp_path / "custom" / "BE_lines_way.json"
    custom.parent.mkdir()
    _write(
        custom,
        [
            {
                "type": "way",
                "id": 11,
                "tags": {"power": "line", "voltage": "380000", "circuits": "1"},
                "geometry": [{"lon": 5.0, "lat": 51.0}, {"lon": 5.1, "lat": 51.0}],
            }
        ],
    )

    _, _, lines = clean(
        {"lines_way": [str(retrieved), str(custom)]}, _NETWORK, {}, _GEO_CRS
    )

    assert set(lines["line_id"]) == {"way/10", "way/11"}
    assert set(lines["country"]) == {"BE"}


def _circuits_for(cables=None, circuits=None, voltage="220000"):
    """Run one raw line through cleaning and return its total circuits."""
    frame = pd.DataFrame(
        [
            {
                "id": "way/1",
                "voltage": voltage,
                "cables": cables,
                "circuits": circuits,
                "frequency": "50",
                "_ac_hz": "50",
            }
        ]
    )
    frame["voltage"] = _clean_voltage(frame["voltage"])
    frame["cables"] = _clean_cables(frame["cables"])
    frame["circuits"] = _clean_circuits(frame["circuits"])
    cleaned = _clean_lines(frame, ["220000"], "0", **_FREQUENCY)
    return sum(int(value) for value in cleaned["circuits"])


@pytest.mark.parametrize(
    ("cables", "expected"),
    [("3+3", 2), ("6+1", 2), ("2x3", 2), ("3x2", 2), ("2x2", 1), ("2-1", 1)],
)
def test_arithmetic_cable_tags_are_evaluated_not_concatenated(cables, expected):
    """Stripping non-digits would read "2x3" as 23 cables instead of 6.

    These values are whole-value corrections precisely because the digit
    strip at the end of _clean_cables cannot know they encode arithmetic.
    """
    assert _circuits_for(cables=cables) == expected


@pytest.mark.parametrize(("circuits", "expected"), [("2/3", 2), ("2-1", 2)])
def test_range_circuit_tags_are_not_concatenated(circuits, expected):
    """Stripping non-digits would read "2/3" as 23 circuits instead of 2."""
    assert _circuits_for(circuits=circuits) == expected


@pytest.mark.parametrize("cables", ["ground", "1 disused"])
def test_lines_without_live_conductors_are_dropped(cables):
    """A ground wire or a retired cable is not a live single-circuit line."""
    assert _circuits_for(cables=cables) == 0


@pytest.mark.parametrize(
    ("cables", "expected"), [("triple", 1), ("single", 1), ("3;3 disused", 1)]
)
def test_word_and_partly_disused_cable_tags_keep_their_meaning(cables, expected):
    """Word forms and partly disused cables resolve to the live circuit count."""
    assert _circuits_for(cables=cables) == expected


def test_unmapped_corrupting_tag_values_are_reported(caplog):
    """The tables only grow if the workflow says what it could not map."""
    with caplog.at_level(logging.WARNING, logger="workflow.scripts.clean"):
        result = list(_clean_cables(pd.Series(["4x5", "4x5", "7+2"])))
    assert result == ["45", "45", "72"], "the strip still runs"
    assert "4x5" in caplog.text
    assert "7+2" in caplog.text


def test_harmless_and_mapped_tag_values_are_not_reported(caplog):
    """A trailing unit strips away cleanly, so warning about it is noise."""
    with caplog.at_level(logging.WARNING, logger="workflow.scripts.clean"):
        assert list(_clean_voltage(pd.Series(["220000 V"]))) == ["220000"]
        assert list(_clean_cables(pd.Series(["2x3"]))) == ["6"]
    assert caplog.text == ""


# --- Frequency and DC -------------------------------------------------------


def _raw_line(way_id, voltage, lon_lats, frequency=None, power="line"):
    tags = {"power": power, "voltage": voltage}
    if frequency is not None:
        tags["frequency"] = frequency
    return {
        "type": "way",
        "id": way_id,
        "tags": tags,
        "geometry": [{"lon": lon, "lat": lat} for lon, lat in lon_lats],
    }


def _converter_station_inputs(tmp_path):
    """A converter station fed by 380 kV and 220 kV AC lines and a 320 kV DC line."""
    substations_path = tmp_path / "BE_substations_way.json"
    _write(
        substations_path,
        [
            {
                "type": "way",
                "id": 1,
                "tags": {
                    "power": "substation",
                    "voltage": "380000;220000;320000",
                    "frequency": "50;50;0",
                },
                "geometry": _ring(3.999, 49.999, 4.001, 50.001),
            }
        ],
    )
    lines_path = tmp_path / "BE_lines_way.json"
    _write(
        lines_path,
        [
            _raw_line(10, "380000", [(3.9, 50.0), (4.0, 50.0)]),
            _raw_line(11, "220000", [(4.0, 49.9), (4.0, 50.0)]),
            _raw_line(12, "320000", [(4.1, 50.0), (4.0, 50.0)], frequency="0"),
        ],
    )
    return {"substations_way": [str(substations_path)], "lines_way": [str(lines_path)]}


def _build(buses, polygons, lines):
    return build_network(
        buses, polygons, lines, False, None, _GEO_CRS, _DISTANCE_CRS, **_BUILD
    )


def test_converter_station_gets_dc_bus_and_converter_not_transformer(tmp_path):
    """PyPSA-Earth's model: DC has its own bus, joined to AC only by a converter."""
    buses, polygons, lines = clean(
        _converter_station_inputs(tmp_path), _NETWORK, {}, _GEO_CRS
    )
    assert lines.set_index("voltage")["dc"].to_dict() == {
        380000: False,
        220000: False,
        320000: True,
    }

    built_buses, built_lines, transformers, converters, _, _ = _build(
        buses, polygons, lines
    )
    station = built_buses[built_buses["station_id"] == "way/1"]
    assert set(station["bus_id"]) == {"way/1-380", "way/1-220", "way/1-320-dc"}
    assert station.set_index("bus_id")["dc"].to_dict()["way/1-320-dc"]

    dc_line = built_lines[built_lines["dc"]]
    assert len(dc_line) == 1
    assert "way/1-320-dc" in set(dc_line[["bus0", "bus1"]].iloc[0])

    # Transformers join AC levels only.
    assert list(transformers["transformer_id"]) == ["way/1-380-220"]

    # The DC side converts to the AC level closest in voltage: 380 kV, not 220.
    assert len(converters) == 1
    converter = converters.iloc[0]
    assert (converter["bus0"], converter["bus1"]) == ("way/1-320-dc", "way/1-380")
    assert (converter["voltage_bus0_kv"], converter["voltage_bus1_kv"]) == (320, 380)


@pytest.mark.parametrize("mode", ["drop", "force_ac"])
def test_dc_lines_can_be_dropped_or_relabelled_as_ac(tmp_path, mode):
    """network.dc_lines drop removes DC lines; force_ac keeps them as AC."""
    network = {**_NETWORK, "dc_lines": mode}
    buses, polygons, lines = clean(
        _converter_station_inputs(tmp_path), network, {}, _GEO_CRS
    )
    assert not lines["dc"].any()
    assert not buses["dc"].any()
    assert (320000 in set(lines["voltage"])) == (mode == "force_ac")

    _, _, _, converters, _, _ = _build(buses, polygons, lines)
    assert converters.empty


def test_railway_traction_frequency_is_dropped_not_read_as_mains(tmp_path):
    """A 16.7 Hz traction line is a separate grid, not a 50 Hz line."""
    lines_path = tmp_path / "DE_lines_way.json"
    _write(
        lines_path,
        [
            _raw_line(1, "220000", [(10.0, 50.0), (10.1, 50.0)], frequency="16.7"),
            _raw_line(2, "220000", [(10.0, 51.0), (10.1, 51.0)], frequency="50"),
        ],
    )
    _, _, lines = clean({"lines_way": [str(lines_path)]}, _NETWORK, {}, _GEO_CRS)
    assert list(lines["line_id"]) == ["way/2"]


def test_frequency_list_is_paired_with_voltage_list(tmp_path):
    """voltage=380000;220000 frequency=50;16.7 is a mains circuit plus a railway one."""
    lines_path = tmp_path / "DE_lines_way.json"
    _write(
        lines_path,
        [
            _raw_line(
                1, "380000;220000", [(10.0, 50.0), (10.1, 50.0)], frequency="50;16.7"
            )
        ],
    )
    _, _, lines = clean({"lines_way": [str(lines_path)]}, _NETWORK, {}, _GEO_CRS)
    assert list(lines["voltage"]) == [380000]


@pytest.mark.parametrize(
    ("frequency", "expected_dc"), [("50.0", False), ("60", False), ("0.0", True)]
)
def test_frequency_matching_is_numeric_not_textual(tmp_path, frequency, expected_dc):
    """A tag of 50.0 is mains and 0.0 is DC, though neither equals its marker."""
    lines_path = tmp_path / "BE_lines_way.json"
    _write(
        lines_path,
        [_raw_line(1, "220000", [(4.0, 50.0), (4.1, 50.0)], frequency=frequency)],
    )
    _, _, lines = clean({"lines_way": [str(lines_path)]}, _NETWORK, {}, _GEO_CRS)
    assert list(lines["dc"]) == [expected_dc]


def test_empty_substation_input_keeps_the_output_schema(tmp_path):
    """build_network selects polygons by bus_id, so the columns must exist."""
    lines_path = tmp_path / "BE_lines_way.json"
    _write(lines_path, [_raw_line(1, "220000", [(4.0, 50.0), (4.1, 50.0)])])
    buses, polygons, lines = clean(
        {"lines_way": [str(lines_path)]}, _NETWORK, {}, _GEO_CRS
    )
    assert buses.empty
    assert {"bus_id", "dc"} <= set(buses.columns)
    assert "bus_id" in polygons.columns
    built_buses, built_lines, *_ = _build(buses, polygons, lines)
    assert len(built_lines) == 1
    assert not built_lines["dc"].any()


def test_dc_cables_count_two_per_circuit_on_split_lines(tmp_path):
    """A DC circuit has two conductors, so 4;4 cables mean two circuits each, not one."""
    lines_path = tmp_path / "BE_lines_way.json"
    _write(
        lines_path,
        [
            {
                **_raw_line(
                    1, "320000;320000", [(4.0, 50.0), (4.1, 50.0)], frequency="0"
                ),
                "tags": {
                    "power": "line",
                    "voltage": "320000;320000",
                    "frequency": "0",
                    "cables": "4;4",
                },
            }
        ],
    )
    _, _, lines = clean({"lines_way": [str(lines_path)]}, _NETWORK, {}, _GEO_CRS)
    assert list(lines["circuits"]) == [4]
    assert list(lines["dc"]) == [True]


# --- PyPSA-Eur's relation-based HVDC links ---------------------------------


def _member(way_id, lon_lats, role):
    return {
        "type": "way",
        "ref": way_id,
        "role": role,
        "geometry": [{"lon": lon, "lat": lat} for lon, lat in lon_lats],
    }


def _hvdc_relation(relation_id, members, voltage="500000", rating="1000 MW"):
    tags = {"type": "route", "route": "power", "voltage": voltage, "frequency": "0"}
    if rating is not None:
        tags["rating"] = rating
    return {"type": "relation", "id": relation_id, "tags": tags, "members": members}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1000 MW", 1000.0), ("1.2 GW", 1200.0), ("500;500", 1000.0), ("", None)],
)
def test_rating_parses_to_megawatts(raw, expected):
    """HVDC ratings sum across entries and scale GW to MW."""
    [value] = _clean_rating(pd.Series([raw])).tolist()
    assert (pd.isna(value) and expected is None) or value == expected


def test_single_link_collapses_parallel_poles_and_skips_terminals():
    """A bipole's two poles would never merge into one line, so AC merging drops it."""
    pole = [(20.0, -5.0), (21.0, -5.0)]
    row = pd.Series(
        {
            "members": [
                _member(1, pole, "line"),
                _member(2, [(20.0, -5.0), (20.5, -5.0001), (21.0, -5.0)], "line"),
                _member(
                    3,
                    [(19.99, -5.01), (20.01, -5.01), (20.01, -4.99), (19.99, -5.01)],
                    "substation",
                ),
            ]
        }
    )
    link, members = _create_single_link(row)
    assert link.geom_type == "LineString"
    assert set(members) == {"way/1", "way/2"}, "both poles are replaced by the link"


def _write_hvdc_case(tmp_path, converter_tag=True, gap_deg=0.03):
    """A 500 kV bipole into a converter hall standing apart from its 220 kV substation."""
    hall_tags = {"power": "substation", "voltage": "500000", "frequency": "0"}
    if converter_tag:
        hall_tags["substation"] = "converter"
    ac_lon = 21.0 + gap_deg
    _write(
        tmp_path / "CD_substations_way.json",
        [
            {
                "type": "way",
                "id": 1,
                "tags": hall_tags,
                "geometry": _ring(20.999, -5.001, 21.001, -4.999),
            },
            {
                "type": "way",
                "id": 2,
                "tags": {"power": "substation", "voltage": "220000"},
                "geometry": _ring(ac_lon - 0.001, -5.001, ac_lon + 0.001, -4.999),
            },
        ],
    )
    poles = [
        _member(10, [(19.0, -5.0), (21.0, -5.0)], "line"),
        _member(11, [(19.0, -5.0), (20.0, -5.0002), (21.0, -5.0)], "line"),
    ]
    _write(tmp_path / "CD_routes_relation.json", [_hvdc_relation(100, poles)])
    _write(
        tmp_path / "CD_lines_way.json",
        [
            _raw_line(10, "500000", [(19.0, -5.0), (21.0, -5.0)], frequency="0"),
            _raw_line(
                11,
                "500000",
                [(19.0, -5.0), (20.0, -5.0002), (21.0, -5.0)],
                frequency="0",
            ),
            _raw_line(20, "220000", [(ac_lon, -5.0), (ac_lon, -4.0)]),
        ],
    )
    return {
        "substations_way": [str(tmp_path / "CD_substations_way.json")],
        "routes_relation": [str(tmp_path / "CD_routes_relation.json")],
        "lines_way": [str(tmp_path / "CD_lines_way.json")],
    }


def test_hvdc_relation_becomes_one_rated_link_replacing_its_poles(tmp_path):
    """PyPSA-Eur's relationship concept: one rated link per HVDC relation."""
    _, _, lines = clean(_write_hvdc_case(tmp_path), _NETWORK, {}, _GEO_CRS)
    dc = lines[lines["dc"]]
    assert list(dc["line_id"]) == ["relation/100"]
    assert list(dc["p_nom_mw"]) == [1000.0]
    assert lines["p_nom_mw"].isna().sum() == len(lines) - 1, "AC lines carry no rating"


def test_converter_hall_apart_from_its_substation_is_wired_to_nearest_ac(tmp_path):
    """PyPSA-Eur's rule: a tagged converter hall 3 km from the AC yard still connects."""
    buses, polygons, lines = clean(_write_hvdc_case(tmp_path), _NETWORK, {}, _GEO_CRS)
    *_, converters, _, _ = build_network(
        buses,
        polygons,
        lines,
        False,
        None,
        _GEO_CRS,
        _DISTANCE_CRS,
        converter_search_radius_m=50000,
        **_BUILD,
    )
    assert len(converters) == 1
    converter = converters.iloc[0]
    assert converter["pairing"] == "nearest_station"
    assert converter["bus1"] == "way/2-220"
    assert converter["p_nom_mw"] == 1000.0


def test_untagged_dc_terminal_is_not_wired_to_nearby_ac(tmp_path):
    """Without the converter tag a DC end may be a border cut, so no guessing."""
    buses, polygons, lines = clean(
        _write_hvdc_case(tmp_path, converter_tag=False), _NETWORK, {}, _GEO_CRS
    )
    *_, converters, _, _ = build_network(
        buses,
        polygons,
        lines,
        False,
        None,
        _GEO_CRS,
        _DISTANCE_CRS,
        converter_search_radius_m=50000,
        **_BUILD,
    )
    assert converters.empty


def test_dc_voltage_floor_is_separate_from_ac(tmp_path):
    """150 kV HVDC survives the 220 kV AC floor; a 150 kV AC line does not."""
    _write(
        tmp_path / "EE_lines_way.json",
        [
            _raw_line(1, "150000", [(24.0, 59.0), (24.1, 59.0)], frequency="0"),
            _raw_line(2, "150000", [(24.0, 58.0), (24.1, 58.0)], frequency="50"),
        ],
    )
    _, _, lines = clean(
        {"lines_way": [str(tmp_path / "EE_lines_way.json")]}, _NETWORK, {}, _GEO_CRS
    )
    assert list(lines["line_id"]) == ["way/1"]


def test_lower_regional_ac_floor_takes_effect(tmp_path):
    """A region's 60 kV floor used to be cut off by the global 220 kV filter first."""
    _write(
        tmp_path / "BR_lines_way.json",
        [_raw_line(1, "69000", [(-47.0, -15.0), (-47.1, -15.0)], frequency="60")],
    )
    regions = {
        "BR": {"minimum_voltage_kv": 60.0, "frequency_hz": {"AC": 60.0, "DC": None}}
    }
    _, _, lines = clean(
        {"lines_way": [str(tmp_path / "BR_lines_way.json")]},
        _NETWORK,
        regions,
        _GEO_CRS,
    )
    assert list(lines["voltage"]) == [69000]


def test_relation_of_cable_ways_is_underground(tmp_path):
    """A relation whose member ways are all cables is underground."""
    _write(
        tmp_path / "BE_routes_relation.json",
        [
            _hvdc_relation(
                100, [_member(1, [(4.0, 50.0), (4.1, 50.0)], "cable")], voltage="320000"
            )
        ],
    )
    _write(
        tmp_path / "BE_cables_way.json",
        [
            _raw_line(
                1, "320000", [(4.0, 50.0), (4.1, 50.0)], frequency="0", power="cable"
            )
        ],
    )
    _, _, lines = clean(
        {
            "routes_relation": [str(tmp_path / "BE_routes_relation.json")],
            "cables_way": [str(tmp_path / "BE_cables_way.json")],
        },
        _NETWORK,
        {},
        _GEO_CRS,
    )
    assert list(lines["line_id"]) == ["relation/100"]
    assert list(lines["underground"]) == [True]
