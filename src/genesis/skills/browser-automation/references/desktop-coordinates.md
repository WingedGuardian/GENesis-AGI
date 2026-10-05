# Desktop coordinates (specification for the gated actuator)

Nothing on main exercises this. It records Win32 semantics for whoever builds
the desktop leg behind `src/genesis/autonomy/desktop_gate.py`; it is not
described behaviour. Browser coordinate rules live in the
`browser-automation` skill, "Coordinate safety".

## Coordinate spaces

| Space | Comes from |
|---|---|
| CSS pixels | `getBoundingClientRect()`, `outerHeight - innerHeight` |
| physical screen pixels | `xdotool` window geometry, a capture taken while DPI-aware |
| DPI-virtualised pixels | any Windows API read by a process that has not called `SetProcessDPIAware()` |
| normalised 0-65,535 | `SendInput` in absolute mode |

These coincide only at `devicePixelRatio == 1` and 100% display scaling, which
is why the bug class sits dormant on an unscaled dev machine and breaks on a
real laptop.

## `SendInput` absolute mode

- With `MOUSEEVENTF_ABSOLUTE`, `MOUSEINPUT.dx`/`.dy` are normalised to
  0-65,535 across the PRIMARY monitor only.
- With `MOUSEEVENTF_VIRTUALDESK` added, the range spans the whole virtual
  desktop, whose origin (`SM_XVIRTUALSCREEN`) is negative when a display sits
  left of or above the primary. Convert with
  `dx = (x - SM_XVIRTUALSCREEN) * 65535 / (SM_CXVIRTUALSCREEN - 1)`.
- Getting it wrong lands correctly on the primary display and wrong on every
  other one. Passing pixels unconverted scales by about `65535 / width`: on a
  1920-wide display a target at x=1000 lands about 29 px from the left edge,
  which reads as a near miss rather than a unit error.

## DPI awareness

Set it before the first measurement, not before the first use. On Windows at
125% scaling a DPI-unaware process is told 1536x864 for a 1920x1080 screen and
every measurement is off by 1.25. Measurements and the awareness API details:
`docs/reference/windows-remote-execution.md`, "DPI".

## Refusals

- `SendInput` returns the number of events injected and returns 0, without
  raising, when the OS refuses. Check it.
- It is not the whole story: input blocked by UIPI (a target window at a
  higher integrity level) is reported by neither the return value nor
  `GetLastError` (Microsoft `SendInput` documentation). Read back the effect on
  screen.

## Confirm, then act; log the landing

After positioning and before pressing, read back what is under the pointer and
refuse if it is not the intended target. Log actual against intended position,
with the drift and scale factor, at the moment of the action. A failed readback
is "unknown", never "fine".
