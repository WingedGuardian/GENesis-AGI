"""CC↔Node pin-lockstep CI guard (scripts/check_cc_node_lockstep.py).

Enforces that ``NODE_MAJOR`` in ``scripts/lib/cc_version.sh`` satisfies the
Node floor declared by the pinned Claude Code's ``engines.node``. All network
access is injected (a fake ``opener``), so these run fully offline and
deterministically. One real-file smoke test parses the live cc_version.sh
without touching the network.
"""

from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check_cc_node_lockstep.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_cc_node_lockstep", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


# ── Fake registry opener ──────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener_ok(engines_node: str | None):
    """An opener returning a valid registry payload with the given engines.node."""
    engines = {"node": engines_node} if engines_node is not None else {}

    def _open(url, timeout=None):
        return _FakeResp(json.dumps({"engines": engines}).encode())

    return _open


def _opener_raising(exc: Exception):
    def _open(url, timeout=None):
        raise exc

    return _open


def _write_pins(tmp_path: Path, cc: str, node: str) -> Path:
    p = tmp_path / "cc_version.sh"
    p.write_text(
        f'CC_VERSION="${{CC_VERSION:-{cc}}}"\n'
        f'NODE_MAJOR="${{NODE_MAJOR:-{node}}}"\n',
        encoding="utf-8",
    )
    return p


# ── parse_pins ────────────────────────────────────────────────────────────


def test_parse_pins_reads_defaults(tmp_path):
    pin = _write_pins(tmp_path, "2.1.201", "22")
    assert mod.parse_pins(pin) == ("2.1.201", 22)


def test_parse_pins_unparseable_fails_closed(tmp_path):
    pin = tmp_path / "cc_version.sh"
    pin.write_text("# no pins here\n", encoding="utf-8")
    with pytest.raises(mod.LockstepViolation):
        mod.parse_pins(pin)


def test_real_pin_file_parses(tmp_path):
    """Smoke: the live cc_version.sh parses (no network)."""
    real = _REPO_ROOT / "scripts" / "lib" / "cc_version.sh"
    cc, node = mod.parse_pins(real)
    assert cc.count(".") == 2 and node >= 1


# ── required_node_major (comparator parser) ───────────────────────────────


@pytest.mark.parametrize(
    "spec,expected",
    [
        (">=22.0.0", 22),
        (">=22", 22),
        ("^22.1.0", 22),
        ("~22", 22),
        ("22.x", 22),
        ("=18.0.0", 18),
        (">=20 <23", 20),   # range: lower bound governs
        (">=20.0.0 <23.0.0", 20),
    ],
)
def test_required_node_major_shapes(spec, expected):
    assert mod.required_node_major(spec) == expected


def test_required_node_major_unrecognized_fails_open():
    with pytest.raises(mod.FailOpen):
        mod.required_node_major("garbage-not-a-version")


# ── check() end-to-end (injected opener) ──────────────────────────────────


def test_lockstep_ok(tmp_path):
    pin = _write_pins(tmp_path, "2.1.201", "22")
    msg = mod.check(pin, opener=_opener_ok(">=22.0.0"))
    assert "lockstep OK" in msg


def test_node_below_floor_fails_closed(tmp_path):
    pin = _write_pins(tmp_path, "2.1.201", "20")
    with pytest.raises(mod.LockstepViolation) as ei:
        mod.check(pin, opener=_opener_ok(">=22.0.0"))
    assert "BELOW the floor" in str(ei.value)


def test_http_404_fails_closed(tmp_path):
    pin = _write_pins(tmp_path, "2.1.999", "22")
    err = urllib.error.HTTPError("url", 404, "Not Found", {}, io.BytesIO(b""))
    with pytest.raises(mod.LockstepViolation) as ei:
        mod.check(pin, opener=_opener_raising(err))
    assert "404" in str(ei.value)


def test_http_500_fails_open(tmp_path):
    pin = _write_pins(tmp_path, "2.1.201", "22")
    err = urllib.error.HTTPError("url", 503, "Service Unavailable", {}, io.BytesIO(b""))
    with pytest.raises(mod.FailOpen):
        mod.check(pin, opener=_opener_raising(err))


def test_network_error_fails_open(tmp_path):
    pin = _write_pins(tmp_path, "2.1.201", "22")
    with pytest.raises(mod.FailOpen):
        mod.check(pin, opener=_opener_raising(urllib.error.URLError("dns fail")))


def test_missing_engines_node_fails_open(tmp_path):
    pin = _write_pins(tmp_path, "2.1.201", "22")
    with pytest.raises(mod.FailOpen):
        mod.check(pin, opener=_opener_ok(None))


# ── main() exit codes ─────────────────────────────────────────────────────


def test_main_violation_exit_1(tmp_path, monkeypatch, capsys):
    pin = _write_pins(tmp_path, "2.1.201", "20")
    monkeypatch.setattr(mod, "fetch_engines_node", lambda v, **k: ">=22.0.0")
    assert mod.main(["--pin-file", str(pin)]) == 1
    assert "FAILED" in capsys.readouterr().err


def test_main_fail_open_exit_0(tmp_path, monkeypatch, capsys):
    pin = _write_pins(tmp_path, "2.1.201", "22")

    def _raise(v, **k):
        raise mod.FailOpen("registry down")

    monkeypatch.setattr(mod, "fetch_engines_node", _raise)
    assert mod.main(["--pin-file", str(pin)]) == 0
    assert "SKIPPED" in capsys.readouterr().err


def test_main_ok_exit_0(tmp_path, monkeypatch, capsys):
    pin = _write_pins(tmp_path, "2.1.201", "22")
    monkeypatch.setattr(mod, "fetch_engines_node", lambda v, **k: ">=22.0.0")
    assert mod.main(["--pin-file", str(pin)]) == 0
    assert "lockstep OK" in capsys.readouterr().out
