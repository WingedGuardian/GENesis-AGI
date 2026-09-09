"""Coordinate-space safety for VNC-delivered clicks.

The VNC click path mixes two coordinate spaces: window position comes from
``xdotool`` in PHYSICAL screen pixels, while the in-page target and the chrome
height come from the DOM in CSS pixels. They coincide only at
``devicePixelRatio == 1``.

The display this suite runs on is dpr 1.0, so the scaled cases cannot be
exercised end to end here — which is exactly why the mapping was extracted
into a pure function and is tested directly.
"""

from __future__ import annotations

import asyncio

import pytest

from genesis.mcp.health import browser
from genesis.mcp.health.browser import vnc_click_target


def test_unscaled_display_is_a_plain_offset():
    """At dpr 1.0 the CSS and physical spaces coincide."""
    x, y = vnc_click_target(
        win_x=100, win_y=50, page_left=200, page_top=300, chrome_h=34, dpr=1.0,
    )
    assert (x, y) == (300, 384)


@pytest.mark.parametrize(
    ("dpr", "expected"),
    [
        (1.25, (350, 467)),   # 100 + 200*1.25 , 50 + (34+300)*1.25
        (1.5, (400, 551)),
        (2.0, (500, 718)),
    ],
)
def test_scaled_display_scales_the_css_offsets_only(dpr, expected):
    """The window origin is already physical; only the CSS parts scale.

    Scaling the whole sum would double-count the window origin — a distinct
    bug from the one this fixes, so it is pinned here too.
    """
    assert vnc_click_target(
        win_x=100, win_y=50, page_left=200, page_top=300, chrome_h=34, dpr=dpr,
    ) == expected


def test_a_scaled_display_lands_far_from_the_unscaled_answer():
    """The regression this exists to prevent, stated as a distance.

    Without the scale factor a control partway down the page is clicked high
    by a wide margin — silently, since nothing errors.
    """
    unscaled = vnc_click_target(
        win_x=0, win_y=0, page_left=0, page_top=800, chrome_h=0, dpr=1.0,
    )
    scaled = vnc_click_target(
        win_x=0, win_y=0, page_left=0, page_top=800, chrome_h=0, dpr=1.25,
    )
    assert scaled[1] - unscaled[1] == 200


@pytest.mark.parametrize("bad_dpr", [0, 0.0, None, -1.0])
def test_an_implausible_dpr_falls_back_to_unscaled(bad_dpr):
    """A missing or nonsense dpr must not collapse the coordinate to the origin.

    ``page.evaluate`` can return a spoofed or absent value — the anti-detection
    layer already spoofs sibling window metrics — and multiplying by 0 would
    silently click the top-left corner of the window.
    """
    x, y = vnc_click_target(
        win_x=10, win_y=20, page_left=200, page_top=300, chrome_h=34,
        dpr=bad_dpr,
    )
    assert (x, y) == (210, 354)


# ---------------------------------------------------------------------------
# Pointer readback probe lifecycle
#
# The readback exists to prove where the pointer actually LANDED, so it runs
# on every VNC click. That makes its failure path a lifecycle question, not a
# cosmetic one: asyncio.wait_for cancels the WAIT, never the child, so a probe
# that times out against a wedged X server keeps running unless it is killed.
# ---------------------------------------------------------------------------


class _FakeProc:
    """A subprocess stand-in that records whether it was killed and reaped."""

    def __init__(self, *, stdout: bytes = b"", hang: bool = False):
        self._stdout = stdout
        self._hang = hang
        self.killed = False
        self.reaped = False
        self.returncode = None

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(60)  # cancelled by wait_for; process lives on
        return self._stdout, b""

    def kill(self):
        self.killed = True

    async def wait(self):
        self.reaped = True
        self.returncode = -9
        return self.returncode


def _patch_spawn(monkeypatch, result):
    """Install ``result`` (a proc or an exception) as the spawned process."""
    async def _fake_exec(*args, **kwargs):
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)


async def test_a_timed_out_probe_is_killed_and_reaped(monkeypatch):
    """The failure this test exists for is a LEAK, not a wrong answer.

    ``asyncio.wait_for`` cancels the wait and leaves the child running —
    measured: ``returncode`` is None and the pid is still alive afterwards.
    Without an explicit kill, every stalled probe outlives its call for as
    long as the wedged X server holds it.
    """
    proc = _FakeProc(hang=True)
    _patch_spawn(monkeypatch, proc)

    assert await browser._read_pointer_position(timeout_s=0.01) is None
    assert proc.killed, "timed-out probe was never killed — the process leaks"
    assert proc.reaped, "killed probe was never awaited — it stays a zombie"


async def test_a_healthy_probe_returns_the_pointer_and_is_not_killed(monkeypatch):
    """Negative control: the kill must fire ONLY on the timeout path.

    A kill on the success path would be indistinguishable from the fix in the
    timeout test above while breaking every real readback.
    """
    proc = _FakeProc(stdout=b"X=412\nY=307\nSCREEN=0\nWINDOW=12582919\n")
    _patch_spawn(monkeypatch, proc)

    assert await browser._read_pointer_position(timeout_s=5) == (412, 307)
    assert not proc.killed
    assert not proc.reaped


async def test_a_missing_xdotool_is_reported_not_raised(monkeypatch):
    """A missing binary must not abandon the click.

    ``create_subprocess_exec`` raises ``FileNotFoundError`` when xdotool is
    absent, and the caller's outer handler for that exception reports a
    missing ``vncdo`` and returns False. Letting it escape would turn an
    unavailable measurement into a skipped click.
    """
    _patch_spawn(monkeypatch, FileNotFoundError("xdotool"))

    assert await browser._read_pointer_position(timeout_s=5) is None


async def test_unparseable_probe_output_is_not_a_pointer(monkeypatch):
    """Absent coordinates must read as absent, never as (0, 0).

    Zero is a legitimate pointer position, so a parse failure that returned it
    would masquerade as a real reading in the drift check.
    """
    proc = _FakeProc(stdout=b"SCREEN=0\nWINDOW=12582919\n")
    _patch_spawn(monkeypatch, proc)

    assert await browser._read_pointer_position(timeout_s=5) is None
    assert not proc.killed


async def test_kill_and_reap_tolerates_an_already_exited_process():
    """A process that died between the timeout and the kill is not an error."""
    class _Gone(_FakeProc):
        def kill(self):
            raise ProcessLookupError

    proc = _Gone()
    await browser._kill_and_reap(proc)  # must not raise
    assert not proc.reaped
