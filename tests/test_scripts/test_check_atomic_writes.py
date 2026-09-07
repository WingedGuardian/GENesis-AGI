"""Controls for the atomic-write guard, in BOTH directions.

WHY THIS FILE IS MOSTLY CONTROLS. The guard went through two versions that both
LOOKED correct and were wrong in opposite directions -- one called 5 of 6 known
leaks safe, the next admitted 16 `dataclasses.replace` calls as filesystem
writes. Neither was caught by running it. A detector that finds nothing is
indistinguishable from one that looks at nothing, so the shapes it must flag and
the shapes it must ignore are both pinned here, and the real known-clean and
known-leaking sites in this repo anchor it to reality.

Install-agnostic: synthetic sources for the detector, and repo-relative reads for
the anchors. No network, no live DB, no ~/.genesis, no wall clock.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
_GUARD = _REPO / "scripts" / "check_atomic_writes.py"


def _load():
    spec = importlib.util.spec_from_file_location("_check_atomic_writes", _GUARD)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_check_atomic_writes"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop("_check_atomic_writes", None)
        raise
    return mod


chk = _load()


def _verdicts(src: str) -> list[str]:
    return [r["verdict"] for r in chk.analyse_source(src, "synthetic.py")]


# ---------------------------------------------------------------------------
# MUST FLAG. The shapes the guard exists for.
# ---------------------------------------------------------------------------

_LEAKS = '''
import os, tempfile
def f(path):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as h:
            h.write("x")
        os.replace(tmp, str(path))
    except OSError:
        pass
'''

_NO_HANDLER = '''
import os, tempfile
def f(path):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    with os.fdopen(fd, "w") as h:
        h.write("x")
    os.replace(tmp, str(path))
'''

_HANDROLLED = '''
import os
def f(path):
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        open(tmp, "w").write("x")
        os.replace(tmp, path)
    except OSError:
        return
'''


@pytest.mark.parametrize(
    "src,expected",
    [(_LEAKS, "LEAKS"), (_NO_HANDLER, "NO_HANDLER"), (_HANDROLLED, "LEAKS")],
)
def test_the_guard_flags_an_unguarded_atomic_write(src, expected):
    assert _verdicts(src) == [expected]


def test_a_hand_rolled_temp_name_is_not_invisible():
    """The first version anchored on `mkstemp` and could not see this shape at
    all -- which hid two of the repo's known leaks, because they build their temp
    name from the pid instead of calling mkstemp."""
    assert _verdicts(_HANDROLLED) == ["LEAKS"]


# ---------------------------------------------------------------------------
# MUST NOT FLAG. Every one of these was a real false positive at some point.
# ---------------------------------------------------------------------------

_CLEAN = '''
import contextlib, os, tempfile
def f(path):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as h:
            h.write("x")
        os.replace(tmp, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
'''

_DATACLASS_REPLACE_BARE = '''
from dataclasses import replace
def f(rec, txt):
    return replace(rec, text=txt)
'''

# THE FORM THAT ACTUALLY OCCURS HERE. The bare form above is excluded by the
# "must be an attribute call" test; this one is an attribute call with exactly
# one positional arg, so neither that test nor the arity test separates it.
# MEASURED: 4 live sites, and all four reached a shipped baseline as "atomic
# writes" with temp == "dataclasses" while a test pinned only the bare form.
_DATACLASS_REPLACE_ATTR = '''
import dataclasses
def f(rec, txt):
    return dataclasses.replace(rec, text=txt)
'''

_SHUTIL_MOVE_LEAKS = '''
import shutil, tempfile
def f(path):
    fd, tmp = tempfile.mkstemp()
    try:
        shutil.move(tmp, path)
    except OSError:
        pass
'''

# The shapes that cost 16 false-positive rows. In each the first operand is
# DURABLE, so "unlink the temp" would destroy live data.
_MOVE_ASIDE = '''
import os
def f(target_path, aside):
    try:
        os.replace(target_path, aside)
    except OSError:
        pass
'''

_CLAIM_BY_RENAME = '''
import os
def f(pending, claimed):
    try:
        os.rename(pending, claimed)
    except OSError:
        return None
'''

_ROTATE = '''
def f(log_path, rotated):
    try:
        log_path.rename(rotated)
    except OSError:
        pass
'''

# The two-hop shape a one-hop "born here" check silently loses.
_TWO_HOP_TEMP = '''
import os, tempfile
from pathlib import Path
def f(out_path, out_dir):
    try:
        with tempfile.NamedTemporaryFile("w", dir=str(out_dir), delete=False) as tmp:
            tmp.write("x")
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, out_path)
    except Exception:
        pass
'''

_STR_REPLACE = '''
def f(tmpl, model):
    return tmpl.replace("{model}", model)
'''

# `tmp_path` is created HERE, not taken as a parameter: the guard only claims a
# site whose operand is a temp born in the same function, so a fixture that
# received one would (correctly) not be classified at all.
_PATH_REPLACE_CLEAN = '''
import contextlib
def f(dest):
    tmp_path = dest.with_suffix(".tmp")
    try:
        tmp_path.replace(dest)
    except OSError:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
'''


def test_a_clean_atomic_write_is_not_flagged():
    assert _verdicts(_CLEAN) == ["CLEANS_UP"]


@pytest.mark.parametrize(
    "src", [_DATACLASS_REPLACE_BARE, _DATACLASS_REPLACE_ATTR], ids=["bare", "attribute"]
)
def test_neither_dataclasses_replace_form_is_a_filesystem_write(src):
    """BOTH forms, because pinning only the bare one is how the attribute form
    shipped in a baseline as four "atomic writes".

    This asserts the OUTCOME, and two independent rules now produce it: the
    explicit `owner in ("dataclasses", "dc")` exclusion, and the born-here rule
    (a dataclass instance is never bound to a temp-maker). MEASURED by mutation:
    deleting the explicit rule leaves this green, because born-here catches it
    anyway. Said plainly rather than left for the next reader to discover -- the
    explicit rule is cheap belt-and-braces over a known 4-site class, not the
    load-bearing mechanism."""
    assert _verdicts(src) == []


def test_shutil_move_is_not_an_escape_hatch():
    """Same operation, different verb. It was invisible, and one live call sat
    three lines above a baselined leak in the same function."""
    assert _verdicts(_SHUTIL_MOVE_LEAKS) == ["LEAKS"]


@pytest.mark.parametrize(
    "src", [_MOVE_ASIDE, _CLAIM_BY_RENAME, _ROTATE],
    ids=["move-aside", "claim-by-rename", "rotate"],
)
def test_a_durable_first_operand_is_not_an_atomic_write(src):
    """THE 16-row lesson. `rename`/`replace` also covers move-aside, claim,
    rotate and quarantine, where the first operand is durable and the correct fix
    is NOTHING. Flagging these put booby-trapped rows in a debt ledger whose own
    remediation text said to unlink the operand -- which would have deleted a
    live credential, a user's file, and pending telemetry."""
    assert _verdicts(src) == []


