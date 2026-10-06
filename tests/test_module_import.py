"""Exercise remote source caching without relying on GitHub or live OSM data."""

import functools
import json
import os
import shutil
import subprocess
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from workflow.scripts._schema import generate_region_index


@pytest.fixture
def remote_module(tmp_path):
    """Serve only module sources, with a fresh URL and cache for each test."""
    repository = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    for directory in ("workflow", "config"):
        shutil.copytree(repository / directory, source / directory)
    payloads = {}

    class Handler(SimpleHTTPRequestHandler):
        """Serve sources and a minimal deterministic Overpass API."""

        def do_POST(self):
            """Return fixture elements matching the requested feature."""
            query = self.rfile.read(int(self.headers["Content-Length"])).decode()
            feature = None
            if 'way["power"="line"]' in query:
                feature = "lines_way"
            elif 'way["power"="substation"]' in query:
                feature = "substations_way"
            body = json.dumps(payloads.get(feature, {"elements": []})).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    handler = functools.partial(Handler, directory=str(source))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/workflow/Snakefile", payloads
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("backend", ["overpass", "geofabrik"])
def test_module_execution(tmp_path, remote_module, remote, backend):
    """Import beside a conflicting scripts package, then clean, build and plot."""
    remote_url, payloads = remote_module
    api_url = remote_url.rsplit("/workflow/", 1)[0] + "/overpass"
    workdir = tmp_path / "consumer"
    workdir.mkdir()
    scripts = workdir / "scripts"
    scripts.mkdir()
    (scripts / "__init__.py").touch()
    (scripts / "_schema.py").write_text("parent_marker = True\n")
    (scripts / "_helpers.py").write_text("parent_marker = 'parent helper'\n")
    (scripts / "probe.py").write_text(
        "from pathlib import Path\n"
        "from _helpers import parent_marker\n"
        "Path(snakemake.output[0]).write_text(parent_marker)\n"
    )
    source = (
        remote_url
        if remote
        else str(Path(__file__).resolve().parents[1] / "workflow/Snakefile")
    )
    (workdir / "Snakefile").write_text(
        f"""import sys
sys.path.insert(0, {str(workdir)!r})
import scripts._schema
import scripts._helpers

module grid:
    snakefile: {source!r}
    config: {{"countries": ["US"], "retrieve": {{"source": {backend!r}, "overpass_api": {{"url": {api_url!r}}}}}}}
    pathvars:
        resources="custom/resources",
        results="custom/results",
        logs="custom/logs",

use rule * from grid as grid_*

assert scripts._schema.parent_marker
assert scripts._helpers.parent_marker == "parent helper"
assert "custom/resources/retrieve/US_lines_way.json" in rules.grid_retrieve_osm_all.input
if {backend!r} == "geofabrik":
    assert rules.grid_retrieve_osm_pbf.params.data_dir == "custom/resources/automatic/earth-osm"
assert grid.config["regions"]["US"]["frequency_hz"]["AC"] == 60

rule parent_probe:
    output: "parent.txt"
    script: "scripts/probe.py"

rule all:
    default_target: True
    input: rules.grid_build_interactive_map.output, rules.parent_probe.output
"""
    )
    raw_dir = workdir / "custom/resources/retrieve"
    raw_dir.mkdir(parents=True)
    for feature in (
        "lines_way",
        "cables_way",
        "substations_way",
        "substations_node",
        "substations_relation",
        "routes_relation",
    ):
        elements = []
        if feature == "lines_way":
            elements = [
                {
                    "type": "way",
                    "id": 1,
                    "tags": {
                        "power": "line",
                        "voltage": "345000",
                        "frequency": "60",
                        "cables": "3",
                    },
                    "geometry": [
                        {"lon": -100.0, "lat": 40.0},
                        {"lon": -99.9, "lat": 40.0},
                    ],
                }
            ]
        if feature == "substations_way":
            elements = [
                {
                    "type": "way",
                    "id": i + 10,
                    "tags": {
                        "power": "substation",
                        "voltage": "345000",
                        "frequency": "60",
                        "substation": "transmission",
                    },
                    "geometry": [
                        {"lon": x, "lat": y}
                        for x, y in (
                            (lon - 0.001, 39.999),
                            (lon + 0.001, 39.999),
                            (lon + 0.001, 40.001),
                            (lon - 0.001, 40.001),
                            (lon - 0.001, 39.999),
                        )
                    ],
                }
                for i, lon in enumerate((-100.0, -99.9))
            ]
        raw_path = raw_dir / f"US_{feature}.json"
        payloads[feature] = {"elements": elements}
        if backend == "geofabrik":
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(json.dumps(payloads[feature]))
    env = {**os.environ, "XDG_CACHE_HOME": str(tmp_path / "cache")}
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-m", "snakemake", "--cores", "1", "--show-failed-logs"],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (workdir / "parent.txt").read_text() == "parent helper"
    assert (workdir / "custom/resources/map.html").stat().st_size > 0
    lines = json.loads(
        (workdir / "custom/resources/build/geojson/lines.geojson").read_text()
    )
    assert len(lines["features"]) == 1
    assert (workdir / "custom/resources/build/csv/buses.csv").stat().st_size > 0
    if backend == "overpass":
        assert (
            "Querying Overpass"
            in (workdir / "custom/logs/retrieve_osm_overpass/US.log").read_text()
        )


def test_region_manifest(tmp_path):
    """Every shipped regional default must be available to remote consumers."""
    regions = Path(__file__).resolve().parents[1] / "config/regions"
    generated = tmp_path / "index.yaml"
    generate_region_index(regions, generated)
    assert generated.read_text() == (regions / "index.yaml").read_text(), (
        "Run pixi run generate-config after adding or removing regional files."
    )
