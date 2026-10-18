"""Differentially check shell_parse against commands actually run by Bash.

Ran-but-unparsed shims fail; conservative over-reporting is only counted.
When a known gap is fixed, delete its row and re-enable its construct below.
"""

from __future__ import annotations

import hashlib
import os
import random
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS / "hooks"))
import shell_parse as sp  # noqa: E402

SHIMS = ("git", "rm", "ls", "cat", "gh", "curl", "python3", "pytest")
WORDS = (
    "push",
    "origin",
    "main",
    "-f",
    "--force",
    "a b",
    "x",
    "-rf",
    "/tmp/zz",
    "--no-verify",
    "$HOME",
    "*",
    "a'b",
    'q"r',
    "-m",
    "msg here",
    "\\$x",
    "-n",
)
# #3264/#3271/#3272: quote-bearing tokens in substitutions can hide commands.
SUBSTITUTION_WORDS = tuple(word for word in WORDS if "'" not in word and '"' not in word)
WRAPPERS = ("", "", "", "env X=1 ", "timeout 5 ", "nice -n 5 ", "command ", "X=1 ", "nohup ")
REDIRECTS = ("", "", " 2>/dev/null", " >out", " 2>&1", " <in")
SEPARATORS = (" ; ", " && ", " | ", "\n", " || ")
CORPUS_SIZE = 1500
MIN_NONBLIND_RATE = 0.66
MIN_EXACT_RATE = 0.47
_MARKER = re.compile(r"__genesis_oracle_\d+__")
_FAIL = "__oracle_fail__"


def _write_shims(directory: Path) -> None:
    script = (
        '#!/bin/sh\n{ printf "%s\\037" "${0##*/}" "$@"; printf "\\036"; } > "$ORACLE_DIR/rec.$$"\n'
        'if [ "${2-}" = "__oracle_fail__" ]; then exit 1; fi\n'
        "exit 0\n"
    )
    for shim in SHIMS:
        path = directory / shim
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)


def _prepare_work_dir(directory: Path) -> None:
    input_file = directory / "in"
    input_file.write_text("oracle input\n", encoding="utf-8")
    input_file.chmod(0o444)
    (directory / "out").write_text("", encoding="utf-8")


@pytest.fixture(scope="module")
def shim_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("bash-oracle-shims")
    _write_shims(directory)
    return directory


@pytest.fixture(scope="module")
def work_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("bash-oracle-work")
    _prepare_work_dir(directory)
    return directory


@pytest.fixture(scope="module")
def home_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("bash-oracle-home")


@dataclass(frozen=True)
class _BashOracle:
    shim_dir: Path
    work_dir: Path
    home_dir: Path

    def run(self, command: str) -> list[tuple[str, ...]]:
        oracle_dir = self.work_dir / ".oracle"
        oracle_dir.mkdir(exist_ok=True)
        for record_path in oracle_dir.glob("rec.*"):
            record_path.unlink()
        (self.work_dir / "out").write_text("", encoding="utf-8")
        env = {
            "PATH": f"{self.shim_dir}:/usr/bin:/bin",
            "ORACLE_DIR": str(oracle_dir),
            "HOME": str(self.home_dir),
            "LC_ALL": "C",
        }
        subprocess.run(
            ["bash", "-c", command],
            cwd=self.work_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
        )
        records = []
        for record_path in oracle_dir.glob("rec.*"):
            data = record_path.read_bytes().decode("utf-8", "replace")
            records.extend(
                tuple(record.split("\x1f")[:-1]) for record in data.split("\x1e") if record
            )
        return sorted(records)


@pytest.fixture(scope="module")
def oracle(shim_dir: Path, work_dir: Path, home_dir: Path) -> _BashOracle:
    return _BashOracle(shim_dir, work_dir, home_dir)