def test_a_temp_bound_two_hops_away_is_still_a_temp():
    """RECALL control for the fix above. Requiring the temp to be born here
    initially LOST a real leak whose temp comes from `with NamedTemporaryFile(...)
    as tmp` and is renamed via `tmp_path = Path(tmp.name)`. Precision measured
    without recall is half a measurement."""
    assert _verdicts(_TWO_HOP_TEMP) == ["LEAKS"]


def test_two_same_named_methods_do_not_share_a_baseline_key():
    """A bare function name lets a clean `A._write` absorb a leaking `C._write`,
    so a genuinely new leak reads as already-baselined and the guard exits 0."""
    # The temps are CREATED in each method, not received as parameters. An
    # earlier version of this fixture took `tmp` as an argument, so the
    # born-here rule excluded both sites, `analyse_source` returned [], and the
    # assertion below reduced to len(set()) == len([]) -- it passed with the
    # qualname mechanism deleted. Caught by mutation, not by running it.
    src = """
import os
class A:
    def _write(self, p):
        tmp = p.with_suffix(".tmp")
        try:
            os.replace(tmp, p)
        except OSError:
            os.unlink(tmp)
            raise
class B:
    def _write(self, p):
        tmp = p.with_suffix(".tmp")
        try:
            os.replace(tmp, p)
        except OSError:
            pass
"""
    rows = chk.analyse_source(src, "s.py")
    assert len(rows) == 2, f"fixture produced no sites to collide: {rows}"
    assert len({chk.key(r) for r in rows}) == len(rows), (
        f"key collision: {[chk.key(r) for r in rows]}"
    )


