"""Frozen-clock guard (scripts/check_frozen_clock.py).

The guard exists because a test that captures a wall clock at IMPORT time gives the
whole suite's runtime to burn its safety margin, and the resulting failure only shows
up on slow runners. Two prior sweeps declared this class closed and were wrong, so
this suite IS the thing keeping it closed — it is written to be mutation-hostile:

  * every shape the guard must catch is paired with the safe shape it must NOT catch,
    because a detection-only suite and an inert guard produce the same "no false
    positives" number;
  * the compound module-level statements (if/try/with/for/while/comprehension) are
    covered as POSITIVES, not only as negatives — a negative passes trivially on a
    scanner that never descends, so negatives alone let that whole branch be deleted
    while the suite stays green;
  * both fail-closed paths (a file that cannot be parsed, and one that cannot be READ)
    are asserted, because the failure mode there is printing CLEAN over a live bomb.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "check_frozen_clock", _REPO_ROOT / "scripts" / "check_frozen_clock.py",
)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

_HEADER = "import time\n\nimport pytest\nfrom datetime import UTC, datetime\n\n"

# (shape label, frozen source, the SAFE counterpart that must stay silent)
SHAPES = [
    (
        "module-body",
        "NOW = datetime.now(UTC)\n",
        "def test_x():\n    now = datetime.now(UTC)\n    assert now\n",
    ),
    (
        "class-body",
        "class TestThing:\n    NOW = datetime.now(UTC)\n",
        "class TestThing:\n    def test_x(self):\n        assert datetime.now(UTC)\n",
    ),
    (
        "default-arg",
        "def helper(seed=datetime.now(UTC)):\n    return seed\n",
        "def helper(seed=None):\n    return seed or datetime.now(UTC)\n",
    ),
    (
        "decorator-arg",
        '@pytest.mark.parametrize("t", [datetime.now(UTC)])\ndef test_x(t):\n    assert t\n',
        '@pytest.mark.parametrize("d", [1])\ndef test_x(d):\n    assert datetime.now(UTC)\n',
    ),
    (
        "scoped-fixture",
        '@pytest.fixture(scope="session")\ndef seed():\n    return datetime.now(UTC)\n',
        "@pytest.fixture\ndef seed():\n    return datetime.now(UTC)\n",
    ),
]

# Module-level COMPOUND statements. These reach the scanner's catch-all descent, which
# nothing else here exercises as a positive — verify-RED showed that branch could be
# deleted outright with the rest of the suite still green.
COMPOUND_FROZEN = [
    ("if-body", "if True:\n    NOW = datetime.now(UTC)\n"),
    ("try-body", "try:\n    NOW = datetime.now(UTC)\nexcept Exception:\n    NOW = None\n"),
    (
        "with-body",
        "import contextlib\nwith contextlib.suppress(Exception):\n"
        "    NOW = datetime.now(UTC)\n",
    ),
    ("for-body", "S = []\nfor _ in range(3):\n    S.append(datetime.now(UTC))\n"),
    ("while-body", "NOW = None\nwhile NOW is None:\n    NOW = datetime.now(UTC)\n"),
    ("list-comprehension", "S = [datetime.now(UTC) for _ in range(3)]\n"),
    ("walrus", "if (n := datetime.now(UTC)):\n    NOW = n\n"),
    ("nested-class", "class A:\n    class B:\n        NOW = datetime.now(UTC)\n"),
    ("kwonly-default", "def f(*, t=datetime.now(UTC)):\n    return t\n"),
    (
        "metaclass-keyword",
        "def mk(x):\n    return type\n\n\nclass C(metaclass=mk(datetime.now(UTC))):\n    pass\n",
    ),
]

# Deferred bodies that sit INSIDE an import-time statement. The top-level `def` cases
# in SHAPES never reach the scanner at all, so only these exercise the rule that a
# function/lambda/generator body is skipped — verify-RED proved the others could not.
DEFERRED_SAFE = [
    ("module-level lambda", "SEED = lambda: datetime.now(UTC)\n"),
    ("def nested in an if", "if True:\n    def helper():\n        return datetime.now(UTC)\n"),
    (
        "def nested in a try",
        "try:\n    def helper():\n        return datetime.now(UTC)\n"
        "except Exception:\n    helper = None\n",
    ),
    ("lambda in a class body", "class TestThing:\n    seed = lambda self: datetime.now(UTC)\n"),
    (
        "nested def in a scoped fixture",
        '@pytest.fixture(scope="session")\ndef seed():\n'
        "    def _later():\n        return datetime.now(UTC)\n    return _later\n",
    ),
    ("generator expression is lazy", "G = (datetime.now(UTC) for _ in range(3))\n"),
    (
        "skipif is import-time by contract",
        '@pytest.mark.skipif(datetime.now(UTC).year > 3000, reason="never")\n'
        "def test_x():\n    pass\n",
    ),
]


def _write(tmp_path: Path, body: str) -> Path:
    f = tmp_path / "test_planted.py"
    f.write_text(_HEADER + body)
    return f


@pytest.mark.parametrize("shape,frozen,_safe", SHAPES, ids=[s[0] for s in SHAPES])
def test_flags_each_frozen_shape(tmp_path, shape, frozen, _safe):
    """POSITIVE control: every import-time position the guard claims to cover."""
    _write(tmp_path, frozen)
    violations = check.scan(tmp_path)
    assert len(violations) == 1, f"{shape} not flagged: {violations}"
    assert violations[0][2] == shape


@pytest.mark.parametrize("shape,_frozen,safe", SHAPES, ids=[s[0] for s in SHAPES])
def test_ignores_the_safe_counterpart(tmp_path, shape, _frozen, safe):
    """NEGATIVE control: the call-time form is the FIX, and must never be flagged."""
    _write(tmp_path, safe)
    assert check.scan(tmp_path) == [], f"{shape} safe form wrongly flagged"


@pytest.mark.parametrize("label,body", COMPOUND_FROZEN, ids=[c[0] for c in COMPOUND_FROZEN])
def test_descends_into_import_time_compound_statements(tmp_path, label, body):
    """A clock does not stop being frozen because it sits inside an `if` or a `try`."""
    _write(tmp_path, body)
    assert len(check.scan(tmp_path)) == 1, f"{label} not flagged"


@pytest.mark.parametrize("label,body", DEFERRED_SAFE, ids=[d[0] for d in DEFERRED_SAFE])
def test_deferred_body_inside_an_import_time_statement_is_safe(tmp_path, label, body):
    """A callable's BODY runs when called, however early the statement holding it runs."""
    _write(tmp_path, body)
    assert check.scan(tmp_path) == [], f"{label} wrongly flagged"


