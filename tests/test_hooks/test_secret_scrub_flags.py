"""Command-line credential flags in ``scripts/hooks/secret_scrub.py``.

A command line carries a credential as the VALUE of a flag: ``--password "a b"``,
``--token=x``, ``curl -u user:pw``. The label arms need a ``:`` or ``=`` right
after the label, so the separated form (a space, then the value) passed through
whole, and a quoted value only kept its first word out of reach when ``=`` was
used. These tests pin both halves of the flag arm:

* every flag NAME the scrubber already treats as a credential label
  (``_TOKEN_LABEL``, ``_PASSWORD_LABEL``, ``_QUOTED_KEY_EXTRA_LABEL``), in every
  separator and quoting form, has its whole value withheld, the flag kept;
* a ``user:password`` pair after ``-u`` / ``--user`` / ``--proxy-user`` keeps the
  user name and loses the password, the convention the URL userinfo arm uses;
* the controls (a ``-file`` flag, ``-p`` as a port, a ``-u`` with no colon,
  prose) stay byte-identical.

Every credential value here is synthetic and assembled from parts.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "hooks"))
import secret_scrub as s  # noqa: E402

R = "[REDACTED]"

# Synthetic secret words, assembled so no fixture line is itself a credential.
W1, W2, W3 = "zq" + "Xv7", "k9" + "wQm", "Pt" + "4rN"
SPACED = f"{W1} {W2} {W3}"

# Flag names taken from the scrubber's own label vocabulary, one or more per
# list, plus the prefixed spellings wget and database clients use.
FLAG_NAMES = [
    "password",
    "passwd",
    "pass",
    "pwd",
    "passphrase",
    "passcode",
    "pin",
    "token",
    "secret",
    "api-key",
    "api_key",
    "access-token",
    "auth-token",
    "refresh-token",
    "secret-key",
    "private-key",
    "client-secret",
    "id-token",
    "db-password",
    "http-password",
    "ftp-password",
    "proxy-password",
    "github-token",
]

QUOTED_FORMS = [
    ('--{f} "{v}"', "--{f} " + R),
    ("--{f} '{v}'", "--{f} " + R),
    ('--{f}="{v}"', "--{f}=" + R),
    ("--{f}='{v}'", "--{f}=" + R),
    ('-{f} "{v}"', "-{f} " + R),
    ('--{f}\t"{v}"', "--{f}\t" + R),
]


def _no_secret_left(out: str) -> bool:
    return not any(w in out for w in (W1, W2, W3))


class TestQuotedValueAfterACredentialFlag:
    @pytest.mark.parametrize("flag", FLAG_NAMES)
    @pytest.mark.parametrize("form,expected", QUOTED_FORMS)
    def test_the_whole_quoted_value_is_withheld(self, flag, form, expected):
        line = "tool " + form.format(f=flag, v=SPACED) + " --host db.example"
        out = s.scrub(line)
        assert out == "tool " + expected.format(f=flag) + " --host db.example"

    @pytest.mark.parametrize("flag", FLAG_NAMES)
    @pytest.mark.parametrize("sep", [" ", "="])
    def test_an_unquoted_value_is_withheld(self, flag, sep):
        out = s.scrub(f"tool --{flag}{sep}{W1}{W2} next")
        assert out == f"tool --{flag}{sep}{R} next"

    def test_the_flag_name_is_case_insensitive(self):
        assert s.scrub(f'tool --PASSWORD "{SPACED}"') == f"tool --PASSWORD {R}"

    @pytest.mark.parametrize(
        "value",
        [
            f'"{W1} \\"{W2}\\" {W3}"',  # escaped double quotes inside
            f"'{W1} '\\''{W2} {W3}'",  # the shell's close-escape-reopen idiom
            f"\"{W1}\" '{W2}'",  # NOT one word: only the first is the value
            f"\"{W1} {W2}\"'{W3} x'",  # adjacent quoted segments are ONE word
            f'"{W1}={W2}:{W3} @x"',  # = : and @ inside
            f'"pä{W1} ü{W2} 密{W3}"',  # unicode
            f'"{W1}\n{W2} {W3}"',  # a quoted newline is part of the value
            f"{W1}\\ {W2}\\ {W3}",  # backslash-escaped spaces, unquoted
        ],
    )
    def test_quoting_shapes_cannot_end_the_value_early(self, value):
        out = s.scrub(f"tool --password {value} tail")
        if value.startswith(f'"{W1}" '):
            # Two shell words: the second is an ordinary argument.
            assert out == f"tool --password {R} '{W2}' tail"
            return
        assert _no_secret_left(out), out
        assert out == f"tool --password {R} tail"

    def test_a_value_starting_with_an_escaped_quote_is_withheld_to_the_line_end(self):
        # A serialized command (a JSON field, a log record): its quoting is
        # escaped, so the value's end cannot be read. The line goes.
        line = '{"command": "mysql --password \\"' + SPACED + '\\" db"}\nnext line'
        assert s.scrub(line) == '{"command": "mysql --password ' + R + "\nnext line"

    def test_an_unclosed_quote_is_withheld_to_the_end_of_the_text(self):
        # A capture cut mid-value: everything after the quote is the value.
        text = f'tool --token "{W1} {W2}\nmore {W3}'
        assert s.scrub(text) == f"tool --token {R}"

    def test_a_flag_at_the_end_of_the_text(self):
        for text in ("tool --password", "tool --password ", "tool --password="):
            assert s.scrub(text) == text

    def test_a_flag_followed_by_another_flag_takes_no_value(self):
        # Stated behaviour: a following word that starts with "-" is read as the
        # next option, not as the value (a password starting with "-" in the
        # separated form is the residual of that reading).
        text = "tool --password --verbose -x"
        assert s.scrub(text) == text

    def test_an_equals_value_starting_with_a_dash_is_still_withheld(self):
        assert s.scrub(f"tool --password=-{W1}") == f"tool --password={R}"

    def test_an_empty_quoted_value_is_kept(self):
        for text in ('tool --password ""', "tool --token=''"):
            assert s.scrub(text) == text

    def test_shell_metacharacters_end_the_value(self):
        out = s.scrub(f"tool --password {W1};echo done|cat")
        assert out == f"tool --password {R};echo done|cat"

    def test_two_flags_on_one_line(self):
        out = s.scrub(f'a --password "{W1} {W2}" --token {W3} z')
        assert out == f"a --password {R} --token {R} z"


class TestUserColonPasswordAfterAUserFlag:
    PW = W1 + W2

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("curl -u alice:{pw} https://h.example", "curl -u alice:" + R + " https://h.example"),
            ("curl -ualice:{pw} https://h.example", "curl -ualice:" + R + " https://h.example"),
            ("curl -u'alice:{pw} x' https://h", "curl -u'alice:" + R + "' https://h"),
            ('curl -u "alice:{pw} x" https://h', 'curl -u "alice:' + R + '" https://h'),
            ('curl -u"alice:{pw}" https://h', 'curl -u"alice:' + R + '" https://h'),
            ("curl --user=alice:{pw} https://h", "curl --user=alice:" + R + " https://h"),
            ("curl --user alice:{pw} https://h", "curl --user alice:" + R + " https://h"),
            ("curl --user 'alice:{pw}' https://h", "curl --user 'alice:" + R + "' https://h"),
            ("curl --proxy-user bob:{pw} https://h", "curl --proxy-user bob:" + R + " https://h"),
            ("curl -su alice:{pw} https://h", "curl -su alice:" + R + " https://h"),
            # A password holding a colon: everything after the FIRST colon goes.
            ("curl -u alice:{pw}:more https://h", "curl -u alice:" + R + " https://h"),
            # The user part may itself be quoted, the colon outside the quotes.
            ("curl -u \"alice\":'{pw} x' h", 'curl -u "alice":' + R + " h"),
        ],
    )
    def test_the_password_goes_and_the_user_stays(self, line, expected):
        out = s.scrub(line.format(pw=self.PW))
        assert out == expected
        assert self.PW not in out

    @pytest.mark.parametrize(
        "text",
        [
            "curl -u alice https://h.example",  # user only: curl prompts
            "curl -u alice: https://h.example",  # an empty password
            "env -u GH_TOKEN /x/guard.sh",
            "python3 -u /x/hook.py",
            "systemctl --user restart genesis-server",
            "chrome --user-data-dir=/x/profile",
            "git push -u origin HEAD",
        ],
    )
    def test_a_user_flag_without_a_password_is_kept(self, text):
        assert s.scrub(text) == text


class TestControlsStayByteIdentical:
    @pytest.mark.parametrize(
        "text",
        [
            "tool --password-file /run/pw.txt",
            "tool --token-file=/run/tok --secret-key-file x",
            "tool --pin-count 3 --passwords-allowed 2",
            # -p is not a credential name in the scrubber's vocabulary, so it is
            # never read as one: a port, mkdir's parents flag and a quoted
            # argument all stay. (-p 'a b c' as a password is the residual.)
            "ssh -p 8080 host.example",
            'mkdir -p "a b c"',
            "tool -p 'a b c'",
            "Enter your password when the prompt asks for it.",
            "the password policy requires a passphrase of four words",
            "bypass the compass, spin the pin",
            "see the --password flag in the docs <value>",
        ],
    )
    def test_unchanged(self, text):
        if text.startswith("see the"):
            # Prose that names a flag loses the next word: the cheap direction.
            assert s.scrub(text) == "see the --password [REDACTED] in the docs <value>"
            return
        assert s.scrub(text) == text


class TestIdempotence:
    CASES = [
        f'tool --password "{SPACED}" x',
        f"tool --token={W1}",
        f"curl -u 'alice:{W1} {W2}' h",
        f'curl -u "alice:{W1}',
        f'tool --password "{W1} unclosed',
        '{"c": "x --password \\"' + SPACED + '\\" y"}\nz',
        f"curl -u alice:{W1} --password '{W2} {W3}'",
    ]

    @pytest.mark.parametrize("text", CASES)
    def test_scrub_twice_equals_scrub_once(self, text):
        once = s.scrub(text)
        assert s.scrub(once) == once
        assert _no_secret_left(once), once


class TestFlagScanCostIsLinear:
    """Deterministic cost: the value scanner never re-reads a character.

    Every value scan covers a span of the input, and a flag found inside a span
    already scanned is skipped, so the spans are disjoint and their total length
    is at most the input length. A version that re-scanned from each nested flag
    would be quadratic on these shapes and fails the count, with no clock.
    """

    SHAPES = {
        "nested_user_flags": "-u a=" + "-ua=" * 20000 + ":x",
        "nested_password_flags": "--password=" * 20000,
        "unclosed_quotes": '--password "x ' * 20000,
        "many_flags": "--token a " * 20000,
        "escaped_quote_runs": '--password "' + '\\"' * 20000,
        "single_quote_runs": "--secret '" + "a" * 40000,
        "dash_wall": "-" * 40000 + " --password x",
        "enclosed_flags": '-m "x --password a\'b ' * 20000,
        "midword_quotes_one_line": "--password a'b --token c\"d " * 10000,
        "midword_quotes_many_lines": "--password a'b\n--token c\"d\n" * 10000,
        "flags_far_apart": ("x " * 50 + "--token a ") * 2000,
        "label_values": "--secret password:password:" * 10000 + " x",
        "user_paths": "rsync -avu h:/a " * 10000,
    }

    @pytest.mark.parametrize("shape", sorted(SHAPES))
    def test_scanned_characters_never_exceed_the_input(self, shape, monkeypatch):
        text = self.SHAPES[shape]
        real_word, real_close, real_line = s._shell_word_end, s._quote_close, s._line_end
        words, searches, line_misses, stepped = [0, 0], [0], [0], [0]

        class Quadratic(AssertionError):
            pass

        def counting_word(t, i, enclosed="", nl=None):
            end = real_word(t, i, enclosed, nl)
            words[0] += 1
            words[1] += end - i
            if words[1] > len(t):  # fail at once rather than run the quadratic out
                raise Quadratic((shape, words[1], len(t)))
            return end

        def counting_close(t, i, limit):
            close = real_close(t, i, limit)
            searches[0] += (limit if close == -1 else close) - i
            if searches[0] > 2 * len(t):
                raise Quadratic((shape, "quote search", searches[0], len(t)))
            return close

        def counting_line(t, i, nl=None):
            # A newline search that is not served from the cache. One per line
            # at most; a search per value on a long line is the quadratic shape.
            if nl is None or nl[0] < i:
                line_misses[0] += 1
                if line_misses[0] > 2 * (t.count("\n") + 1):  # two passes
                    raise Quadratic((shape, "newline search", line_misses[0]))
            return real_line(t, i, nl)

        class CountingEvents:
            # Characters the quote tracker steps over, measured on the search
            # it actually runs. It resumes where it stopped, so the total stays
            # near one pass per flag pass; restarting from 0 is quadratic.
            def __init__(self, real):
                self.real = real

            def search(self, t, j, i):
                m = self.real.search(t, j, i)
                stepped[0] += (m.end() if m else i) - j
                if stepped[0] > 2 * len(t):
                    raise Quadratic((shape, "quote tracker", stepped[0], len(t)))
                return m

        monkeypatch.setattr(s, "_QUOTE_EVENT", CountingEvents(s._QUOTE_EVENT))
        monkeypatch.setattr(s, "_line_end", counting_line)
        monkeypatch.setattr(s, "_shell_word_end", counting_word)
        monkeypatch.setattr(s, "_quote_close", counting_close)
        # scrub() swallows exceptions into a placeholder, so call the two flag
        # passes directly: a Quadratic must reach the test.
        s._redact_credential_flags(text)
        s._redact_user_passwords(text)
        assert words[0], "the scanner never ran: the count below proves nothing"


class TestFuzz:
    """Random command lines mixing the new forms with ordinary text."""

    ORDINARY = [
        "ls -la",
        "git status",
        "cd /srv/app",
        "echo hello world",
        "make test",
        "grep -rn TODO src",
        "python3 -m pytest -q",
        "docker ps",
        "ssh -p 2222 h",
        "mkdir -p 'a b'",
        "tar -xzf a.tgz",
        "curl https://h.example/x",
        "sort -u f",
        "the password policy",
        "kubectl get pods -n x",
        "env -u HOME x",
    ]

    @staticmethod
    def _secret(rng: random.Random, n: int) -> str:
        alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        words = ["".join(rng.choice(alphabet) for _ in range(6)) for _ in range(rng.randint(1, 3))]
        return "QX" + str(n) + "Z" + " ".join(words)  # unique per case

    def _credential(self, rng: random.Random, secret: str) -> str:
        flag = rng.choice(FLAG_NAMES)
        dash = rng.choice(["--", "-"])
        one_word = secret.replace(" ", "")
        form = rng.randrange(7)
        if form == 0:
            return f'{dash}{flag} "{secret}"'
        if form == 1:
            return f"{dash}{flag} '{secret}'"
        if form == 2:
            return f'{dash}{flag}="{secret}"'
        if form == 3:
            return f"{dash}{flag} {one_word}"
        if form == 4:
            return f"{dash}{flag}={one_word}"
        if form == 5:
            q = rng.choice(["", "'", '"'])
            return f"-u {q}user{rng.randint(0, 9)}:{secret if q else one_word}{q}"
        q = rng.choice(["", "'"])
        return f"--user={q}u:{secret if q else one_word}{q}"

    def test_twenty_thousand_random_lines(self):
        rng = random.Random(4421)
        for n in range(20000):
            parts = [rng.choice(self.ORDINARY) for _ in range(rng.randint(0, 3))]
            if n % 2:
                secret = self._secret(rng, n)
                parts.insert(rng.randint(0, len(parts)), "tool " + self._credential(rng, secret))
                line = rng.choice([" && ", " ; ", " | ", "\n"]).join(parts)
                out = s.scrub(line)
                for word in secret.split(" "):
                    assert word not in out, (line, out)
                assert s.scrub(out) == out, line
            else:
                line = rng.choice([" && ", " ; ", " | ", "\n"]).join(parts)
                assert s.scrub(line) == line, line


class TestSeparatorWordsAreNotTheValue:
    """A lone separator word is the separator the label arms accept, not the value.

    Read as the value, ``=`` was withheld and the credential after it survived:
    the label arm had redacted these shapes before the flag pass existed.
    """

    LONG = W1 + W2 + W3 + "Lm8q"  # 16+ characters, the token arm's floor

    @pytest.mark.parametrize(
        "text,expected",
        [
            (f"tool --password = {W1} next", f"tool --password = {R} next"),
            (f"tool --password : {W1} next", f"tool --password : {R} next"),
            (f"tool --password is {W1} next", f"tool --password is {R} next"),
            (f"tool --api-key is {LONG} next", f"tool --api-key is {R} next"),
            (f"tool --password: {W1}", f"tool --password: {R}"),
            (f"tool --password= {W1}", f"tool --password= {R}"),
            (f"tool --password = -{W1}", f"tool --password = {R}"),
        ],
    )
    def test_the_value_after_the_separator_is_withheld(self, text, expected):
        assert s.scrub(text) == expected


class TestAFlagNamedInsideAQuotedArgument:
    """A flag named in quoted prose ends its value where that quote closes.

    Read as a fresh word, the enclosing closing quote looked like an opener with
    no partner and everything after it, every later line included, was withheld.
    """

    @pytest.mark.parametrize(
        "text,expected",
        [
            (
                'gh pr create --title "fix(hooks): redact --password values" --body-file b.md\nnext line\nlast',
                'gh pr create --title "fix(hooks): redact --password [REDACTED]" --body-file b.md\nnext line\nlast',
            ),
            (
                'git commit -m "docs: the --secret flag" -m "more"\nok',
                'git commit -m "docs: the --secret [REDACTED]" -m "more"\nok',
            ),
            (
                "echo 'use --pin carefully' ; ls\nnext",
                "echo 'use --pin [REDACTED]' ; ls\nnext",
            ),
            (
                "don't pass --token values, it's unsafe\nnext line",
                # The comma belongs to the shell word, so it goes with it.
                "don't pass --token [REDACTED] it's unsafe\nnext line",
            ),
            (
                'echo "the --password"\nnext',
                'echo "the --password"\nnext',
            ),
            (
                'curl -m "see curl -u alice:x" -o out\nnext',
                'curl -m "see curl -u alice:[REDACTED]" -o out\nnext',
            ),
        ],
    )
    def test_later_text_is_kept(self, text, expected):
        assert s.scrub(text) == expected

    def test_a_quoted_value_inside_a_quoted_command_is_withheld_whole(self):
        out = s.scrub(f"bash -c \"mysql --password '{W1} {W2}' db\"\nnext")
        assert out == f'bash -c "mysql --password {R} db"\nnext'

    def test_a_mid_word_quote_with_no_partner_ends_the_word(self):
        assert s.scrub(f"tool --password {W1}'s\nnext") == f"tool --password {R}'s\nnext"


class TestEncodedQuotingAtAnyDepth:
    """A serialized command's value is withheld to the end of the line at any
    JSON encoding depth: two levels put ``\\\\\\"`` before the quote."""

    @pytest.mark.parametrize("levels", [1, 2, 3])
    def test_every_word_of_the_value_is_withheld(self, levels):
        import json

        value = f'mysql --password "{W1} {W2}" db'
        for _ in range(levels):
            value = json.dumps(value)
        out = s.scrub(value + "\nnext")
        assert _no_secret_left(out), out
        assert out.endswith("\nnext")


