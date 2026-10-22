"""Fixtures shared by more than one test module in this directory.

The `systemctl` fence this file used to carry (issue #2863) is now the
suite-wide live fence in `tests/_live_fence.py`, armed from `tests/conftest.py`:
the session-bus variables point nowhere, `systemctl` and friends reach a stub,
and the `user_manager` marker keeps the real bus for a test that needs it.
"""

from tests.test_scripts._deploy_candidates_world import (  # noqa: F401  (deploy_candidates)
    dc,
    dc_ready,
    dc_world,
)
from tests.test_scripts._deploy_station import station  # noqa: F401  (the deploy script's fixture)