@pytest.mark.parametrize(
    "body",
    [
        "NOW = datetime.now(UTC)  # frozen-clock-ok: 34d margin vs a 35d window\n",
        "# frozen-clock-ok: 34d margin vs a 35d window\nNOW = datetime.now(UTC)\n",
        "# unbounded: two seeds only\n# frozen-clock-ok: 11d margin vs a 14d window\n"
        "NOW = datetime.now(UTC)\n",
        "# frozen-clock-ok: 11d margin vs a 14d window\n_X = (\n    datetime.now(UTC)\n)\n",
        "NOW = datetime.now(UTC)  # frozen-clock-ok: unbounded, nothing ages this seed\n",
    ],
    ids=["same-line", "directly-above", "in-a-comment-block", "wrapped-statement", "unbounded"],
)
def test_opt_out_is_honoured(tmp_path, body):
    _write(tmp_path, body)
    assert check.scan(tmp_path) == []


@pytest.mark.parametrize(
    "marker",
    ["# frozen-clock-ok:", "# frozen-clock-ok:   ", "# frozen-clock-ok"],
    ids=["empty-reason", "whitespace-reason", "no-colon"],
)
def test_opt_out_without_a_stated_reason_does_not_count(tmp_path, marker):
    """The margin is the point — a bare wave-off must not silence the guard."""
    _write(tmp_path, f"NOW = datetime.now(UTC)  {marker}\n")
    assert len(check.scan(tmp_path)) == 1


def test_opt_out_reason_must_state_a_magnitude(tmp_path):
    """A reason with no number and no 'unbounded' records no measurement."""
    _write(tmp_path, "NOW = datetime.now(UTC)  # frozen-clock-ok: it is fine, honest\n")
    assert len(check.scan(tmp_path)) == 1


def test_marker_inside_a_string_literal_does_not_waive(tmp_path):
    """Only real comment tokens count, so prose about the guard cannot disarm it."""
    _write(tmp_path, 'NOW = datetime.now(UTC); MSG = "# frozen-clock-ok: 5d bogus"\n')
    assert len(check.scan(tmp_path)) == 1


