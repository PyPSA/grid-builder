"""Check that Snakemake executes the packages resolved by Pixi."""

import subprocess
import tomllib
from pathlib import Path

import yaml


def test_exported_environments_match_lock():
    """Prevent stale platform pins or manifest dependencies reaching consumers."""
    root = Path(__file__).resolve().parents[1]
    manifest = tomllib.loads((root / "pixi.toml").read_text())
    lock = yaml.safe_load((root / "pixi.lock").read_text())
    environment = lock["environments"]["module"]
    exported = yaml.safe_load((root / "workflow/envs/module.yaml").read_text())
    dependencies = manifest["feature"]["module"]["dependencies"]
    assert set(exported["dependencies"]) == {
        f"{name} {version}" for name, version in dependencies.items()
    }
    assert exported["channels"] == manifest["workspace"]["channels"] + ["nodefaults"]
    packages = {package["conda"]: package for package in lock["packages"]}
    for platform in manifest["workspace"]["platforms"]:
        pin = root / f"workflow/envs/module.{platform}.pin.txt"
        entries = {
            line
            for line in pin.read_text().splitlines()
            if line and not line.startswith(("#", "@"))
        }
        expected = {
            f"{item['conda']}#{packages[item['conda']]['md5']}"
            for item in environment["packages"][platform]
        }
        assert entries == expected, (
            f"Re-export the {platform} environment from pixi.lock"
        )


def test_export_to_custom_directory(tmp_path):
    """Support the same export command used by ModelBlocks integration tests."""
    root = Path(__file__).resolve().parents[1]
    outdir = tmp_path / "exported environments"
    subprocess.run(
        ["pixi", "run", "--locked", "export-snakemake-env", "module", str(outdir)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    for source in (root / "workflow/envs").glob("module.*"):
        assert (outdir / source.name).read_bytes() == source.read_bytes()
