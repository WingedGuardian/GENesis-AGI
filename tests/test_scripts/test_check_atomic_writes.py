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


_ATTR_TEMP_DURABLE_SIBLING = """
class Store:
    def rotate(self, path, dest):
        self.staging = path.with_suffix(".tmp")
        self.live_file = path
        # DURABLE. The real file is being moved aside; the correct remediation
        # is nothing at all. It shares only the `self` root with the temp above.
        self.live_file.replace(dest)
"""

_ATTR_TEMP_ITSELF_LEAKS = """
class Store:
    def commit(self, path, dest):
        self.staging = path.with_suffix(".tmp")
        try:
            self.staging.write_text("x")
            self.staging.replace(dest)
        except OSError:
            return
"""


_INTERPOLATED_TEMP_LEAKS = """
import os


def restore(target, data):
    tmp = target.with_name(f".{target.name}.restore-tmp-{os.getpid()}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except OSError:
        return
"""

_INTERPOLATED_NEAR_MISS = """
import os


def publish(target, data):
    # `whats-new-` is ordinary prose that happens to end in a scratch stem. It is
    # NOT a temp, and the relaxed f-string rule must not claim it.
    page = target.with_name(f"whats-new-{target.name}")
    try:
        page.write_bytes(data)
        os.replace(page, target)
    except OSError:
        return
"""


def test_a_temp_marker_followed_by_interpolation_is_still_a_temp():
    """The end-anchored suffix test made a whole site INVISIBLE, not merely
    misjudged. `f".{name}.restore-tmp-{os.getpid()}"` puts the marker before a
    unique component, so `.endswith(_TEMP_SUFFIXES)` failed and the site produced
    no row at all -- which is why the error surfaced in the published DENOMINATOR
    (58 sites, actually 59) rather than in any verdict. Its live instance is
    guardian/cred_integrity.py restore_file, and it reads CLEANS_UP."""
    assert _verdicts(_INTERPOLATED_TEMP_LEAKS) == ["LEAKS"]


def test_prose_ending_in_a_scratch_stem_is_not_a_temp():
    """PRECISION control, and the reason the relaxation is scoped to f-strings
    with a separator before the stem. Matching a stem anywhere would admit
    `.partition`, `foo.parts` and this fixture -- and a false temp is a false
    LEAK row whose printed remediation says to unlink a durable file."""
    assert _verdicts(_INTERPOLATED_NEAR_MISS) == []


_ATTR_TEMP_VIA_DERIVED_PATH = """
import os
import tempfile


class Store:
    def commit(self, dest):
        self.handle = tempfile.NamedTemporaryFile(delete=False)
        try:
            self.handle.write(b"x")
            os.replace(self.handle.name, dest)
        except OSError:
            return
"""


def test_an_attribute_temp_reached_through_a_derived_path_is_still_a_temp():
    """The false CLEAN that binding whole paths created, and the reason the
    prefix rung exists.

    `self.handle` is bound to a temp maker; the rename names `self.handle.name`.
    Neither the full path nor the bare root (`self`, deliberately no longer
    bound) matches, so before the prefix rung this produced NO ROW -- invisible
    and absent from the debt ledger, which `_unlinks` calls strictly worse than a
    false flag. MEASURED across the change: LEAKS before the binding fix, [] with
    the binding fix alone, LEAKS again with the prefix rung.

    This is also the only test that pins the root fallback the comment above
    `_born_in`'s return insists must stay: an audit showed reducing that line to
    `temp_expr in temps` alone left the whole suite green."""
    assert _verdicts(_ATTR_TEMP_VIA_DERIVED_PATH) == ["LEAKS"]


_KWARG_MOVE_LEAKS = """
import os
import tempfile


def commit(dest, data):
    fd, tmp = tempfile.mkstemp()
    try:
        os.write(fd, data)
        os.close(fd)
        os.replace(src=tmp, dst=dest)
    except OSError:
        return
"""

_IMPORTED_MOVE_LEAKS = """
import tempfile
from os import replace


def commit(dest, data):
    fd, tmp = tempfile.mkstemp()
    try:
        replace(tmp, dest)
    except OSError:
        return
"""

_IMPORTED_DATACLASS_REPLACE = """
import tempfile
from dataclasses import replace


def bump(dest):
    fd, tmp = tempfile.mkstemp()
    try:
        return replace(tmp, dest)
    except OSError:
        return None
"""

_MULTI_ARG_PATH_JOIN = """
import os
import shutil
import tempfile
from pathlib import Path


def commit(dest):
    tmpdir = tempfile.mkdtemp()
    try:
        Path(tmpdir, "payload").replace(dest)
    except OSError:
        shutil.rmtree(tmpdir)
        os.unlink(tmpdir)
"""