class TestUserFlagNonCredentialColons:
    @pytest.mark.parametrize(
        "text",
        [
            "date -u +%H:%M:%S",
            "date -u '+%Y-%m-%d %H:%M'",
            "rsync -avu host.example:/srv/data ./data",
        ],
    )
    def test_unchanged(self, text):
        assert s.scrub(text) == text


class TestNoValueMainRedactsIsNowKept:
    """Differential against the scrubber without the two flag passes.

    Adding passes can only redact more, unless a new pass consumes text an older
    pass needed (a separator read as the value). The oracle is this module with
    the two passes switched off, which is main's scrubber: every other arm is
    unchanged. Every case whose secret the oracle redacts must stay redacted.
    """

    S = "zq" + "Xv7" + "KpQ"
    LABELS = ["password", "passwd", "pass", "pwd", "pin", "token", "secret", "api-key",
              "api_key", "access-token", "api key", "client_secret", "passphrase"]
    PREFIXES = ["--", "-", "", "$", '"', "'", '{"', '\\"', "x="]
    SEPS = [" ", "=", " = ", " =", "= ", ":", ": ", " : ", " is ", "\t", "  ", '"=',
            '": ', '\\": ', '="', "='", ' "', '" ', '"="', " is: "]

    def _values(self):
        v = self.S
        return [v, f'"{v}"', f"'{v}'", f'"{v} x"', f"'x {v}'", f"{v},y", f"[{v}]", f'\\"{v}\\"']

    def test_no_new_leak(self, monkeypatch):
        import itertools

        cases = [
            f"cmd {p}{label}{sep}{v} tail"
            for p, label, sep, v in itertools.product(
                self.PREFIXES, self.LABELS, self.SEPS, self._values()
            )
        ]
        new = [s.scrub(t) for t in cases]
        monkeypatch.setattr(s, "_redact_credential_flags", lambda t: t)
        monkeypatch.setattr(s, "_redact_user_passwords", lambda t: t)
        old = [s.scrub(t) for t in cases]
        oracle_redacts = sum(self.S not in o for o in old)
        assert oracle_redacts > 1000, "the oracle redacts nothing: the comparison is empty"
        leaks = [t for t, o, n in zip(cases, old, new, strict=True) if self.S not in o and self.S in n]
        assert not leaks, leaks[:5]


