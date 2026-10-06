"""Host leg: parsing the guardian's ram-status, and every way it degrades."""

from __future__ import annotations

import sys

import pytest

from genesis.hostmetrics import host as h

MIB = 1024 * 1024
_CONFIG = "host_ip: 192.0.2.10\nhost_user: tester\nssh_key: /nonexistent/key\n"


def test_parse_ok_reply():
    m = h.parse_ram_status(
        {"ok": True, "host": {"used_pct": 25.0, "detail": "25.0% used (1024M / 4096M)"}}
    )
    assert (m.total, m.used, m.unavailable) == (4096 * MIB, 1024 * MIB, None)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"ok": False, "error": "denied"}, "gateway refused the verb"),
        (
            {"ok": True, "host": {"used_pct": None, "detail": "host meminfo unreadable"}},
            "not measured by the guardian",
        ),
        (
            {"ok": True, "host": {"used_pct": 3.0, "detail": "3% of something"}},
            "unrecognised host detail",
        ),
    ],
)
def test_parse_degrades_with_a_reason(payload, reason):
    m = h.parse_ram_status(payload)
    assert m.total is None and reason in m.unavailable


def test_parser_reads_the_guardian_formatter_output(monkeypatch):
    # Contract: the detail string is produced by guardian/memory_watch.py; if its
    # format changes, the host leg would go silently unavailable. Pin it here.
    from genesis.guardian import memory_watch

    monkeypatch.setattr(
        memory_watch,
        "_read_meminfo",
        lambda: {"MemTotal": 8 * 1024 * 1024, "MemAvailable": 6 * 1024 * 1024},
    )
    pct, detail = memory_watch.measure_host_mem_pct()
    m = h.parse_ram_status({"ok": True, "host": {"used_pct": pct, "detail": detail}})
    assert (m.total, m.used) == (8192 * MIB, 2048 * MIB)


def test_no_config_is_unavailable(tmp_path):
    m = h.read_host_memory(tmp_path / "absent.yaml")
    assert "no host link configured" in m.unavailable


def test_missing_pyyaml_is_unavailable(tmp_path, monkeypatch):
    cfg = tmp_path / "g.yaml"
    cfg.write_text(_CONFIG)
    monkeypatch.setitem(sys.modules, "yaml", None)  # import yaml → ImportError
    assert "PyYAML not importable" in h.read_host_memory(cfg).unavailable


def test_incomplete_config_is_unavailable(tmp_path):
    cfg = tmp_path / "g.yaml"
    cfg.write_text("host_user: tester\n")
    assert "lacks host_ip" in h.read_host_memory(cfg).unavailable


def test_call_failure_is_unavailable_not_raised(tmp_path):
    cfg = tmp_path / "g.yaml"
    cfg.write_text(_CONFIG)

    def boom(*a):
        raise OSError("ssh missing")

    reason = h.read_host_memory(cfg, boom).unavailable
    assert reason == "host call failed: OSError"  # the type only: messages can carry the host


def test_raw_ssh_error_text_never_reaches_the_reason():
    # SSH stderr carries user@address; the reason printed to stdout must not.
    m = h.parse_ram_status(
        {"ok": False, "error": "tester@192.0.2.10: Permission denied (publickey)."}
    )
    assert m.unavailable == "guardian ram-status: SSH authentication failed"
    assert "192.0.2.10" not in m.unavailable and "tester" not in m.unavailable


def test_hung_call_times_out(tmp_path, monkeypatch):
    import asyncio

    cfg = tmp_path / "g.yaml"
    cfg.write_text(_CONFIG)
    monkeypatch.setattr(h, "_CALL_TIMEOUT", 0.05)

    class Hung:
        async def ram_status(self):
            await asyncio.sleep(5)

    assert "timed out" in h.read_host_memory(cfg, lambda *a: Hung()).unavailable


def test_reads_config_and_parses_the_reply(tmp_path):
    cfg = tmp_path / "g.yaml"
    cfg.write_text(_CONFIG)
    seen = {}

    class Remote:
        async def ram_status(self):
            return {"ok": True, "host": {"used_pct": 50.0, "detail": "50.0% used (2M / 4M)"}}

    def factory(ip, user, key):
        seen.update(ip=ip, user=user, key=key)
        return Remote()

    m = h.read_host_memory(cfg, factory)
    assert (m.total, m.used) == (4 * MIB, 2 * MIB)
    assert seen == {"ip": "192.0.2.10", "user": "tester", "key": "/nonexistent/key"}
