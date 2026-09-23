"""Focused tests for generic OSM cleaning and topology construction."""

import json

import pandas as pd

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
            {
                "Type": "way",
                "id": 3,
                "lonlat": json.dumps([[4.02, 50.02], [4.08, 50.08]]),
                "tags.power": "line",
                "tags.voltage": "220000",
            },
        ]
    )
    raw = tmp_path / "BE_substation.csv"
    frame.to_csv(raw, index=False)

    buses, polygons, lines = clean_osm_data(
        [str(raw)],
        {
            "minimum_voltage_kv": 220,
            "under_construction": "remove",
            "remove_after": None,
        },
        {},
    )

    assert set(buses["osm_id"]) == {"way/1", "way/2"}
    assert len(polygons) == 2
    assert lines.empty
