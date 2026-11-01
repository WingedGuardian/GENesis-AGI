"""Private comparison readback primitives; no runtime or approval authority."""

import math


def finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def valid_c_counters(state):
    return (isinstance(state, dict)
            and all(type(state.get(k)) is int and state[k] >= 0
                    for k in ("cursor", "timer_reserved", "timer_done", "level", "attempts"))
            and finite_number(state.get("wake_at")))