class _Generator:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.next_id = 0
        self.started = False
        self.or_rhs_ids: set[str] = set()
        self.controlled_pair: tuple[str, str] | None = None

    def leaf(self, executable: str, args: list[str], *, failure: bool | None = None) -> str:
        self.next_id += 1
        marker = f"__genesis_oracle_{self.next_id}__"
        if failure is None:
            failure = self.rng.randrange(4) == 0
        return " ".join([executable, marker, *([_FAIL] if failure else []), *args])

    def word(self, *, in_dq_subst: bool = False) -> str:
        word = self.rng.choice(SUBSTITUTION_WORDS if in_dq_subst else WORDS)
        if "'" in word:
            return dq(word)
        if in_dq_subst and '"' in word:
            assert "'" not in word
            return f"'{word}'"
        choice = self.rng.random()
        if choice < 0.3:
            return f"'{word}'"
        if choice < 0.5:
            return dq(word)
        if " " in word or '"' in word or "*" in word:
            return f"'{word}'"
        return word

    def simple(self, depth: int = 0, *, in_dq_subst: bool = False) -> str:
        command = self.rng.choice(SHIMS)
        args = [self.word(in_dq_subst=in_dq_subst) for _ in range(self.rng.randint(0, 4))]
        result = self.rng.choice(WRAPPERS) + self.leaf(command, args)
        result += self.rng.choice(REDIRECTS)
        choice = self.rng.random()
        if depth < 2 and not in_dq_subst and choice < 0.12:
            return f"bash -c {dq(self.compound(depth + 1))}"
        if depth < 2 and choice < 0.22:
            nested = self.simple(depth + 1, in_dq_subst=True)
            return self.leaf(self.rng.choice(SHIMS), [f'"$({nested})"'])
        if depth < 2 and choice < 0.28:
            return f"( {self.compound(depth + 1, in_dq_subst=in_dq_subst)} )"
        if depth < 2 and choice < 0.32:
            return f"{{ {self.compound(depth + 1, in_dq_subst=in_dq_subst)}; }}"
        return result

    def compound(self, depth: int = 0, *, in_dq_subst: bool = False) -> str:
        if depth == 0 and not self.started:
            self.started = True
            left = self.leaf("git", [], failure=True)
            right = self.leaf("curl", [], failure=False)
            left_id, right_id = _MARKER.findall(left + right)
            self.controlled_pair = (left_id, right_id)
            self.or_rhs_ids.add(right_id)
            return f"{left} || {right}"
        parts = [self.simple(depth, in_dq_subst=in_dq_subst)]
        for _ in range(self.rng.randint(0, 2)):
            separator = self.rng.choice(SEPARATORS)
            right = self.simple(depth, in_dq_subst=in_dq_subst)
            if separator == " || ":
                self.or_rhs_ids.update(_MARKER.findall(right))
            parts.extend((separator, right))
        return "".join(parts)


def dq(payload: str) -> str:
    return '"' + re.sub(r"([\\\"$`])", r"\\\1", payload) + '"'


@dataclass(frozen=True)
class _Finding:
    kind: str
    command: str
    segments: tuple[tuple[str, tuple[str, ...]], ...]
    observed: tuple[tuple[str, ...], ...]

    def format(self) -> str:
        return (
            f"command={self.command!r}\n  parser segments={self.segments!r}"
            f"\n  observed invocations={self.observed!r}"
        )


@dataclass
class _CorpusResult:
    seed: int
    blind: int
    exact: int
    extra: int
    misses: list[_Finding]
    argv_qualifying: int
    argv_mismatches: list[_Finding]
    trace_sha256: str
    or_rhs_allocated: int
    or_rhs_observed: int
    controlled_or_reached: bool


def _invocation_counts(records: tuple[tuple[str, ...], ...]) -> Counter:
    return Counter(
        (record[0], record[1])
        for record in records
        if len(record) > 1 and record[0] in SHIMS and _MARKER.fullmatch(record[1])
    )


