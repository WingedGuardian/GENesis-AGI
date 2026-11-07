"""The printed status and its token must use the same captured inputs."""

from __future__ import annotations

import hashlib
import subprocess

import pytest

from tests.test_scripts._deploy_station import REPO

STATUS = REPO / "scripts/lib/deploy_status.sh"
SERVING = "a" * 40
VALUES = {"pid": "1111", "inv": "fixture-invocation", "edits": "none", "overrides": "none"}


def _token(values):
    raw = "\n".join([SERVING, values["pid"], values["inv"], values["overrides"], "fresh", ""])
    return "b1-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _probe(tmp_path, values=None, *, changing="", verify=False, direct=False, mutate_print=False):
    values = VALUES | (values or {})
    for name, value in values.items():
        (tmp_path / name).write_text(value + "\n")
    # File-backed readers work across command-substitution subshells. The
    # second read changes exactly one input; no timing or real systemd needed.
    script = r"""
source "$1"
scratch="$2" changing="$3" mutate_print="$4"
_value() {
    cat "$scratch/$1"
    if [ "$changing" = "$1" ]; then printf '%s\n' "$2" > "$scratch/$1"; fi
    printf '%s\n' "$1" >> "$scratch/reads"
}
systemctl() {
    case " $* " in
        *" MainPID "*) _value pid 2222 ;;
        *" InvocationID "*) _value inv later-invocation ;;
    esac
}
_runtime_edits() { _value edits '1 paths: src/changed.py'; }
_runtime_overrides() { _value overrides '1 files, later-fingerprint'; }
_read_serving() { SERVING=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; SERVING_HELD="$SERVING"; }
_git_ro() { case "$1" in rev-parse) echo "$SERVING" ;; ls-tree) echo fresh ;; esac; }
_runtime_held() { return 0; }
_unit_active_state() { echo active; }
_server_outside_unit() { return 1; }
_report_pending() { :; }
echo() {
    builtin echo "$@"
    if [ "$mutate_print" = yes ] && [[ "$*" == 'runtime-overrides: '* ]]; then
        printf '%s\n' '1 files, changed-at-print' > "$scratch/overrides"
    fi
}
if [ "$5" = direct ]; then
    _read_serving
    _bracket
    printf '%s\n' "$BRACKET"
else
    _status_main "$6"
fi
"""
    result = subprocess.run(
        [
            "bash",
            "-c",
            script,
            "_",
            str(STATUS),
            str(tmp_path),
            changing,
            "yes" if mutate_print else "no",
            "direct" if direct else "status",
            _token(values) if verify is True else (verify or ""),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    reads = (tmp_path / "reads").read_text().splitlines()
    return result, reads


@pytest.mark.parametrize("changing", ["pid", "inv", "edits", "overrides"])
@pytest.mark.parametrize("verify", [False, True])
def test_status_prints_and_hashes_one_capture(tmp_path, changing, verify):
    result, reads = _probe(tmp_path, changing=changing, verify=verify)
    assert result.returncode == 0, (result.stdout, result.stderr)
    for name in VALUES:
        assert reads.count(name) == 1, (name, reads, result.stdout)
    for label, name in [
        ("mainpid", "pid"),
        ("invocation", "inv"),
        ("runtime-edits", "edits"),
        ("runtime-overrides", "overrides"),
    ]:
        assert f"{label}: {VALUES[name]}\n" in result.stdout
    expected = (
        "valid — nothing the server runs changed since the token" if verify else _token(VALUES)
    )
    assert f"bracket: {expected}\n" in result.stdout


@pytest.mark.parametrize("verify", [False, True])
def test_overlay_changed_at_print_still_hashes_printed_capture(tmp_path, verify):
    result, reads = _probe(tmp_path, verify=verify, mutate_print=True)
    assert result.returncode == 0, result.stdout
    assert "runtime-overrides: none\n" in result.stdout
    assert (tmp_path / "overrides").read_text() == "1 files, changed-at-print\n"
    assert reads.count("overrides") == 1
    if not verify:
        assert f"bracket: {_token(VALUES)}\n" in result.stdout


@pytest.mark.parametrize(
    "values",
    [
        {"pid": ""},
        {"pid": "0"},
        {"pid": "invalid"},
        {"inv": ""},
        {"edits": ""},
        {"edits": "1 paths: src/changed.py"},
        {"overrides": "unreadable"},
    ],
)
@pytest.mark.parametrize("verify", [False, True])
def test_invalid_capture_never_recovers_by_reading_again(tmp_path, values, verify):
    result, reads = _probe(tmp_path, values, changing=next(iter(values)), verify=verify)
    assert result.returncode == int(verify), result.stdout
    assert "bracket: INVALID" in result.stdout if verify else "bracket: unknown" in result.stdout
    assert all(reads.count(name) == 1 for name in VALUES), reads


@pytest.mark.parametrize("overrides", ["none", "", "1 files, fixture-fingerprint"])
def test_direct_bracket_keeps_its_reading_contract(tmp_path, overrides):
    values = VALUES | {"overrides": overrides}
    result, reads = _probe(tmp_path, values, direct=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == _token(values) + "\n"
    assert sorted(reads) == sorted(VALUES)


def test_explicit_empty_override_is_not_reread(tmp_path):
    result, reads = _probe(tmp_path, {"overrides": ""}, changing="overrides", verify=True)
    assert result.returncode == 0, result.stdout
    assert "runtime-overrides: \n" in result.stdout
    assert reads.count("overrides") == 1


def test_changed_capture_invalidates_an_earlier_token(tmp_path):
    first, _ = _probe(tmp_path)
    assert _token(VALUES) in first.stdout
    # A later invocation takes a new capture. The old token no longer matches.
    changed = VALUES | {"overrides": "1 files, changed"}
    second_dir = tmp_path / "changed"
    second_dir.mkdir()
    second, _ = _probe(second_dir, changed, verify=_token(VALUES))
    assert second.returncode == 1, second.stdout
    assert "bracket: INVALID" in second.stdout
    assert f"now {_token(changed)}" in second.stdout
