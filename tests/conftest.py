"""Shared pytest fixtures."""

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def module_path():
    """Parent directory of the project."""
    # If your module needs files in resources/user/, place automated downloads here.
    return Path(__file__).parent.parent
