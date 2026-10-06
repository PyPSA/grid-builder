"""Export Pixi's environment and platform pins using the ModelBlocks convention."""

import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


def export(environment: str = "module", output_dir: str = "workflow/envs") -> None:
    """Write the environment YAML and explicit platform package specifications."""
    root = Path(__file__).resolve().parents[1]
    manifest = tomllib.loads((root / "pixi.toml").read_text())
    if environment not in manifest["environments"]:
        raise ValueError(f"Unknown environment: {environment}")
    outdir = root / output_dir
    outdir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "pixi",
            "workspace",
            "export",
            "conda-environment",
            "--environment",
            environment,
            str(outdir / f"{environment}.yaml"),
        ],
        cwd=root,
        check=True,
    )
    with tempfile.TemporaryDirectory() as temporary:
        for platform in manifest["workspace"]["platforms"]:
            subprocess.run(
                [
                    "pixi",
                    "workspace",
                    "export",
                    "conda-explicit-spec",
                    "--environment",
                    environment,
                    "--platform",
                    platform,
                    temporary,
                ],
                cwd=root,
                check=True,
            )
            pin = Path(temporary) / f"{environment}_{platform}_conda_spec.txt"
            (outdir / f"{environment}.{platform}.pin.txt").write_bytes(pin.read_bytes())


if __name__ == "__main__":
    export(*sys.argv[1:])