_RECEIVER_KWARG_MOVE = """
import tempfile
from pathlib import Path


def commit(dest, data):
    fd, tmp = tempfile.mkstemp()
    try:
        Path(tmp).write_bytes(data)
        Path(tmp).replace(target=dest)
    except OSError:
        return
"""

_SHADOWED_IMPORT_NAME = """
import tempfile
from os import replace


def apply(mapping, dest):
    # `replace` here is a LOCAL, not the os function. Matching the imported name
    # without scope analysis produced a row for it.
    replace = mapping["fn"]
    fd, tmp = tempfile.mkstemp()
    try:
        return replace(tmp, dest)
    except OSError:
        return None
"""


def test_a_receiver_form_keyword_move_is_not_invisible():
    """`Path(tmp).replace(target=dest)` has zero positional args.

    The keyword-aware resolution was added, but the ARITY TEST that runs before
    it still counted positional args only -- so this form was dropped one line
    earlier and the resolver's `"target"` branch was dead code. An audit found
    the resolver's own docstring promising resolution happens "before any arity
    test", which was false for exactly this branch."""
    assert _verdicts(_RECEIVER_KWARG_MOVE) == ["LEAKS"]


def test_a_locally_rebound_import_name_is_not_a_move():
    """PRECISION control for the directly-imported allowlist.

    The allowlist matched by NAME with no scope analysis, so a local
    `replace = mapping["fn"]`, a parameter called `move`, or a nested
    `def replace` all produced rows in a file that happened to import the real
    one. Latent -- no file in this tree imports these names directly -- but a
    false LEAK row carries this guard's "unlink the temp" remediation, so it is
    a booby-trapped work item rather than noise."""
    assert _verdicts(_SHADOWED_IMPORT_NAME) == []


def test_a_keyword_only_move_is_not_invisible():
    """`os.replace(src=..., dst=...)` has NO positional args, so an arity test
    read it as "not a filesystem move" and the site produced no row -- silent,
    and invisible in every published count. Operands are now resolved by
    position OR keyword before any arity check."""
    assert _verdicts(_KWARG_MOVE_LEAKS) == ["LEAKS"]


def test_a_directly_imported_move_is_not_invisible():
    """`from os import replace` binds a bare NAME, which the attribute-call rule
    excluded along with `dataclasses.replace`. The names an os/shutil import
    actually binds are now allowlisted, so the real move is seen."""
    assert _verdicts(_IMPORTED_MOVE_LEAKS) == ["LEAKS"]


def test_a_directly_imported_dataclasses_replace_is_still_not_a_move():
    """PRECISION control, and the reason the attribute rule is narrowed rather
    than dropped. `from dataclasses import replace` binds the SAME bare name and
    touches no filesystem; admitting it was the largest false-positive class this
    guard ever had (16 of 49 rows on the first baseline).

    THE FIXTURE IS DELIBERATELY SHAPED TO MAKE THE ALLOWLIST LOAD-BEARING, and
    the first version was not. It called `replace(record, path=dest)`, which the
    guard drops for reasons that have nothing to do with the import: the
    destination never resolves (`path=` is not `dst=`) and `record` is a
    parameter, so `_born_in` rejects it. An audit deleted the allowlist entirely
    and this test stayed green -- a precision control that survives deletion of
    the mechanism it names is decoration, which is the exact defect this PR
    exists to remove, committed in the test written to prevent it.

    So the call is now `replace(tmp, dest)` with `tmp` from `mkstemp` and both
    operands positional -- structurally identical to a real `os.replace` leak.
    The ONLY thing standing between it and a LEAKS row is that `replace` was
    bound by `dataclasses`, not by `os`."""
    import ast as _ast

    # The barrier itself, asserted rather than inferred from the verdict.
    assert chk._directly_imported_moves(_ast.parse(_IMPORTED_DATACLASS_REPLACE)) == {}
    assert _verdicts(_IMPORTED_DATACLASS_REPLACE) == []


def test_a_multi_argument_path_join_is_not_reduced_to_its_directory():
    """A multi-arg `Path()` is a JOIN, not a transparent wrapper.

    Stripping it to its first argument recorded `tmpdir` as the temp, so a
    handler removing `tmpdir` was credited with cleaning up a CHILD it never
    unlinked and the site read CLEANS_UP -- a false clean, which hides the leak
    AND keeps it out of the debt ledger.

    Stated precisely, because this is an improvement rather than a full fix: the
    join is now left intact, so the site is UNMATCHED rather than wrongly
    cleared. `_born_in` looks up the temp expression and a joined path is not a
    name it has seen bound, which is the documented limit on complex operands.
    Unmatched is the safe direction; the test asserts the false CLEANS_UP is gone
    rather than claiming a verdict the guard does not produce."""
    assert "CLEANS_UP" not in _verdicts(_MULTI_ARG_PATH_JOIN)


