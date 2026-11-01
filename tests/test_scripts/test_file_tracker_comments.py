"""Scanned comments share the filer's real snapshot/lock/signal pipeline."""

import io
import json
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import private_module


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    filer = private_module("owned_comment_filer", root / "scripts/file_tracker_issue.py")

    class OwnedPath(type(Path())):
        @classmethod
        def home(cls):
            return cls(tmp_path)

    monkeypatch.setattr(filer, "Path", OwnedPath)
    body = tmp_path / "draft.md"
    original = b"\nSynthetic measured finding\r\n"
    body.write_bytes(original)
    state = SimpleNamespace(
        filer=filer, body=body, original=original, calls=[], scans=[], posts=[], pages=None,
        existing=[], post_error=None, post_persists=True, post_response=None,
        lookup_error=None, lookup_returncode=0, post_returncode=0, mutate_on_lookup=False,
        target={"number": 7, "html_url": "https://github.com/Example/Fixture/issues/7"},
    )

    def scan(title, text):
        state.scans.append((title, text))
        return []

    monkeypatch.setattr(filer, "privacy_scan", scan)

    def response(data, code=0):
        return subprocess.CompletedProcess([], code, json.dumps(data), "")

    def run(argv):
        state.calls.append(argv)
        assert state.scans == [("", original.decode())], "network preceded the full raw-body scan"
        if argv[:3] == ["gh", "repo", "view"]:
            if state.lookup_error is not None:
                raise state.lookup_error
            if state.mutate_on_lookup:
                body.write_text("Different synthetic draft after scan")
            if "viewerPermission" in argv:
                return response({"viewerPermission": "WRITE"})
            return response({"isFork": False, "nameWithOwner": "Example/Fixture"})
        endpoint = "repos/Example/Fixture/issues/7"
        if endpoint in argv and "POST" not in argv:
            return response(state.target)
        if any(str(a).startswith(endpoint + "/comments") for a in argv):
            if "POST" not in argv:
                assert "--paginate" in argv and "--slurp" in argv
                return response(state.pages if state.pages is not None else [state.existing], state.lookup_returncode)
            field = argv[argv.index("-F") + 1]
            assert field.startswith("body=@")
            snapshot = Path(field.removeprefix("body=@"))
            assert snapshot != body and snapshot.read_bytes() == original
            assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
            assert stat.S_IMODE(snapshot.parent.stat().st_mode) == 0o700
            state.posts.append(argv)
            posted = dict(id=42, body=original.decode(), html_url=endpoint_url(42))
            if state.post_persists:
                state.existing.append(posted)
            if state.post_error:
                raise state.post_error
            return response(state.post_response if state.post_response is not None else posted, state.post_returncode)
        raise AssertionError(f"unexpected owned runner command: {argv!r}")

    def main(*extra):
        try:
            return filer.main(["--comment-on", "7", "--body-file", str(body), *extra], run=run)
        except SystemExit as exc:
            return exc.code

    state.main = main
    return state


def endpoint_url(identifier):
    return f"https://github.com/Example/Fixture/issues/7#issuecomment-{identifier}"


def test_scanned_raw_bytes_are_posted_once_from_private_snapshot(fixture, capsys):
    assert fixture.main() == 0
    assert len(fixture.posts) == 1
    assert endpoint_url(42) in capsys.readouterr().out
    assert not list(fixture.body.parent.glob("tmp/issue-body-*"))


def test_comment_dry_run_runs_all_reads_but_no_post(fixture):
    assert fixture.main("--dry-run") == 0
    assert fixture.calls and fixture.posts == []


def test_exact_duplicate_after_full_first_page_never_posts(fixture):
    first = [dict(id=i, body=f"Synthetic other body {i}", html_url=endpoint_url(i)) for i in range(1, 101)]
    fixture.pages = [first, [dict(id=101, body=fixture.original.decode(), html_url=endpoint_url(101))]]
    assert fixture.main() == 3
    assert fixture.posts == []


@pytest.mark.parametrize("target", [
    {"number": True, "html_url": "https://github.com/Example/Fixture/issues/7"},
    {"number": 8, "html_url": "https://github.com/Example/Fixture/issues/8"},
    {"number": 7, "html_url": "https://github.com/Elsewhere/Fixture/issues/7"},
    {"number": 7, "html_url": "https://other.invalid/Example/Fixture/issues/7"},
])
def test_wrong_or_transferred_target_refuses_before_post(fixture, target):
    fixture.target = target
    assert fixture.main() == 2
    assert fixture.posts == []


def test_match_does_not_hide_malformed_later_page(fixture):
    fixture.pages = [[dict(id=42, body=fixture.original.decode(), html_url=endpoint_url(42))], {"error": "partial"}]
    assert fixture.main() == 2
    assert fixture.posts == []


def test_uncertain_post_reconciles_existing_body_without_second_post(fixture):
    fixture.post_error = subprocess.TimeoutExpired("owned gh", 120)
    assert fixture.main() == 0
    assert len(fixture.posts) == 1


def test_uncertain_absent_post_stays_unknown_and_never_retries(fixture):
    fixture.post_error = subprocess.TimeoutExpired("owned gh", 120)
    fixture.post_persists = False
    assert fixture.main() == 4
    assert len(fixture.posts) == 1