def _measure_corpus(oracle: _BashOracle, seed: int) -> _CorpusResult:
    generator = _Generator(seed)
    trace = hashlib.sha256()
    blind = exact = extra = argv_qualifying = 0
    or_rhs_allocated = or_rhs_observed = 0
    controlled_or_reached = False
    misses: list[_Finding] = []
    argv_mismatches: list[_Finding] = []
    for _ in range(CORPUS_SIZE):
        command = generator.compound()
        # #3257's '\'' idiom remains a separate reproduction row.
        assert "'\\''" not in command
        segments, blind_spot = sp.analyze_checked(command)
        observed = tuple(oracle.run(command))
        allocated = _MARKER.findall(command)
        assert len(allocated) == len(set(allocated)), "generated leaf IDs must be unique"
        assert all(
            len(record) > 1 and record[0] in SHIMS and record[1] in allocated for record in observed
        ), "recorder emitted an invocation not allocated by this command"
        observed_ids = {record[1] for record in observed}
        rhs_ids = generator.or_rhs_ids.intersection(allocated)
        or_rhs_allocated += len(rhs_ids)
        or_rhs_observed += len(rhs_ids & observed_ids)
        if generator.controlled_pair is not None:
            left_id, right_id = generator.controlled_pair
            controlled_or_reached |= right_id in observed_ids and any(
                record[1:3] == (left_id, _FAIL) for record in observed
            )
        segment_view = tuple((segment.exe, tuple(segment.argv)) for segment in segments)
        normalized_observed = tuple(
            tuple(
                argument.replace(str(oracle.home_dir), "<TMP_HOME>").replace(
                    str(oracle.work_dir), "<TMP_WORK>"
                )
                for argument in invocation
            )
            for invocation in observed
        )
        trace.update(repr((command, blind_spot, segment_view, normalized_observed)).encode("utf-8"))
        if blind_spot is not None:
            blind += 1
            continue
        parsed_ids = _invocation_counts(
            tuple((segment.exe, *segment.argv[1:]) for segment in segments)
        )
        ran_ids = _invocation_counts(observed)
        missing, extra_ids = ran_ids - parsed_ids, parsed_ids - ran_ids
        if missing:
            misses.append(
                _Finding(
                    "ran-not-parsed",
                    command,
                    segment_view,
                    observed,
                )
            )
        if extra_ids:
            extra += 1
        if not missing and not extra_ids:
            exact += 1
            if observed and not any(char in command for char in ("$", "`", "\\", "*")):
                argv_qualifying += 1
                parsed_argv = Counter(
                    (segment.exe, tuple(segment.argv[1:]))
                    for segment in segments
                    if segment.exe in SHIMS
                )
                observed_argv = Counter((invocation[0], invocation[1:]) for invocation in observed)
                if parsed_argv != observed_argv:
                    argv_mismatches.append(
                        _Finding(
                            "argv-mismatch",
                            command,
                            segment_view,
                            observed,
                        )
                    )
    return _CorpusResult(
        seed,
        blind,
        exact,
        extra,
        misses,
        argv_qualifying,
        argv_mismatches,
        trace.hexdigest(),
        or_rhs_allocated,
        or_rhs_observed,
        controlled_or_reached,
    )


@pytest.fixture(scope="module")
def corpus_result(oracle: _BashOracle) -> _CorpusResult:
    seed = int(os.environ.get("SHELL_PARSE_BASH_ORACLE_SEED", "1656"))
    return _measure_corpus(oracle, seed)


def test_shim_harness_records_argv(oracle: _BashOracle) -> None:
    assert oracle.run("ls a 'b c'") == [("ls", "a", "b c")]


def test_generated_corpus_has_no_silent_misses(corpus_result: _CorpusResult) -> None:
    result = corpus_result
    nonblind = CORPUS_SIZE - result.blind
    print(
        f"seed={result.seed} N={CORPUS_SIZE} blind={result.blind}"
        f" exact={result.exact} extra={result.extra} miss={len(result.misses)}"
        f" trace={result.trace_sha256}"
        f" OR-RHS-leaves={result.or_rhs_observed}/{result.or_rhs_allocated}"
    )
    assert not result.misses, "Ran-but-unparsed shims:\n" + "\n".join(
        finding.format() for finding in result.misses[:10]
    )
    assert nonblind / CORPUS_SIZE >= MIN_NONBLIND_RATE
    assert result.exact / CORPUS_SIZE >= MIN_EXACT_RATE
    assert result.controlled_or_reached, "the deliberate failing-LHS OR control did not execute"
    assert result.or_rhs_observed > 0


def test_argv_matches_for_expansion_free_commands(corpus_result: _CorpusResult) -> None:
    result = corpus_result
    print(
        f"argv seed={result.seed} qualifying={result.argv_qualifying}"
        f" mismatches={len(result.argv_mismatches)}"
    )
    _assert_argv_evidence(result)