def test_str_replace_is_not_a_filesystem_write():
    assert _verdicts(_STR_REPLACE) == []


def test_the_temp_is_the_RECEIVER_in_the_path_replace_form():
    """`tmp.replace(dest)` puts the temp on the left of the dot and the
    DESTINATION in the argument. Reading args[0] as the temp -- which an earlier
    version did -- checks the wrong operand for cleanup, so a correctly-cleaned
    site reads as a leak and a leaking one can read as clean."""
    rows = chk.analyse_source(_PATH_REPLACE_CLEAN, "synthetic.py")
    assert [r["temp"] for r in rows] == ["tmp_path"]
    assert [r["verdict"] for r in rows] == ["CLEANS_UP"]


# ---------------------------------------------------------------------------
# ANCHORS. Synthetic shapes can drift from the code they model; these do not.
# ---------------------------------------------------------------------------

def _verdicts_for(rel: str) -> set[str]:
    src = (_REPO / rel).read_text(encoding="utf-8")
    return {r["verdict"] for r in chk.analyse_source(src, rel)}


@pytest.mark.parametrize(
    "rel",
    [
        "src/genesis/util/atomic.py",
        "src/genesis/cc/session_cache.py",
    ],
)
def test_known_clean_implementations_read_clean(rel):
    """NEGATIVE control against the real tree. `atomic.py` is the reference
    implementation; its cleanup branch is verified by test_atomic.py."""
    assert "CLEANS_UP" in _verdicts_for(rel)
    assert "LEAKS" not in _verdicts_for(rel)


@pytest.mark.parametrize(
    "rel",
    [
        "src/genesis/cc/fallback_state.py",
        "src/genesis/sentinel/state.py",
        "src/genesis/session_awareness/statefiles.py",
    ],
)
def test_known_leaking_sites_still_read_dirty(rel):
    """POSITIVE control against the real tree. If a fix lands, this test failing
    is the CORRECT signal: remove the row here and from the baseline together."""
    assert _verdicts_for(rel) & {"LEAKS", "NO_HANDLER"}, (
        f"{rel} no longer reads dirty -- if that is a real fix, drop it from this "
        "control list and from config/atomic_write_baseline.json"
    )


# ---------------------------------------------------------------------------
# THE GUARD AS A PROCESS.
# ---------------------------------------------------------------------------

