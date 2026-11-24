"""Concurrent publication controls for the positive FD witness."""

import threading

import pytest

from tests import _live_writes as audit
from tests.test_live_writes import home as home
from tests.test_live_writes_fd import _write


@pytest.mark.parametrize("stage", ["metadata", "judgment"])
def test_state_replacement_during_postprocessing_cannot_discharge_old_evidence(home, monkeypatch, stage):
    witness = audit._W
    entered, finish = threading.Event(), threading.Event()
    original_parent, original_judge = witness._parent, audit._judge
    calls = 0
    errors = []

    def pause():
        entered.set()
        assert finish.wait(10)

    def parent(*args):
        nonlocal calls
        value = original_parent(*args)
        calls += 1
        if stage == "metadata" and calls == 2:
            pause()
        return value

    def judge(event, args):
        if stage == "judgment" and event == "open" and isinstance(args[0], int):
            pause()
        return original_judge(event, args)

    def run():
        try:
            _write(home.parent / "allowed")
        except BaseException as error:
            errors.append(error)

    monkeypatch.chdir(home / "genesis")
    monkeypatch.setattr(witness, "_parent", parent)
    monkeypatch.setattr(audit, "_judge", judge)
    audit._S.hits.clear()
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(10)
        monkeypatch.setattr(audit._S, "hits", {})
    finally:
        finish.set()
        thread.join(10)
    assert not thread.is_alive() and not errors
    assert audit._S.faults >= 1
    assert any(row[1] == "observer fault" for row in audit._S.hits)
    assert (home.parent / "allowed/owned").read_bytes() == b"owned synthetic bytes"


def test_overlapping_captures_do_not_read_a_drained_but_unpublished_scope_as_clean(home, monkeypatch):
    witness = audit._W
    entered, native_finish, drained, publish_finish = [threading.Event() for _ in range(4)]
    original_begin, original_publish = witness._begin, witness._publish_pending
    errors = []

    def begin(*args):
        scope = original_begin(*args)
        if scope is not None:
            entered.set()
            assert native_finish.wait(10)
        return scope

    def publish(scopes):
        if threading.current_thread() is capturing:
            drained.set()
            assert publish_finish.wait(10)
        return original_publish(scopes)

    def run():
        try:
            _write(home.parent / "allowed")
        except BaseException as error:
            errors.append(error)

    def capture():
        try:
            witness.capture(audit._live_writes_report.census_state)
        except BaseException as error:
            errors.append(error)

    monkeypatch.chdir(home / "genesis")
    monkeypatch.setattr(witness, "_begin", begin)
    monkeypatch.setattr(witness, "_publish_pending", publish)
    writing = threading.Thread(target=run)
    capturing = threading.Thread(target=capture)
    writing.start()
    try:
        assert entered.wait(10)
        capturing.start()
        assert drained.wait(10)
        _, state = witness.capture(audit._live_writes_report.census_state)
        assert state["faults"] >= 1
    finally:
        publish_finish.set()
        native_finish.set()
        if capturing.ident is not None:
            capturing.join(10)
        writing.join(10)
    assert not writing.is_alive() and not capturing.is_alive() and not errors
