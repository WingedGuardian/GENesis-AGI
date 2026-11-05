"""A `*` inside a `genesis db query` argument is SQL, not a secrets glob (#3789)."""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"


class TestDbQueryLiteralsAreNotGlobs:
    """#3789: a `*` inside a `genesis db query` argument is SQL, not a pattern.

    The glob arm expanded every token against the hook's cwd, so from a
    checkout root holding `secrets.env` a read-only query denied a dispatched
    session and raised a false CRITICAL. The exemption is an exact-form
    allowlist (`secrets_target._db_query_literals`); everything outside it keeps
    the old behaviour. The must-gate list carries every bypass spelling two
    adversarial reviews found against earlier, general designs.
    """

    @staticmethod
    def _touches(**kw) -> bool:
        spec = importlib.util.spec_from_file_location("st", _HOOKS / "secrets_target.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.touches_secrets(**kw)

    @pytest.fixture
    def in_checkout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """cwd = a directory holding a real `secrets.env`, as a checkout root does."""
        home = tmp_path / "home"
        (home / "genesis").mkdir(parents=True)
        (home / "genesis" / "secrets.env").write_text("API_KEY=x\n")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(home / "genesis")
        return home / "genesis"

    Q = "~/genesis/.venv/bin/python -m genesis db query"

    def test_query_literals_clear(self, in_checkout: Path) -> None:
        commands = [
            # The two real denials, verbatim in shape.
            self.Q
            + " \"select * from session_charters where session_id like 'ab12%'\" --json --limit 2",
            'timeout 90 .venv/bin/python -m genesis db query "SELECT a, substr(b,1,10) AS c, '
            '(SELECT count(*) FROM t o WHERE o.id = p.id) AS n FROM p ORDER BY b" 2>&1 | head -20',
            self.Q + ' "select count(*) from t"',
            self.Q + ' "select * from t" 2>&1 | tail -5',
            self.Q + ' "select * from t" 2>&1 | cut -c1-600 | head -5',
            self.Q + " 'select * from t where a = \"b\"'",
            self.Q + ' "select * from t" | head',
            self.Q + ' "select * from t" 2>&1 | head -c 3000',
        ]
        for command in commands:
            assert not self._touches(command=command), command

    def test_only_a_bare_query_line_is_exempt(self, in_checkout: Path) -> None:
        """Anything beyond `query [2>&1] [| head|tail|cut]` keeps the old check,
        including harmless-looking shapes: the grammar is closed on purpose."""
        commands = [
            'python3 -m genesis db query "select * from t" --json\n'
            + self.Q
            + ' "select count(*) from u"',
            self.Q + ' "select * from t"; echo done',
            self.Q + ' "select * from t" && ' + self.Q + ' "select count(*) from u"',
            "cd ~/genesis; " + self.Q + ' "select * from t"',
            self.Q + ' "select * from t" | sort',
            self.Q + ' "select * from t" > out.txt',
            "env " + self.Q + ' "select * from t"',
        ]
        for command in commands:
            assert self._touches(command=command), command

    def test_a_query_never_shields_a_real_glob(self, in_checkout: Path) -> None:
        q = self.Q
        commands = [
            # The same piece outside a query argument.
            q + ' "select *" ; cat *',
            q + ' "select *" && echo *',
            q + ' "select *" > *',
            q + ' "select *" # *',
            q + " s*.env",
            q + ' "x" s*',
            q + ' "a"*',
            q + ' x"*"',
            q + ' \'s*"".env\' && cat s*"".env',
            # Expansion inside the quoted argument, or anywhere.
            q + ' "$(cat s*.env)"',
            q + ' "a\\"*"',
            'x="*"; ' + q + ' "$x"',
            # Not the exact form.
            "bash -c '" + q + ' "*"\'',
            "FOO=1 " + q + ' "*" && cat *',
            q + 'x "*"',
            'python3 -m genesis db queryx "*"',
            'perl -m genesis db query "*"',
            './genesis -m genesis db query "*"',
            "python3 - <<'PY'\nimport glob\nprint(open(glob.glob('secr*')[0]).read())\nPY",
            q + ' "x" 2>&1 | tail s*.env',
            # shlex(posix=False) pairs a mid-word quote differently from bash:
            # bash reads `s*.env` unquoted here (security review, before the PR).
            q + " q\" 'a\" --db s*.env --param 'b\"'",
            q + " x\" 'a\" s*.env 'b\"'",
            # The program can print a literal back; a later segment that
            # executes its input would expand it (final review, before the PR).
            q + " \"select 'cat s*.env'\" | sh",
            q + " \"select 'cat s*.env'\" | bash -s",
            q + " \"select 'cat s*.env'\" | xargs -I{} sh -c {}",
            q + " \"select 'cat s*.env'\" > x; . ./x",
            q + " \"select 'cat s*.env'\" | python3",
            q + " \"select 'cat s*.env'\" 2>&1 | sort --compress-program=sh",
            # Process substitution hides the executor inside a redirect target,
            # and `xargs` makes a reader of a printed filename (re-check review).
            q + " \"select 'cat s*.env'\" > >(sh)",
            q + " \"select 'cat s*.env'\" >(sh)",
            q + " \"select 'cat s*.env'\" | head > >(sh)",
            q + " \"select 'secrets.env', 's*'\" | xargs head",
            q + " \"select 's*'\" | ./head",
            # Starts and ends with `"` but is three pieces to bash, which globs s*.env.
            q + ' "s"*".env"',
            q + " 's'*'.env'",
            # A quote inside a comment swallows the next line (#3473).
            q + ' "select 1" # "\ncat s*.env\n# "',
            q + " \"select *\" # '\ncat s*.env # '",
        ]
        for command in commands:
            assert self._touches(command=command), command

    def test_bypasses_from_earlier_designs_still_gate(self, in_checkout: Path) -> None:
        """Each really reads the file under bash; review found them against v1/v2."""
        commands = [
            'eval "cat s*.env"',
            "watch -n1 'cat s*.env'",
            "flock f -c 'cat s*.env'",
            "find . -exec sh -c 'cat s*.env' {} +",
            "trap 'cat s*.env' EXIT",
            "git -c alias.x='!cat s*.env' x",
            'echo "$(cat s*.env)"',
            'cat s*"".env',
            "head s*''.env",
            "cat $'s'*.env",
            "echo ok # '\ngrep -h . secrets.* # '",
            "echo '<<\"X\"'\ncat secrets.*\nX",
            "grep -h . $(echo 'x *')",
            "python3 -c \"import os; os.system('cat ./s*.env')\"",
            "python3 x.py 'cat *'",
            'sqlite3 :memory: ".once |sh" "select \'cat *\'"',
            'psql -c "select 1 \\g |cat *"',
            'bash -c "cat *"',
            'ssh host "cat *"',
            "cat *",
            'find . -name "secrets.*" -exec cat {} +',
            "grep KEY s*.env",
        ]
        for command in commands:
            assert self._touches(command=command), command

    def test_declared_residuals_still_gate(self, in_checkout: Path) -> None:
        """Outside the exact form, by design: each needs its own argument for safety."""
        commands = [
            'sqlite3 data/genesis.db "select * from x"',
            'python3 -c "print(2 * 3)"',
            "python3 - <<'PY'\nfrom a import *\nPY",
            "ssh host \"python3 -m genesis db query 'select count(*) from t'\"",
            'echo "*"',
        ]
        for command in commands:
            assert self._touches(command=command), command

    def test_a_comment_or_heredoc_gets_no_exemption(self, in_checkout: Path) -> None:
        """A comment on the query segment breaks the argv agreement (the
        canonical argv drops it, the quote-keeping tokens keep it), and a
        here-doc anywhere ends the exemption, so neither ever exempts (#3473)."""
        spec = importlib.util.spec_from_file_location("st", _HOOKS / "secrets_target.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod._db_query_literals(self.Q + ' "select *"') == {"*"}
        assert mod._db_query_literals(self.Q + ' "select *" # note') == set()
        assert mod._db_query_literals(self.Q + " \"select *\" <<'X'\nbody\nX") == set()
        # A blind spot (here an unterminated quote) gets no exemption either.
        assert mod._db_query_literals(self.Q + ' "s*.env"\n\'x') == set()
        # Only a non-executing sink may sit beside a query.
        assert mod._db_query_literals(self.Q + ' "select *" | head -5') == {"*"}
        assert mod._db_query_literals(self.Q + ' "select *" | sh') == set()

    def test_a_reported_blind_spot_always_refuses(
        self, in_checkout: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The parser's own fail-closed contract: when analyze_checked reports a
        blind spot, its segments are not trusted, even if they look valid."""
        spec = importlib.util.spec_from_file_location("st", _HOOKS / "secrets_target.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        command = self.Q + ' "select *" | head -5'
        segments, blind = mod.shell_parse.analyze_checked(command)
        assert blind is None and mod._db_query_literals(command) == {"*"}
        monkeypatch.setattr(mod.shell_parse, "analyze_checked", lambda _c: (segments, object()))
        assert mod._db_query_literals(command) == set()

    def test_a_long_line_gets_no_exemption_and_stays_fast(self, in_checkout: Path) -> None:
        """The canonical parse is quadratic in pipe stages and a hook that overruns
        its timeout allows the command, so an over-long line is refused before any
        parse (security review, before the PR: 8.3s at 131 KB)."""
        spec = importlib.util.spec_from_file_location("st", _HOOKS / "secrets_target.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        at_cap = self.Q + ' "select *"'
        at_cap += " " * (mod._DBQ_MAX_CHARS - len(at_cap))
        assert mod._db_query_literals(at_cap) == {"*"}
        assert mod._db_query_literals(at_cap + " ") == set()
        command = self.Q + " 'select *' | head secrets.env " + "| cut " * 20000
        started = time.monotonic()
        assert self._touches(command=command)
        assert time.monotonic() - started < 2.0

    def test_timeout_takes_ascii_digits_only(self, in_checkout: Path) -> None:
        """`str.isdigit` accepts `²` and Arabic-Indic digits, which real `timeout` rejects."""
        tail = ' python3 -m genesis db query "select *"'
        assert not self._touches(command="timeout 90" + tail)
        assert self._touches(command="timeout ²" + tail)
        assert self._touches(command="timeout ٣" + tail)