class TestALabelInsideTheValueKeepsItsPair:
    """A value word that is itself a label: the label arm's pair is withheld whole.

    When the flag pass ran before the label arms, withholding only the label word
    left the label arm no anchor, and the credential after it survived. The flag
    passes now run last, on what the label arms already withheld.
    """

    LONG = W1 + W2 + W3 + "Lm8q"

    @pytest.mark.parametrize(
        "text",
        [
            f"vault write auth/x --secret password: {W1}",
            f"login --token PASSWORD = {W1}",
            f"setup --pin pin is {W1}",
            f"tool --api-key api_key: {LONG}",
            f"echo --password Password: '{W1}'",
            f"curl -u x:y password = {W1}",
        ],
    )
    def test_the_credential_after_the_label_is_withheld(self, text):
        out = s.scrub(text)
        assert W1 not in out and "Lm8q" not in out, out
        assert s.scrub(out) == out


class TestAPathLeadingPassword:
    """A ``/``-leading password half is withheld everywhere except a short ``-u``
    on rsync or scp, whose operand is a remote path."""

    @pytest.mark.parametrize(
        "text",
        [
            f"curl -u admin:/{W1} h",
            f"curl --user admin:/{W1} h",
            f"curl --user=admin:/{W1} h",
            f"curl --proxy-user bob:/{W1} h",
            f"echo rsync; curl -u admin:/{W1} h",
        ],
    )
    def test_redacted(self, text):
        assert W1 not in s.scrub(text)

    @pytest.mark.parametrize(
        "text",
        [
            "rsync -avu host.example:/srv/x ./y",
            "sudo rsync -avu host.example:/srv/x ./y",
            "/usr/bin/scp -u host.example:/srv/x .",
            "cd /a && rsync -avu host.example:/srv/x ./y",
        ],
    )
    def test_remote_paths_are_kept(self, text):
        assert s.scrub(text) == text


class TestRedactedPunctuationIsStable:
    def test_a_redacted_value_with_trailing_punctuation_scrubs_to_itself(self):
        text = f"tool --token {R}, next"
        assert s.scrub(text) == text
