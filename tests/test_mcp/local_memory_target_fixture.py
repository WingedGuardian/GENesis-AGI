"""Observe the real launch decision without reading or replacing stdin."""

import json
import os
import sys
from pathlib import Path

from tests.test_mcp.local_memory_fixture import install as install_memory


def install():
    install_memory()
    descriptor = os.environ.get("LOCAL_MEMORY_TARGET_OBSERVER_FD")
    if descriptor is None:
        return
    descriptor = int(descriptor)
    root = Path(__file__).resolve().parents[2]
    launcher = root / "scripts/genesis_mcp_server.py"
    stdio = root / "src/genesis/mcp/shared_client/_stdio.py"
    claims = 0
    assert sys.getprofile() is None, "the fixture must not replace another profiler"

    def observe(frame, event, result):
        nonlocal claims
        name = frame.f_code.co_name
        if name == "serve" and event == "call" and Path(frame.f_code.co_filename).resolve() == stdio:
            claims += 1
        if name != "_local_frontend" or event != "return" or Path(frame.f_code.co_filename).resolve() != launcher:
            return
        record = {"event": "frontend-selection", "shared": result is not None,
                  "ordinary_db": str(frame.f_globals["_DEFAULT_DB"]), "shared_io_claims": claims}
        payload = (json.dumps(record) + "\n").encode()
        assert os.write(descriptor, payload) == len(payload)
        # The decision is the boundary under test. Leave actual serving untouched.
        sys.setprofile(None)

    sys.setprofile(observe)