def _assert_argv_evidence(result: _CorpusResult) -> None:
    assert result.argv_qualifying > 0, "no argv comparisons were exercised"
    assert not result.argv_mismatches, "\n".join(
        finding.format() for finding in result.argv_mismatches[:10]
    )


def test_empty_argv_evidence_is_rejected(corpus_result: _CorpusResult) -> None:
    with pytest.raises(AssertionError, match="no argv comparisons"):
        _assert_argv_evidence(replace(corpus_result, argv_qualifying=0))


@pytest.mark.parametrize("separator", ["&&", "||"])
@pytest.mark.parametrize("failure", [False, True])
def test_short_circuit_status_twins(oracle: _BashOracle, separator: str, failure: bool) -> None:
    left = ("git", "__genesis_oracle_1__", *([_FAIL] if failure else []))
    right = ("curl", "__genesis_oracle_2__")
    command = f"{' '.join(left)} {separator} {' '.join(right)}"
    observed = tuple(oracle.run(command))
    reached = failure if separator == "||" else not failure
    assert observed == tuple(sorted([left, *([right] if reached else [])]))
    segments, blind = sp.analyze_checked(command)
    assert blind is None
    parsed = tuple((segment.exe, *segment.argv[1:]) for segment in segments)
    assert not (_invocation_counts(observed) - _invocation_counts(parsed))


def test_skipped_same_executable_cannot_cancel_a_missed_invocation(
    oracle: _BashOracle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = 'cat __genesis_oracle_1__ "$(git __genesis_oracle_2__ push)" || git __genesis_oracle_3__ status'
    analyze = sp.analyze_checked

    def drop_executed_leaf(text):
        segments, blind = analyze(text)
        return [s for s in segments if s.argv[1:2] != ["__genesis_oracle_2__"]], blind

    monkeypatch.setattr(sp, "analyze_checked", drop_executed_leaf)
    monkeypatch.setattr(_Generator, "compound", lambda self: command)
    monkeypatch.setitem(globals(), "CORPUS_SIZE", 1)
    result = _measure_corpus(oracle, 1656)
    assert len(result.misses) == 1, "the actual corpus scorer accepted a dropped executed leaf"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git __genesis_oracle_1__ 'a b'", [("git", "__genesis_oracle_1__", "a b")]),
        ("env X=1 git __genesis_oracle_1__ x", [("git", "__genesis_oracle_1__", "x")]),
        ("git __genesis_oracle_1__ <in", [("git", "__genesis_oracle_1__")]),
        (
            "git __genesis_oracle_1__ ; curl __genesis_oracle_2__ x",
            [("curl", "__genesis_oracle_2__", "x"), ("git", "__genesis_oracle_1__")],
        ),
    ],
)
def test_literal_argv_controls(oracle: _BashOracle, command: str, expected: list) -> None:
    assert oracle.run(command) == expected
    segments, blind = sp.analyze_checked(command)
    assert blind is None
    assert Counter((s.exe, *s.argv[1:]) for s in segments if s.exe in SHIMS) == Counter(expected)


@pytest.mark.parametrize(
    ("gap_id", "command"),
    [
        ("gh3257", r"ls 'a'\''b'; curl x"),
        ("gh3264", r'cat "$(rm "q\"r")" && curl x'),
        ("gh3271", r'''bash -c "cat \"\$(rm 'q\"r')\" && curl x"'''),
        ("gh3272", r'''rm "$(pytest 'q"r')" ; curl x ; ls "$(cat 'q"r')"'''),
    ],
    ids=("gh3257", "gh3264", "gh3271", "gh3272"),
)
def test_known_gap_still_reproduces(oracle: _BashOracle, gap_id: str, command: str) -> None:
    observed = oracle.run(command)
    segments, blind = sp.analyze_checked(command)
    message = f"{gap_id} is fixed; delete this row and re-enable its construct in the generator."
    assert any(invocation[0] == "curl" for invocation in observed), message
    assert blind is None, message
    assert not any(segment.exe == "curl" for segment in segments), message