def test_an_attribute_temp_does_not_make_its_SIBLINGS_temps():
    """FIXTURE-PINNED, because this fix is behaviourally NULL on this repo today.

    `_bound_names` walked to every `ast.Name` beneath an assignment target, so
    `self.staging = path.with_suffix(".tmp")` recorded **`self`** as a temp. Any
    later `self.<anything>.replace(dst)` then shared that root and was reported
    as a leak -- carrying this guard's own "unlink the temp" remediation, aimed
    at a durable file. That is the same durable-operand trap the born-here rule
    exists to prevent, re-entered through the BINDING side rather than the
    operand side.

    MEASURED before landing: the live tree reads 59 sites / 28 clean / 31 dirty
    both with and without the fix, so no current file exercises this shape. A
    scan-count control would therefore have proved nothing, and only a synthetic
    fixture can fail if the walk-to-every-Name behaviour returns.

    The fixture binds and uses inside ONE method on purpose. A first draft put
    the binding in `__init__` and the move in another method, which the guard
    does not span by design -- so it returned [] with and without the fix, and
    would have passed as a control while measuring nothing at all."""
    assert _verdicts(_ATTR_TEMP_DURABLE_SIBLING) == []


def test_an_attribute_temp_is_still_a_temp():
    """RECALL control for the test above -- the half that a precision-only fix
    silently breaks. Narrowing the binding must not stop the attribute path
    ITSELF being recognised, or the fix trades a false flag for a false clean,
    which is strictly worse: the leak becomes invisible AND leaves the ledger."""
    assert _verdicts(_ATTR_TEMP_ITSELF_LEAKS) == ["LEAKS"]


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


# --------------------------------------------------------------------------
# IDENTITY, NOT SUBSTRING. Raised by CodeRabbit on the PR; the false-CLEAN
# direction is the dangerous one, because a leak that reads clean is both
# invisible AND excluded from the debt ledger, so nothing revisits it.
# --------------------------------------------------------------------------

_UNLINKS_A_DIFFERENT_FILE = '''
import os, tempfile
def f(path):
    fd, tmp = tempfile.mkstemp()
    try:
        os.replace(tmp, path)
    except OSError:
        os.unlink(tmp_backup)
        raise
'''

_UNLINKS_THE_TEMP = '''
import os, tempfile
def f(path):
    fd, tmp = tempfile.mkstemp()
    try:
        os.replace(tmp, path)
    except OSError:
        os.unlink(tmp)
        raise
'''

_UNLINKS_WRAPPED = '''
import os, tempfile
from pathlib import Path
def f(path):
    fd, tmp = tempfile.mkstemp()
    try:
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink()
        raise
'''

#: The real shape from src/genesis/autonomy/executor/engine.py -- the cleanup
#: REBUILDS the temp path from a local instead of reusing the variable. Genuinely
#: clean, and unresolvable without recursive alias substitution.
_UNLINKS_RECONSTRUCTED = '''
import contextlib
from pathlib import Path
def f(plan_path, content):
    path = Path(plan_path).expanduser()
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content)
        tmp.rename(path)
    except OSError:
        with contextlib.suppress(OSError):
            Path(plan_path).expanduser().with_suffix(".tmp").unlink(missing_ok=True)
'''


@pytest.mark.parametrize(
    "src,expected",
    [
        (_UNLINKS_A_DIFFERENT_FILE, "LEAKS"),
        (_UNLINKS_THE_TEMP, "CLEANS_UP"),
        (_UNLINKS_WRAPPED, "CLEANS_UP"),
        (_UNLINKS_RECONSTRUCTED, "CLEANS_UP"),
    ],
    ids=["different-file", "the-temp", "wrapped", "reconstructed"],
)
def test_cleanup_is_credited_by_identity_not_by_substring(src, expected):
    """`temp_expr in args` credited `os.unlink(tmp_backup)` as cleanup for temp
    `tmp` -- MEASURED, a leaking site read CLEANS_UP.

    The reconstructed case is why the fix is alias RESOLUTION rather than a
    stricter string test: identity alone turns that false CLEAN into a false
    FLAG, which is better and still wrong. It is the real shape from
    autonomy/executor/engine.py, whose cleanup rebuilds the path from a local.
    """
    assert [r["verdict"] for r in chk.analyse_source(src, "s.py")] == [expected]


