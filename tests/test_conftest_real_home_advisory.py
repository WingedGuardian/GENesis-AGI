"""The CI-only advisory that names files a test wrote under the real ~/.genesis."""

from __future__ import annotations

import tests.conftest as genesis_conftest


class _Reporter:
    def __init__(self):
        self.lines: list[str] = []

    def section(self, title, **_):
        self.lines.append(f"== {title}")

    def write_line(self, line, **_):
        self.lines.append(line)


def _summary(monkeypatch, home, at_start):
    monkeypatch.setattr(genesis_conftest, "_REAL_GENESIS_HOME", home)
    monkeypatch.setattr(genesis_conftest, "_REAL_GENESIS_HOME_AT_START", at_start)
    reporter = _Reporter()
    genesis_conftest.pytest_terminal_summary(reporter)
    return reporter.lines


def test_a_home_created_during_the_run_is_named_with_every_entry(tmp_path, monkeypatch):
    home = tmp_path / ".genesis"
    home.mkdir()
    (home / "bootstrap_manifest.json").write_text("{}")
    (home / "capabilities.json").write_text("{}")
    lines = _summary(monkeypatch, home, at_start=False)
    assert any("real home written" in line for line in lines)
    assert "  bootstrap_manifest.json" in lines and "  capabilities.json" in lines


def test_nothing_is_said_when_the_home_existed_before_or_was_never_checked(tmp_path, monkeypatch):
    home = tmp_path / ".genesis"
    home.mkdir()
    assert _summary(monkeypatch, home, at_start=True) == []
    assert _summary(monkeypatch, home, at_start=None) == []  # not CI, or a worker


def test_nothing_is_said_when_the_home_is_still_absent(tmp_path, monkeypatch):
    assert _summary(monkeypatch, tmp_path / ".genesis", at_start=False) == []


def test_the_start_state_is_recorded_only_on_ci_and_never_in_a_worker(tmp_path, monkeypatch):
    class Config:
        pass

    monkeypatch.setattr(genesis_conftest, "_REAL_GENESIS_HOME", tmp_path / ".genesis")
    monkeypatch.setattr(genesis_conftest, "_REAL_GENESIS_HOME_AT_START", None)
    monkeypatch.delenv("CI", raising=False)
    genesis_conftest._note_real_home_at_start(Config())
    assert genesis_conftest._REAL_GENESIS_HOME_AT_START is None
    monkeypatch.setenv("CI", "true")
    worker = Config()
    worker.workerinput = {}
    genesis_conftest._note_real_home_at_start(worker)
    assert genesis_conftest._REAL_GENESIS_HOME_AT_START is None
    genesis_conftest._note_real_home_at_start(Config())
    assert genesis_conftest._REAL_GENESIS_HOME_AT_START is False