def test_the_repo_passes_its_own_baseline():
    proc = subprocess.run(
        [sys.executable, str(_GUARD)], capture_output=True, text=True, timeout=300
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_baseline_has_no_stale_rows():
    """A landed fix must not leave a row behind, or the ledger stops shrinking
    and the next reader cannot tell debt from noise."""
    proc = subprocess.run(
        [sys.executable, str(_GUARD)], capture_output=True, text=True, timeout=300
    )
    assert "no longer match" not in proc.stdout, proc.stdout


def test_every_baseline_row_is_well_formed():
    doc = json.loads(
        (_REPO / "config" / "atomic_write_baseline.json").read_text(encoding="utf-8")
    )
    assert doc["known"], "an empty baseline means the class is closed -- delete it"
    for row in doc["known"]:
        rel, _, rest = row.partition("::")
        func, _, temp = rest.partition("::")
        assert rel and func and temp, f"malformed baseline row: {row!r}"
        assert (_REPO / rel).exists(), f"baseline names a file that is gone: {rel}"


def _drive_main(tmp_path, monkeypatch, capsys, rows, baseline):
    """Run main() against a controlled scan result.

    main()'s decision logic has four outcomes (floor, stale, new, clean) and two
    of them fire on overlapping inputs, so an end-to-end test can return the
    right exit code via the WRONG branch. MEASURED by mutation: an earlier
    empty-repo test returned 1 through the stale path and stayed green with the
    floor deleted. Driving the inputs directly is what separates them.
    """
    monkeypatch.setattr(chk, "REPO", tmp_path)
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "atomic_write_baseline.json").write_text(
        json.dumps({"known": baseline}), encoding="utf-8"
    )
    monkeypatch.setattr(chk, "scan", lambda repo: (rows, []))
    code = chk.main()
    out = capsys.readouterr()
    return code, out.out + out.err


def _row(file="a.py", func="f", temp="tmp", verdict="LEAKS"):
    return {"file": file, "line": 1, "func": func, "temp": temp, "verdict": verdict}


def test_a_scan_that_sees_fewer_sites_than_its_ledger_fails(tmp_path, monkeypatch, capsys):
    """Path.rglob on a missing directory yields nothing rather than raising, so a
    mis-rooted scan produced ([], []) -> no dirty sites -> exit 0: a green check
    for a run that examined nothing. Asserted on the FLOOR's own message, because
    the stale branch also returns 1 on this input."""
    code, text = _drive_main(
        tmp_path, monkeypatch, capsys, rows=[], baseline=["a.py::f::tmp", "b.py::g::tmp"]
    )
    assert code == 1
    assert "scanned only 0 sites against a 2-row baseline" in text


def test_a_stale_baseline_row_fails_rather_than_merely_printing(tmp_path, monkeypatch, capsys):
    """A landed fix must drop its row in the same change, or the ledger stops
    shrinking. The guard used to only PRINT this and return 0."""
    rows = [_row(file="a.py", func="f", temp="tmp"),
            _row(file="b.py", func="g", temp="tmp", verdict="CLEANS_UP")]
    code, text = _drive_main(
        tmp_path, monkeypatch, capsys,
        rows=rows, baseline=["a.py::f::tmp", "b.py::g::tmp"],
    )
    assert code == 1, "a stale row must FAIL, not just print"
    assert "b.py::g::tmp" in text


def test_a_new_dirty_site_fails(tmp_path, monkeypatch, capsys):
    code, text = _drive_main(
        tmp_path, monkeypatch, capsys, rows=[_row()], baseline=[]
    )
    assert code == 1
    assert "NEW" in text


def test_the_remediation_warns_before_it_advises_unlinking(tmp_path, monkeypatch, capsys):
    """The text that made 16 false-positive rows dangerous. It must lead with
    'confirm the operand is a temp', because applied blindly to a move-aside the
    advice deletes live data."""
    _, text = _drive_main(tmp_path, monkeypatch, capsys, rows=[_row()], baseline=[])
    assert "FIRST confirm" in text
    assert text.index("FIRST confirm") < text.index("unlink the temp")
    assert "the correct fix is NOTHING" in text


def test_a_matching_baseline_passes(tmp_path, monkeypatch, capsys):
    code, _ = _drive_main(
        tmp_path, monkeypatch, capsys, rows=[_row()], baseline=["a.py::f::tmp"]
    )
    assert code == 0


def test_an_unreadable_file_fails_closed(tmp_path):
    """A guard that cannot parse a file must not report it clean. Asserted on the
    scanner rather than the CLI so no unparseable file has to be committed."""
    bad = tmp_path / "src" / "broken.py"
    bad.parent.mkdir(parents=True)
    bad.write_text("def f(:\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    rows, errors = chk.scan(tmp_path)
    assert errors, "a syntax error must be reported, not skipped"
    assert rows == []
