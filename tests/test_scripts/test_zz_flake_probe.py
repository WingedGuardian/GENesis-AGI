"""THROWAWAY CI proof for #5024 (reverted before review): this change's own flaky test.

It fails on its first attempt and would pass on a second. The file is in the
change, so --rerun-exclude-path must deny it the retry and the job must go red.
"""

import os
import tempfile


def test_fails_once_but_is_the_changes_own():
    marker = os.path.join(tempfile.gettempdir(), "genesis-5024-own-flake")
    if not os.path.exists(marker):
        open(marker, "w").close()
        raise AssertionError("first attempt fails on purpose (#5024 CI proof)")