def test_a_name_that_merely_CONTAINS_a_temp_name_is_not_that_temp():
    """The `_born_in` half of the same class: propagation walked the RHS TEXT, so
    any name containing a known temp's name counted as derived from it."""
    src = '''
import os, tempfile
def f(path):
    fd, tmp = tempfile.mkstemp()
    tmp_unrelated_listing = compute_something_else()
    try:
        os.replace(tmp_unrelated_listing, path)
    except OSError:
        pass
'''
    # The renamed operand is NOT a temp this function created, so the site is
    # not this guard's business at all.
    assert chk.analyse_source(src, "s.py") == []


def test_a_suffix_that_merely_CONTAINS_tmp_is_not_a_temp_suffix():
    """`.tmpl` contains `.tmp` and is a TEMPLATE, not a scratch file.

    The suffix test is anchored to the END of a string literal for this reason.
    Matching anywhere in the literal would classify every `.tmpl` write as a temp
    -- and this repo really does write those (systemd `.service.template`), so
    the guard would start flagging template renders as leaked temps.
    """
    src = '''
import os
def f(path):
    rendered = path.with_suffix(".tmpl")
    try:
        os.replace(rendered, path)
    except OSError:
        pass
'''
    assert chk.analyse_source(src, "s.py") == []


def test_a_real_temp_suffix_at_the_END_still_counts():
    """The other direction, so the anchoring cannot be tightened into blindness."""
    src = '''
import os
def f(path):
    scratch = path.with_suffix(".tmp")
    try:
        os.replace(scratch, path)
    except OSError:
        pass
'''
    assert [r["verdict"] for r in chk.analyse_source(src, "s.py")] == ["LEAKS"]


def test_a_wrapped_receiver_is_not_invisible():
    """`Path(tmp).replace(dest)` produced NO ROW AT ALL.

    The receiver kept its wrapper, so the temp expression was `Path(tmp_path)`;
    `_born_in` looks up a bare NAME and that is not one, so the site was silently
    DROPPED rather than judged. MEASURED: three real files in this repo use the
    style -- ego/config.py:98, mcp/health/settings.py:706, outreach/config.py:231
    -- and all three were invisible. They happen to clean up, so nothing was
    hidden today, but the guard's whole claim is that a new one cannot arrive
    silently, and in this house style it could.
    """
    leaks = '''
import os, tempfile
from pathlib import Path
def f(path):
    fd, tmp_path = tempfile.mkstemp()
    try:
        Path(tmp_path).replace(path)
    except OSError:
        pass
'''
    assert [r["verdict"] for r in chk.analyse_source(leaks, "s.py")] == ["LEAKS"]

    cleans = '''
import contextlib, os, tempfile
from pathlib import Path
def f(path):
    fd, tmp_path = tempfile.mkstemp()
    try:
        Path(tmp_path).replace(path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
'''
    assert [r["verdict"] for r in chk.analyse_source(cleans, "s.py")] == ["CLEANS_UP"]


def test_the_published_counts_match_the_tree():
    """The README, the docstring and the CI comment all quote measured counts.

    A number in permanent record that nobody re-derives is a claim wearing
    measurement's grammar -- and one of them was already wrong (28 files vs 27),
    caught by a cross-model reviewer rather than by me.
    """
    import json as _json

    rows, errors = chk.scan(_REPO)
    assert not errors
    dirty = [r for r in rows if r["verdict"] in ("LEAKS", "NO_HANDLER")]
    doc = _json.loads((_REPO / "config" / "atomic_write_baseline.json").read_text())
    readme = " ".join(doc["_README"])
    files = len({r["file"] for r in rows})
    assert f"{len(rows)} atomic-write sites across {files}" in readme
    assert f"{len(dirty)} across {len({r['file'] for r in dirty})} files are dirty" in readme
    assert len(doc["known"]) == len(dirty)

    # THE OTHER TWO SURFACES. This test's docstring named three from the day it
    # was written and its assertions read one, so a correction that landed on the
    # README and the guard docstring left the CI comment quoting the old
    # denominator -- a stale number in permanent record, under a test whose whole
    # purpose was to prevent exactly that. Normalise whitespace first: both
    # surfaces wrap their prose, so the counts straddle a newline.
    def _flat(text: str) -> str:
        return " ".join(text.split())

    guard = _flat((_REPO / "scripts" / "check_atomic_writes.py").read_text())
    assert f"{len(rows)} atomic-write sites across {files} files, {len(dirty)} of them dirty" in guard, (
        "the guard docstring quotes a count the tree no longer produces"
    )
    ci = _flat((_REPO / ".github" / "workflows" / "ci.yml").read_text())
    assert f"{len(rows)} sites, {len(dirty)} dirty" in ci, (
        "the CI job comment quotes a count the tree no longer produces"
    )
