"""Focused tests for generic OSM cleaning and topology construction."""

import json

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString

from workflow.scripts.build_osm_network import build_osm_network
from workflow.scripts.clean_osm_data import clean_osm_data


def test_cleaner_removes_line_in_overlapping_substation_polygons(tmp_path):
    """Containment filtering remains index-safe when polygons overlap."""
    square = [[4.0, 50.0], [4.1, 50.0], [4.1, 50.1], [4.0, 50.1], [4.0, 50.0]]
    frame = pd.DataFrame(
        [
            {
                "Type": "area",
                "id": 1,
                "lonlat": json.dumps(square),
                "tags.power": "substation",
                "tags.voltage": "220000",
            },
            {
                "Type": "area",
                "id": 2,
                "lonlat": json.dumps(square),
                "tags.power": "substation",
                "tags.voltage": "220000",
            },
        ]
    )
    substations_path = tmp_path / "BE_substation.csv"
    frame.to_csv(substations_path, index=False)

    lines_frame = pd.DataFrame(
        [
            {
                "Type": "way",
                "id": 3,
                "lonlat": json.dumps([[4.02, 50.02], [4.08, 50.08]]),
                "tags.power": "line",
                "tags.voltage": "220000",
            }
        ]
    )
    lines_path = tmp_path / "BE_line.csv"
    lines_frame.to_csv(lines_path, index=False)

    network = {"minimum_voltage_kv": 220}
    buses, polygons, lines = clean_osm_data(
        [str(substations_path), str(lines_path)], network, {}
    )

    assert set(buses["bus_id"]) == {"way/1", "way/2"}
    assert len(polygons) == 2
    assert lines.empty


def test_cleaner_groups_relation_member_ways_into_one_line(tmp_path):
    """A route=power relation collapses its member ways into one circuit."""
    frame = pd.DataFrame(
        [
            {
                "Type": "way",
                "id": 10,
                "lonlat": json.dumps([[4.0, 50.0], [4.1, 50.0]]),
                "tags.power": "line",
                "tags.voltage": "380000",
            },
            {
                "Type": "way",
                "id": 11,
                "lonlat": json.dumps([[4.1, 50.0], [4.2, 50.0]]),
                "tags.power": "line",
                "tags.voltage": "380000",
            },
        ]
    )
    raw = tmp_path / "BE_line.csv"
    frame.to_csv(raw, index=False)

    def _member(ref: int, lon0: float, lon1: float) -> dict:
        return {
            "type": "way",
            "ref": ref,
            "role": "line",
            "geometry": [{"lat": 50.0, "lon": lon0}, {"lat": 50.0, "lon": lon1}],
        }

    relation_payload = {
        "elements": [
            {
                "type": "relation",
                "id": 99,
                "tags": {"route": "power", "voltage": "380000", "cables": "3"},
                "members": [_member(10, 4.0, 4.1), _member(11, 4.1, 4.2)],
            }
        ]
    }
    relations_path = tmp_path / "BE_relation.json"
    relations_path.write_text(json.dumps(relation_payload))

    network = {"minimum_voltage_kv": 220}
    _, _, lines = clean_osm_data([str(raw)], network, {}, [str(relations_path)])

    assert len(lines) == 1
    assert lines.iloc[0]["line_id"] == "relation/99"
    assert lines.iloc[0].geometry.length > 0


def test_builder_merges_compatible_segments_through_virtual_bus():
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

    buses, built_lines, transformers = build_osm_network(
        substations, substations_polygon, lines, "keep", None, merge_distance_m=1
    )

    assert len(buses) == 2
    assert len(built_lines) == 1
    assert len(transformers) == 0
