"""Set of standard Modelblocks tests.

PLEASE ENSURE THIS SET OF MINIMAL TESTS WORKS BEFORE PUBLISHING YOUR MODULE.
Contents may be updated in future template updates.
"""

import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from clio_tools.data_module import ModuleInterface


@pytest.fixture(scope="module")
def module_path():
    """Parent directory of the project."""
    return Path(__file__).parent.parent


def test_interface_file(module_path):
    """The interfacing file should be correct."""
    assert ModuleInterface.from_yaml(module_path / "INTERFACE.yaml")


@pytest.mark.parametrize(
    "file",
    [
        "AUTHORS",
        "CITATION.cff",
        "INTERFACE.yaml",
        "LICENSE",
        "README.md",
        "config/config.yaml",
        "config/config.schema.json",
        "tests/integration/Snakefile",
    ],
)
def test_standard_file_existance(module_path, file):
    """Check that a minimal set of files used for documentation are present."""
    assert Path(module_path / file).exists()


def test_snakemake_integration_testing(module_path, tmp_path):
    """Run retrieval from scratch in its Conda environment and validate outputs."""
    log_dir = module_path / "tests/integration/logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    run_log = log_dir / "integration.log"
    conda_prefix = module_path / ".snakemake/conda"
    try:
        with run_log.open("w", encoding="utf-8") as log:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "snakemake",
                    "--snakefile",
                    str(module_path / "tests/integration/Snakefile"),
                    "--directory",
                    str(tmp_path),
                    "--use-conda",
                    "--conda-prefix",
                    str(conda_prefix),
                    "--cores",
                    "1",
                    "--show-failed-logs",
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=600,
                check=False,
            )
    finally:
        if (tmp_path / "logs").exists():
            shutil.copytree(tmp_path / "logs", log_dir, dirs_exist_ok=True)

    assert result.returncode == 0, run_log.read_text(encoding="utf-8")
    output_dir = tmp_path / "resources/grid-builder/osm/out"
    for feature in ("substation", "line"):
        with (output_dir / f"benin_{feature}.csv").open(encoding="utf-8") as file:
            rows = list(csv.DictReader(file))
        geojson = json.loads(
            (output_dir / f"benin_{feature}.geojson").read_text(encoding="utf-8")
        )
        assert rows, f"No {feature} records retrieved"
        assert geojson["type"] == "FeatureCollection"
        assert len(geojson["features"]) == len(rows)
        assert all(item["geometry"] for item in geojson["features"])

    country_log = (tmp_path / "logs/grid-builder/retrieve_osm/benin.log").read_text(
        encoding="utf-8"
    )
    assert "Successfully retrieved OSM data for benin" in country_log
    assert str(conda_prefix) in country_log, "Retrieval must use its Conda Python"
