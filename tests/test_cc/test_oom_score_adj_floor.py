"""The oom_score_adj floor, measured on an owned child.

``set_oom_score_adj``'s docstring, the server unit template and the
memory-resilience reference all used to say that lowering an OOM adjustment is
always refused. It is not: the kernel compares the requested value against the
inherited ``oom_score_adj_min`` when CAP_SYS_RESOURCE is absent, so a decrease
that stays at or above that floor is accepted and only a value below it fails
with ``EACCES``.

The probe is ordered so the decrease is unambiguous: it raises to 500, then asks
for 100. Under the old claim that write would be refused; under the actual rule
it is accepted on any host whose floor is at most 100, which includes a floor of
0 and the floor this container inherits. The floor is then found by walking
below 100 until a write is refused.

Everything runs in a child process, so nothing here touches the pytest process, a
live service, or any process this test does not own. The child exits normally.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys

import pytest

_PROBE = r"""
import json

PATH = "/proc/self/oom_score_adj"


def read():
    with open(PATH) as handle:
        return int(handle.read().strip())


def write(value):
    try:
        with open(PATH, "w") as handle:
            handle.write(str(value))
        return None
    except OSError as exc:
        return exc.errno


out = {"start": read()}
errno_ = write(500)
out["raise"] = {"write": 500, "errno": errno_, "read": read()}
errno_ = write(100)
out["decrease"] = {"write": 100, "errno": errno_, "read": read()}
out["below"] = []
for value in (0, -1, -500):
    errno_ = write(value)
    out["below"].append({"write": value, "errno": errno_, "read": read()})
print(json.dumps(out))
"""


def _probe() -> dict:
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.fixture(scope="module")
def observed() -> dict:
    if not os.path.exists("/proc/self/oom_score_adj"):
        pytest.skip("/proc/self/oom_score_adj is not available on this platform")
    return _probe()


def test_raising_is_accepted_without_privilege(observed: dict) -> None:
    raise_ = observed["raise"]
    assert raise_["errno"] is None, raise_
    assert raise_["read"] == 500, raise_


def test_a_decrease_from_the_raised_value_is_accepted(observed: dict) -> None:
    # 500 -> 100. This is the write the old wording said would be refused.
    decrease = observed["decrease"]
    assert decrease["write"] < observed["raise"]["read"], (decrease, observed["raise"])
    assert decrease["errno"] is None, decrease
    assert decrease["read"] == 100, decrease


def test_a_decrease_below_the_floor_is_refused(observed: dict) -> None:
    refusals = [step for step in observed["below"] if step["errno"] is not None]
    if not refusals:
        pytest.skip("this environment holds CAP_SYS_RESOURCE, so the floor does not bind")

    for step in refusals:
        assert step["errno"] == errno.EACCES, step
        # The refusal leaves the value where the accepted decrease put it.
        assert step["read"] == 100, step


def test_the_floor_is_the_lowest_accepted_value(observed: dict) -> None:
    accepted = [step for step in observed["below"] if step["errno"] is None]
    if not accepted:
        pytest.skip("this environment holds CAP_SYS_RESOURCE, so the floor does not bind")

    # Anything accepted below 100 is the floor itself; everything under it was
    # refused, so the floor is the lowest accepted value, not a fixed 0.
    floor = min(step["write"] for step in accepted)
    for step in observed["below"]:
        if step["write"] < floor:
            assert step["errno"] == errno.EACCES, step
