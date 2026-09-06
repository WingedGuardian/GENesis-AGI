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

import pytest

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