@pytest.mark.parametrize("extra", [
    ("--area", "bug"), ("--difficulty", "easy"), ("--title-file", "unused"),
])
def test_mixed_mode_arguments_refuse_without_network(fixture, extra):
    assert fixture.main(*extra) == 2
    assert fixture.calls == []


def test_privacy_refusal_has_no_network_or_snapshot(fixture, monkeypatch):
    def refuse(*args):
        raise fixture.filer.Refused("Synthetic privacy rejection")

    monkeypatch.setattr(fixture.filer, "privacy_scan", refuse)
    assert fixture.main() == 2
    assert fixture.calls == [] and fixture.posts == []
    assert not list(fixture.body.parent.glob("tmp/issue-body-*"))


def test_draft_change_after_scan_does_not_change_posted_snapshot(fixture):
    fixture.mutate_on_lookup = True
    assert fixture.main() == 0
    assert fixture.body.read_bytes() != fixture.original
    assert len(fixture.posts) == 1


@pytest.mark.parametrize("raw", [b"", b" \r\n", b"\xff"])
def test_empty_or_invalid_utf8_has_no_scan_or_network(fixture, raw):
    fixture.body.write_bytes(raw)
    assert fixture.main() == 2
    assert fixture.scans == [] and fixture.calls == []


@pytest.mark.parametrize("pages", [[], [None], [[{"id": True, "body": "Synthetic", "html_url": endpoint_url(1)}]]])
def test_invalid_paginated_schema_is_not_absence(fixture, pages):
    fixture.pages = pages
    assert fixture.main() == 2 and fixture.posts == []


def test_failed_later_page_is_not_absence_even_with_valid_partial_stdout(fixture):
    fixture.lookup_returncode = 1
    assert fixture.main() == 2 and fixture.posts == []


@pytest.mark.parametrize("changes", [
    {"id": True}, {"id": 0}, {"body": "Different body"},
    {"html_url": "https://other.invalid/Example/Fixture/issues/7#issuecomment-42"},
    {"html_url": "https://github.com/Elsewhere/Fixture/issues/7#issuecomment-42"},
    {"html_url": "https://github.com/Example/Fixture/issues/7#issuecomment-43"},
])
def test_unconfirmed_response_and_absence_stay_unknown(fixture, changes):
    fixture.post_response = dict(id=42, body=fixture.original.decode(), html_url=endpoint_url(42)) | changes
    fixture.post_persists = False
    assert fixture.main() == 4 and len(fixture.posts) == 1


@pytest.mark.parametrize("persists", [False, True])
def test_nonzero_post_reconciles_only_by_read(fixture, persists):
    fixture.post_returncode = 1
    fixture.post_persists = persists
    assert fixture.main() == (0 if persists else 4)
    assert len(fixture.posts) == 1


@pytest.mark.parametrize("persists", [False, True])
def test_interrupted_post_reconciles_only_by_read(fixture, persists):
    fixture.post_error = KeyboardInterrupt()
    fixture.post_persists = persists
    assert fixture.main() == (0 if persists else 4)
    assert len(fixture.posts) == 1


def test_reused_lookup_error_does_not_print_its_private_message(fixture, capsys):
    fixture.lookup_error = fixture.filer.Refused("synthetic-private-diagnostic")
    assert fixture.main() == 2
    assert "synthetic-private-diagnostic" not in capsys.readouterr().err
    assert fixture.posts == []


def test_confirmed_comment_survives_stdout_failure(fixture, monkeypatch):
    def failed_print(*args, **kwargs):
        raise OSError("Synthetic reporting failure")

    monkeypatch.setattr(fixture.filer, "print", failed_print, raising=False)
    assert fixture.main() == 0 and len(fixture.posts) == 1


@pytest.mark.parametrize("outcome, expected, posts", [
    ("refused", 2, 0), ("unknown", 4, 1), ("confirmed", 0, 1), ("duplicate", 3, 0),
])
def test_closed_stderr_preserves_comment_outcome(fixture, monkeypatch, outcome, expected, posts):
    closed = io.StringIO()
    closed.close()
    if outcome == "refused":
        fixture.lookup_error = fixture.filer.Refused("Synthetic preflight refusal")
    elif outcome == "duplicate":
        fixture.existing.append(dict(id=42, body=fixture.original.decode(), html_url=endpoint_url(42)))
    else:
        fixture.post_error = subprocess.TimeoutExpired("owned gh", 120)
        fixture.post_persists = outcome == "confirmed"
    with monkeypatch.context() as patch:
        patch.setattr(fixture.filer.sys, "stderr", closed)
        result = fixture.main()
    assert result == expected
    assert len(fixture.posts) == posts
    assert not list(fixture.body.parent.glob("tmp/issue-body-*"))


def test_confirmed_comment_survives_lock_release_interrupt(fixture, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def interrupted_release(slug):
        yield
        raise KeyboardInterrupt()

    monkeypatch.setattr(fixture.filer, "tracker_lock", interrupted_release)
    assert fixture.main() == 0 and len(fixture.posts) == 1
