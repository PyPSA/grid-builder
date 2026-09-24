# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT

"""Helper functions shared by workflow/scripts/*.py.

Logging setup, internal data loading, shared geometry constants, and
mock_snakemake for testing.

Other scripts import this module as ``from scripts._helpers import ...``,
not ``workflow.scripts._helpers``. That plain form resolves in both places
these scripts run: under a real
Snakemake ``script:`` execution, Snakemake puts ``workflow/`` on ``sys.path``
(via the Snakefile's own ``sys.path.insert(0, workflow.basedir)``, captured
and propagated into every script's execution preamble); under pytest,
``pytest.ini``'s ``pythonpath`` setting puts ``workflow/`` on ``sys.path``
too, alongside the project root that lets test files import
``workflow.scripts.X``.
"""

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

GEO_CRS = "EPSG:4326"
BUS_TOL = 500  # metres; default station merge tolerance


def configure_logging(log_path: str) -> None:
    """Send rule and dependency logging to the Snakemake log file."""
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


def load_internal_yaml(filename: str) -> Any:
    """Load a YAML file from workflow/internal/, resolved relative to this checkout.

    Uses ``__file__`` rather than ``workflow.source_path()``: this is called
    from plain executed scripts (via Snakemake's ``script:`` directive),
    which only get a ``snakemake`` object in scope, not the ``workflow``
    object that ``source_path()`` needs — that API is only reachable from
    Snakefile-level rule code. Resolving via ``__file__`` still works
    correctly when this project is used as a Snakemake submodule, because
    Snakemake's own ``script:`` resolution already points each script at its
    real location inside this checkout (the same reasoning ``mock_snakemake``
    below relies on for ``script_dir``).
    """
    path = Path(__file__).resolve().parent.parent / "internal" / filename
    with open(path) as handle:
        return yaml.safe_load(handle)


def mock_snakemake(
    rulename: str,
    root_dir: str | Path | None = None,
    configfiles: list[str] | str | None = None,
    submodule_dir: str | Path = "workflow/submodules/grid-builder",
    **wildcards: str,
) -> Any:
    """Mock a Snakemake object for testing scripts outside of Snakemake.

    This function is expected to be executed from the 'scripts'-directory of
    the snakemake project. It returns a snakemake.script.Snakemake object,
    based on the Snakefile.

    If a rule has wildcards, you have to specify them in **wildcards.

    Parameters
    ----------
    rulename: str
        name of the rule for which the snakemake object should be generated
    root_dir: str/path-like
        path to the root directory of the snakemake project
    configfiles: list, str
        list of configfiles to be used to update the config
    submodule_dir: str, Path
        when this project is itself used as a submodule of another
        Snakemake workflow, submodule_dir is its conventional path
        relative to that parent project's directory.
    **wildcards:
        keyword arguments fixing the wildcards. Only necessary if wildcards are
        needed.
    """
    import os

    import snakemake as sm
    from packaging import version
    from snakemake import __version__ as sm_version
    from snakemake.api import Workflow
    from snakemake.common import SNAKEFILE_CHOICES
    from snakemake.logging import LoggerManager
    from snakemake.script import Snakemake
    from snakemake.settings.types import (
        ConfigSettings,
        DAGSettings,
        OutputSettings,
        ResourceSettings,
        StorageSettings,
        WorkflowSettings,
    )

    script_dir = Path(__file__).parent.resolve()
    project_dir = script_dir.parent.parent
    if root_dir is None:
        root_dir = script_dir.parent
    else:
        root_dir = Path(root_dir).resolve()

    workdir = None
    user_in_script_dir = Path.cwd().resolve() == script_dir
    if str(submodule_dir) in __file__:
        # the submodule_dir path is only need to locate the project dir
        os.chdir(Path(__file__[: __file__.find(str(submodule_dir))]))
    elif user_in_script_dir:
        os.chdir(root_dir)
    elif Path.cwd().resolve() == project_dir:
        # In this repository, notebooks/terminals often run from project root.
        # Keep the caller's workdir so pathvars resolve like normal project runs.
        workdir = Path.cwd().resolve()
    elif Path.cwd().resolve() != root_dir:
        logger.info(
            "Not in scripts or root directory, will assume this is a separate workdir"
        )
        workdir = Path.cwd()

    try:
        for p in SNAKEFILE_CHOICES:
            p = root_dir / p
            if os.path.exists(p):
                snakefile = p
                break
        if configfiles is None:
            configfiles = []
        elif isinstance(configfiles, str):
            configfiles = [configfiles]

        resource_settings = ResourceSettings()
        config_settings = ConfigSettings(configfiles=map(Path, configfiles))
        workflow_settings = WorkflowSettings()
        storage_settings = StorageSettings()
        dag_settings = DAGSettings(rerun_triggers=[])

        workflow_kwargs = dict(
            config_settings=config_settings,
            resource_settings=resource_settings,
            workflow_settings=workflow_settings,
            storage_settings=storage_settings,
            dag_settings=dag_settings,
            storage_provider_settings=dict(),
            overwrite_workdir=workdir,
        )

        # Snakemake version-dependent logger handling
        if version.parse(sm_version) >= version.parse("9.14.6"):
            output_settings = OutputSettings()
            workflow_kwargs["logger_manager"] = LoggerManager(
                logger=logger, settings=output_settings
            )

        workflow = Workflow(**workflow_kwargs)
        workflow.include(snakefile)

        if configfiles:
            for f in configfiles:
                if not os.path.exists(f):
                    raise FileNotFoundError(f"Config file {f} does not exist.")
                workflow.configfile(f)

        workflow.global_resources = {}
        rule = workflow.get_rule(rulename)
        dag = sm.dag.DAG(workflow, rules=[rule])
        wc = wildcards
        job = sm.jobs.Job(rule, dag, wc)

        def make_accessable(*ios: list[str]) -> None:
            """Rewrite each path in each ``ios`` list to an absolute path, in place."""
            for io in ios:
                for i, _ in enumerate(io):
                    io[i] = os.path.abspath(io[i])

        make_accessable(job.input, job.output, job.log)
        snakemake = Snakemake(
            job.input,
            job.output,
            job.params,
            job.wildcards,
            job.threads,
            job.resources,
            job.log,
            job.dag.workflow.config,
            job.rule.name,
            None,
        )
        # create log and output dir if not existent
        for path in list(snakemake.log) + list(snakemake.output):
            Path(path).parent.mkdir(parents=True, exist_ok=True)

    finally:
        if user_in_script_dir:
            os.chdir(script_dir)
    return snakemake
