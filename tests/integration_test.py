"""Set of standard Modelblocks tests.

PLEASE ENSURE THIS SET OF MINIMAL TESTS WORKS BEFORE PUBLISHING YOUR MODULE.
Contents may be updated in future template updates.
"""

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
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
        "config/examples/config.BE-NL.yaml",
        "tests/integration/Snakefile",
    ],
)
def test_standard_file_existance(module_path, file):
    """Check that a minimal set of files used for documentation are present."""
    assert Path(module_path / file).exists()


def test_snakemake_integration_testing(module_path):
    """Run retrieval from scratch in its Conda environment and validate outputs."""
    log_dir = module_path / "tests/integration/logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    run_log = log_dir / "integration.log"
    conda_prefix = module_path / ".snakemake/conda"

    # A directory under module_path, not pytest's tmp_path: on Windows CI,
    # tmp_path can land on a different drive than the checkout, and
    # Snakemake's relative-path handling then fails with "ValueError: path
    # is on mount 'D:', start on mount 'C:'". Keeping the working directory
    # on the same drive as the Snakefile/conda prefix avoids that. Every
    # assertion that reads from workdir has to stay inside this block -
    # TemporaryDirectory deletes it on exit.
    with tempfile.TemporaryDirectory(
        dir=module_path, prefix=".integration-", ignore_cleanup_errors=True
    ) as temporary_directory:
        workdir = Path(temporary_directory)
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
                        str(workdir),
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
                    env={**os.environ, "XDG_CACHE_HOME": str(workdir / "cache")},
                )
        finally:
            if (workdir / "logs").exists():
                shutil.copytree(workdir / "logs", log_dir, dirs_exist_ok=True)

        assert result.returncode == 0, run_log.read_text(encoding="utf-8")
        output_dir = workdir / "resources/grid-builder/osm/retrieve"
        for feature in ("substations_way", "lines_way"):
            payload = json.loads(
                (output_dir / f"benin_{feature}.json").read_text(encoding="utf-8")
            )
            elements = payload["elements"]
            assert elements, f"No {feature} records retrieved"
            assert all(item["geometry"] for item in elements)

        build_dir = workdir / "resources/grid-builder/osm/build"
        for component in ("buses", "lines", "transformers"):
            with (build_dir / "csv" / f"{component}.csv").open(
                encoding="utf-8"
            ) as file:
                rows = list(csv.DictReader(file))
            assert rows or component == "transformers"

        with (build_dir / "csv" / "lines.csv").open(encoding="utf-8") as file:
            built_lines = list(csv.DictReader(file))
        assert all(row["bus0"] != row["bus1"] for row in built_lines)
        assert all(float(row["voltage_kv"]) >= 220 for row in built_lines)

        for geojson_name in ("stations_polygon", "buses_polygon"):
            polygons = json.loads(
                (build_dir / "geojson" / f"{geojson_name}.geojson").read_text(
                    encoding="utf-8"
                )
            )
            assert polygons["type"] == "FeatureCollection"

        country_log = (
            workdir / "logs/grid-builder/retrieve_osm_pbf/benin.log"
        ).read_text(encoding="utf-8")
        assert "Wrote" in country_log
        assert str(conda_prefix) in country_log, "Retrieval must use its Conda Python"
