"""Fixtures shared by more than one test module in this directory."""

from tests.test_scripts._deploy_candidates_world import (  # noqa: F401  (deploy_candidates)
    dc,
    dc_ready,
    dc_world,
)
from tests.test_scripts._deploy_station import station  # noqa: F401  (the deploy script's fixture)
