"""Report framing and worker snapshots, using synthetic rows and owned files."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import _live_writes as audit
from tests.test_live_writes import _SUBRUN_CONFTEST


def _summary(tmp_path, monkeypatch, *, installed=True, disarmed="", hits=(), faults=0, missing=()):
    monkeypatch.setattr(audit._S, "installed", installed)
    monkeypatch.setattr(audit._S, "disarmed", disarmed)
    monkeypatch.setattr(audit._S, "hits", dict.fromkeys(hits))
    monkeypatch.setattr(audit._S, "faults", faults)
    monkeypatch.setattr(audit._S, "unreported", list(missing))
    report = tmp_path / "report.tsv"
    report.write_bytes(b"stale earlier report\n")
    monkeypatch.setenv("GENESIS_LIVE_WRITES_REPORT", str(report))
    lines = []
    reporter = type("R", (), {
        "config": object(),
        "write_line": lambda self, line, **kw: lines.append(line),
        "section": lambda self, line, **kw: lines.append(line),
    })()
    audit.pytest_terminal_summary(reporter)
    return report.read_bytes(), "\n".join(lines)


def test_report_escapes_fields_with_independent_golden_bytes(tmp_path, monkeypatch):
    key = ("test\tname", "class\nline", "op\rreturn", "/path\\n\t\n\r\udcff")
    payload, _ = _summary(tmp_path, monkeypatch, hits=[key])
    assert payload == b"test\\tname\tclass\\nline\top\\rreturn\t/path\\\\n\\t\\n\\r\xff\n"
    assert payload.count(b"\n") == 1
    assert payload.count(b"\t") == 3


@pytest.mark.parametrize("installed,disarmed", [(True, ""), (False, "no real home known"), (False, "")])
@pytest.mark.parametrize("with_hits", [False, True])
def test_controller_keeps_arrived_evidence_and_replaces_stale_report(tmp_path, monkeypatch, installed, disarmed, with_hits):
    key = ("test::owned", "~/.genesis", "write", "/owned")
    payload, text = _summary(tmp_path, monkeypatch, installed=installed, disarmed=disarmed,
                             hits=[key] if with_hits else [], faults=3, missing=["gw-missing"])
    assert payload == (b"test::owned\t~/.genesis\twrite\t/owned\n" if with_hits else b"")
    assert "3 events could not be classified" in text
    assert "no report from xdist worker gw-missing" in text
    assert ("~/.genesis: 1 tests" in text) == with_hits
    if not installed:
        assert ("disarmed" if disarmed else "not installed") in text


def test_worker_terminal_summary_does_not_publish_controller_report(tmp_path, monkeypatch):
    report = tmp_path / "report.tsv"
    report.write_bytes(b"controller marker")
    monkeypatch.setenv("GENESIS_LIVE_WRITES_REPORT", str(report))
    lines = []
    reporter = type("R", (), {"config": type("C", (), {"workeroutput": {}})(),
                              "write_line": lambda self, line, **kw: lines.append(line)})()
    audit.pytest_terminal_summary(reporter)
    assert report.read_bytes() == b"controller marker"
    assert not lines


def _subrun(tmp_path, conftest, source, workers):
    (tmp_path / "conftest.py").write_text(conftest)
    test_file = tmp_path / "test_owned.py"
    test_file.write_text(source)
    report = tmp_path / "report.tsv"
    repo = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(map(str, (tmp_path, repo, repo / "src"))),
           "GENESIS_LIVE_WRITES_REPORT": str(report)}
    env.pop("PYTEST_CURRENT_TEST", None)
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if workers:
        pytest.importorskip("xdist")
        command += ["-n", str(workers)]
    result = subprocess.run([*command, str(test_file)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=180)
    text = result.stdout + result.stderr
    assert result.returncode == 0, text
    return report.read_bytes(), text


@pytest.mark.parametrize("workers", [0, 1])
def test_native_unusual_filename_remains_one_physical_report_row(tmp_path, workers):
    payload, text = _subrun(tmp_path, _SUBRUN_CONFTEST, r'''
import os
from tests import _live_writes as audit

def test_owned_name(tmp_path):
    audit._S.real_home = str(tmp_path)
    audit._S.allowed_roots = ()
    name = os.fsencode(tmp_path) + b"/owned-\t\n\r\\n-\xff"
    with open(name, "wb") as output:
        output.write(b"owned")
''', workers)
    assert payload.count(b"\n") == 1, (payload, text)
    assert payload.count(b"\t") == 3
    assert payload.endswith(b"/owned-\\t\\n\\r\\\\n-\xff\n")


def test_xdist_snapshot_includes_late_ordinary_sessionfinish_writer(tmp_path):
    # Imported BEFORE audit: default hook order currently snapshots too early.
    (tmp_path / "late_writer.py").write_text('''
def pytest_sessionfinish(session):
    target = getattr(session.config, "_owned_late_target", None)
    if target:
        target.write_bytes(b"owned late")
        from tests import _live_writes as audit
        audit._S.faults += 1
''')
    conftest = _SUBRUN_CONFTEST.replace('["tests._live_writes"]', '["late_writer", "tests._live_writes"]')
    payload, text = _subrun(tmp_path, conftest, '''
from tests import _live_writes as audit

def test_prepare_late_owned_writer(tmp_path, request):
    audit._S.real_home = str(tmp_path)
    audit._S.allowed_roots = ()
    request.config._owned_late_target = tmp_path / "late-sessionfinish"
''', 1)
    assert b"/late-sessionfinish\n" in payload, text
    assert b"<outside a test>\t" in payload
    assert "1 events could not be classified" in text


@pytest.mark.parametrize("payload", [
    b"t\tc\top\t/path", b"t\tc\top\t/path\\q\n", b"t\tc\top\t/path\\\n",
    b"t\tc\t/path\n", b"t\tc\top\t/path\textra\n", b"t\tc\top\t/a\rb\n",
])
def test_decoder_rejects_malformed_framing(payload):
    from tests import _live_writes_report as codec

    with pytest.raises(ValueError):
        codec.decode(payload)


def test_decoder_roundtrip_preserves_literal_escapes_unicode_and_raw_bytes():
    from tests import _live_writes_report as codec

    rows = [("test::\\t", "class\u0085\u2028", "op\n\t\r\\", "/path\udcff"),
            ("", "", "", "")]
    assert codec.decode(codec.encode(rows)) == rows
    assert codec.decode(b"") == []