def test_one_marker_cannot_waive_two_sites(tmp_path):
    """One recorded measurement per site: two clocks in a span need two waivers."""
    _write(tmp_path, "A = datetime.now(UTC); B = time.time()  # frozen-clock-ok: 5d one reason\n")
    assert len(check.scan(tmp_path)) == 1
    _write(
        tmp_path,
        "# frozen-clock-ok: 5d for A\n# frozen-clock-ok: 5d for B\n"
        "A = datetime.now(UTC); B = time.time()\n",
    )
    assert check.scan(tmp_path) == []


def test_opt_out_does_not_reach_across_a_blank_line(tmp_path):
    """Only the CONTIGUOUS comment block counts, so a stray marker cannot drift down."""
    _write(tmp_path, "# frozen-clock-ok: 9d elsewhere\n\nNOW = datetime.now(UTC)\n")
    assert len(check.scan(tmp_path)) == 1


def test_two_clocks_on_one_line_are_two_violations(tmp_path):
    """Deduping by name alone would under-report and under-charge the waiver rule."""
    _write(tmp_path, "A, B = datetime.now(UTC), datetime.now(UTC)\n")
    assert len(check.scan(tmp_path)) == 2


@pytest.mark.parametrize(
    "expr",
    [
        "datetime.datetime.now(UTC)",
        "datetime.utcnow()",
        "datetime.today()",
        "time.monotonic()",
        "time.perf_counter()",
        "time.time()",
        "time.localtime()",
        "time.gmtime()",
        "time.clock_gettime(0)",
    ],
)
def test_recognises_dotted_clock_variants(tmp_path, expr):
    _write(tmp_path, f"SEED = {expr}\n")
    assert len(check.scan(tmp_path)) == 1, f"{expr} not recognised"


@pytest.mark.parametrize(
    "decorator",
    ['@pytest.fixture(scope=_S)', '@pytest.fixture(**_KW)'],
    ids=["scope-is-a-variable", "scope-via-kwargs"],
)
def test_unprovable_fixture_scope_fails_closed(tmp_path, decorator):
    """The guard cannot prove the scope is narrow, so it must not assume it is."""
    _write(tmp_path, f'_S = "session"\n_KW = {{}}\n{decorator}\ndef f():\n    return datetime.now(UTC)\n')
    assert len(check.scan(tmp_path)) == 1


def test_unparseable_file_is_reported_not_silently_skipped(tmp_path):
    """Fail CLOSED: a file the guard cannot parse is a gap, never a silent pass."""
    (tmp_path / "test_broken.py").write_text("def oops(:\n")
    violations = check.scan(tmp_path)
    assert len(violations) == 1
    assert violations[0][2] == "unparseable"


@pytest.mark.parametrize("mode", ["non-utf8", "unreadable"])
def test_file_that_cannot_be_read_is_reported_not_silently_skipped(tmp_path, mode):
    """The same fail-closed rule for READ errors — this branch printed CLEAN over a bomb."""
    f = tmp_path / "test_unreadable.py"
    if mode == "non-utf8":
        f.write_bytes("NOW = datetime.now(UTC)  # caf\xe9\n".encode("latin-1"))
    else:
        f.write_text("NOW = datetime.now(UTC)\n")
        os.chmod(f, 0o000)
    try:
        violations = check.scan(tmp_path)
        assert len(violations) == 1
        assert violations[0][2] == "unreadable"
    finally:
        os.chmod(f, 0o644)


def test_repo_tests_tree_is_clean():
    """The real tree must stay clean — this is what actually blocks a regression."""
    violations = check.scan(_REPO_ROOT / "tests")
    assert violations == [], f"frozen clocks reintroduced: {violations}"


def test_main_returns_nonzero_on_a_violation(tmp_path, monkeypatch, capsys):
    _write(tmp_path, "NOW = datetime.now(UTC)\n")
    monkeypatch.setattr(check, "SCAN_ROOT", tmp_path)
    assert check.main() == 1
    assert "frozen-clock" in capsys.readouterr().out


def test_main_returns_zero_on_a_clean_tree(tmp_path, monkeypatch, capsys):
    """Negative control: a main() hardwired to 1 would pass the test above alone."""
    _write(tmp_path, "def test_x():\n    assert datetime.now(UTC)\n")
    monkeypatch.setattr(check, "SCAN_ROOT", tmp_path)
    assert check.main() == 0
    assert "CLEAN" in capsys.readouterr().out
