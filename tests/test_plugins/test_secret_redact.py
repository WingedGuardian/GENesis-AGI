"""genesis-secret-redact: the value loader (bin/known_values.py).

The mod that uses it (hooks/register.js) is tested in ``test_secret_redact_mod.py`` and, against a real Claude
Code, by ``scripts/e2e_secret_redact.py``. Canary values only.
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import random
import sys
import urllib.parse
from io import StringIO
from pathlib import Path

import pytest

from tests.conftest import private_module

REPO = Path(__file__).resolve().parents[2]
BIN = REPO / "plugins" / "genesis-secret-redact" / "bin"
KEY = "ck_" + "a1b2c3d4e5f60718293a4b5c6d7e8f90"
TOKEN = "ct_Zx9-Yw8_Vu7Ts6Rq5Po4Nm3Lk2Ji1H"

known = private_module("_test_known_values", BIN / "known_values.py")


@pytest.fixture
def secrets_file(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    path.write_text(
        f'CANARY_API_KEY={KEY}\nexport CANARY_SERVICE_TOKEN="{TOKEN}"\nCANARY_REGION=us-east-1-long-name\n'
    )
    monkeypatch.setenv("SECRETS_PATH", str(path))
    return path


# --- loader -------------------------------------------------------------------------------------------------


DOTENV_CASES = [
    "PLAIN=value",
    "export EXPORTED=value2",
    "SINGLE='single quoted value'",
    'DOUBLE="double quoted value"',
    'ESCAPED="line\\nnext \\"q\\""',
    "INLINE=value # trailing comment",
    "HASH_IN_VALUE=abc#def",
    "EQUALS=a=b=c",
    "EMPTY=",
    "   SPACED   =   spaced value   ",
    "# a comment",
    "",
]


def test_selection_is_by_name_and_length():
    pairs = {
        "CANARY_API_KEY": KEY,
        "SHORT_TOKEN": "tooshort",
        "USER_TIMEZONE": "America/New_York_Region_Name",
        "SERVICE_PASSWORD": "p" * 16,
    }
    vars_ = {n["var"] for n in known.needles(pairs)}
    assert vars_ == {"CANARY_API_KEY", "SERVICE_PASSWORD"}


def test_selection_ignores_the_case_of_the_name():
    assert {n["var"] for n in known.needles({"service_api_key": KEY, "Client_Secret": TOKEN})} == {
        "service_api_key",
        "Client_Secret",
    }


@pytest.mark.parametrize(
    "name", ["GH_PAT", "DB_PW", "STRIPE_SK", "WG_PSK", "HMAC_BASE", "BEARER", "AWS_CREDS"]
)
def test_short_markers_select_a_name(name):
    assert known.needles({name: KEY})


@pytest.mark.parametrize(
    "name", ["DATA_PATH", "PATTERN_FILE", "SKILL_DIR", "PWA_HOST", "DISPATCH_MODE"]
)
def test_short_markers_match_whole_words_of_the_name_only(name):
    assert known.needles({name: "/srv/data/some/long/directory/name"}) == []


def test_no_spelling_is_shorter_than_the_minimum():
    # A needle of a few characters would rewrite ordinary text; an empty one would split every character.
    assert known.spellings("short") == set()
    assert known.spellings("") == set()
    assert min(len(n["text"]) for n in known.needles({"CANARY_API_KEY": KEY})) >= known.MIN_LEN


def test_webhooks_dsns_and_credential_urls_are_secrets():
    pairs = {
        "CHAT_WEBHOOK_GENERAL": "https://chat.example/api/webhooks/123/abcdefghij",
        "SENTRY_DSN": "https://abcdef0123456789@sentry.example/1",
        "DATABASE_URL": "postgres://app:s3cretpassw0rd@db.example:5432/app",
        "CACHE_URL": "redis://:s3cretpassw0rd@cache.example:6379/0",
        "MIRROR_URL": "https://ghx_0123456789abcdef0123@git.example/org/repo.git",
        "REGISTRY_URL": "https://registry.example:443/@scope/package-name",
        "HA_URL": "http://homeassistant.example:8123",
    }
    assert {n["var"] for n in known.needles(pairs)} == {
        "CHAT_WEBHOOK_GENERAL",
        "SENTRY_DSN",
        "DATABASE_URL",
        "CACHE_URL",
        "MIRROR_URL",
    }


#: Every character python-dotenv's grammar treats specially, plus two plain ones.
FALLBACK_ALPHABET = [
    "A",
    "b",
    "=",
    "'",
    '"',
    "\\",
    "#",
    " ",
    "\n",
    "\r",
    "\t",
    "$",
    "{",
    "}",
    ":",
    "-",
    "n",
]
FALLBACK_CASES = [
    'QUOTED_COMMENT="quoted value" # note',
    "SINGLE_COMMENT='single value' # note",
    'MULTI="first line of it\nsecond line of it"',
    "export EXPORTED=plain-value-here",
    'ESCAPED="a\\"b\\\\c\\nd\\te\\\'f"',
    "SINGLE_ESCAPED='abc\\'def\\\\ghi'",
    "UNQUOTED=value with spaces # comment",
    "BASE=long-secret-prefix\nDERIVED=${BASE}-suffix\nDEFAULTED=${MISSING_NAME:-fallback}-x\nGONE=${MISSING_NAME}",
    "SINGLE_INTERPOLATED='${BASE}'\nBASE=later",
    "NO_VALUE\nNO_VALUE_REF=${NO_VALUE}x",
    "'quoted key'=value",
    "BAD LINE HERE\nGOOD=after-a-bad-line",
    'UNCLOSED="never closed\nNEXT=value',
    "CRLF=one\r\nSECOND=two\r\n",
]


def _dotenv_values(text: str) -> dict[str, str]:
    dotenv = pytest.importorskip("dotenv")
    return {k: v for k, v in dotenv.dotenv_values(stream=StringIO(text)).items() if v is not None}


@pytest.mark.parametrize("text", [*FALLBACK_CASES, "\n".join(DOTENV_CASES) + "\n"])
def test_fallback_parser_matches_python_dotenv_on_named_shapes(text):
    assert known._fallback_parse(text) == _dotenv_values(text)


def test_fallback_interpolation_prefers_the_files_keys_to_the_environment(monkeypatch):
    monkeypatch.setenv("REDACT_TEST_BASE", "from-the-environment")
    monkeypatch.setenv("REDACT_TEST_ONLY_ENV", "only-in-the-environment")
    text = "REDACT_TEST_BASE=from-the-file\nA=${REDACT_TEST_BASE}\nB=${REDACT_TEST_ONLY_ENV}\n"
    parsed = known._fallback_parse(text)
    assert (parsed["A"], parsed["B"]) == ("from-the-file", "only-in-the-environment")
    assert parsed == _dotenv_values(text)


def test_fallback_parser_matches_python_dotenv_on_every_short_input(caplog):
    """Differential over the grammar: every string of up to 3 special characters, and 20,000 seeded random
    strings of 4 to 40. The fallback is a port of python-dotenv's parser, so any difference is a bug."""
    pytest.importorskip("dotenv")
    caplog.set_level("CRITICAL")  # python-dotenv warns on each line it cannot parse
    rng = random.Random(20261112)
    texts = ["".join(c) for n in range(4) for c in itertools.product(FALLBACK_ALPHABET, repeat=n)]
    texts += [
        "".join(rng.choice(FALLBACK_ALPHABET) for _ in range(rng.randint(4, 40)))
        for _ in range(20000)
    ]
    different = [t for t in texts if known._fallback_parse(t) != _dotenv_values(t)]
    assert different == []


#: python-dotenv 1.2.3 changed three rules; one input per rule, with what each rule set reads from it.
DIALECT_CASES = [
    (
        "A='a\\\\' #'\n",
        {"A": "a\\"},
        {"A": "a\\' #"},
    ),  # a backslash escapes any character inside quotes
    ("﻿A=b\n", {"A": "b"}, {"﻿A": "b"}),  # a leading byte-order mark is dropped
    ("A= #b\n", {"A": ""}, {"A": "#b"}),  # after `=` and a blank, `#` starts a comment
]


def test_the_port_follows_the_rules_of_the_python_dotenv_in_use():
    pytest.importorskip("dotenv")
    rules = known._dialect()
    assert len(set(rules)) == 1, rules  # the three rules changed in one release
    for text, newer, older in DIALECT_CASES:
        assert known._fallback_parse(text) == _dotenv_values(text) == (newer if rules[0] else older)


def test_without_python_dotenv_the_port_follows_the_newer_rules(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "dotenv", None
    )  # makes `from dotenv import ...` raise ImportError
    known._dialect.cache_clear()
    try:
        assert known._dialect() == (True, True, True)
        for text, newer, _older in DIALECT_CASES:
            assert known._fallback_parse(text) == newer
    finally:
        known._dialect.cache_clear()


def test_parse_uses_python_dotenv_when_importable_and_the_port_when_not(monkeypatch):
    pytest.importorskip("dotenv")
    text = "A=x\nB=${A}-y\n"
    with monkeypatch.context() as patched:
        patched.setattr(known, "_fallback_parse", lambda _text: pytest.fail("the port was used"))
        assert known.parse_dotenv(text)["B"] == "x-y"
    with monkeypatch.context() as patched:
        patched.setitem(
            sys.modules, "dotenv", None
        )  # makes `from dotenv import ...` raise ImportError
        patched.setattr(known, "_fallback_parse", lambda _text: {"from": "the port"})
        assert known.parse_dotenv(text) == {"from": "the port"}


def test_each_long_line_of_a_multiline_value_has_its_own_placeholder():
    value = "-----BEGIN PRIVATE KEY-----\nMIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX\nshort\n-----END PRIVATE KEY-----"
    found, restore = known.select({"PRIVATE_KEY_PEM": value})
    by_text = {n["text"]: n["placeholder"] for n in found}
    assert by_text["MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX"] == "[secrets.env:PRIVATE_KEY_PEM#2]"
    assert by_text[value] == "[secrets.env:PRIVATE_KEY_PEM]"
    # PEM armor is the same in every key file, and a short line would rewrite ordinary text: neither is a needle.
    assert not {"-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----", "short"} & set(by_text)
    assert restore["[secrets.env:PRIVATE_KEY_PEM#2]"] == "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX"


def test_every_needle_text_has_its_own_placeholder_and_restore_is_its_inverse(secrets_file):
    result = known.load(known.repo_root(str(REPO)))
    placeholders = [n["placeholder"] for n in result["needles"]]
    assert len(placeholders) >= 6 and len(set(placeholders)) == len(placeholders)
    assert result["restore"] == {n["placeholder"]: n["text"] for n in result["needles"]}
    # The file's own spelling carries the plain name; every other text of that value a numbered one.
    by_text = {n["text"]: n["placeholder"] for n in result["needles"]}
    assert by_text[KEY] == "[secrets.env:CANARY_API_KEY]"
    others = {
        p for t, p in by_text.items() if t != KEY and p.startswith("[secrets.env:CANARY_API_KEY")
    }
    assert others and all("~" in p for p in others)


def test_a_line_placeholder_restores_the_files_spelling_of_that_line():
    # A line-by-line read of the file (grep) shows one line of a quoted multi-line value, escapes and all.
    source = 'MULTI_SECRET="first-line-of-the-secret\nsecond \\"line\\" of the secret"\n'
    found, restore = known.select(known.parse_dotenv(source), source)
    by_text = {n["text"]: n["placeholder"] for n in found}
    assert by_text['second \\"line\\" of the secret'] == "[secrets.env:MULTI_SECRET#2]"
    assert by_text['second "line" of the secret'] == "[secrets.env:MULTI_SECRET#2~1]"
    assert restore["[secrets.env:MULTI_SECRET#2]"] == 'second \\"line\\" of the secret'
    assert restore["[secrets.env:MULTI_SECRET#2~1]"] == 'second "line" of the secret'


def test_an_interpolated_values_expression_is_not_a_needle():
    # The expression is not the secret: as a needle it would hide ordinary config text from the session.
    source = "OTHER=x\nCANARY_API_KEY=${OTHER}a1b2c3d4e5f60718293a\n"
    redacted, restore = _redact(source)
    assert redacted == source
    assert "${OTHER}a1b2c3d4e5f60718293a" not in restore.values()
    # The value it expands to is a needle with a numbered name, and restores to itself.
    placeholder = next(p for p, t in restore.items() if t == "xa1b2c3d4e5f60718293a")
    assert placeholder.startswith("[secrets.env:CANARY_API_KEY~")


def test_the_last_binding_of_a_name_gives_the_restore_text():
    source = "CANARY_API_KEY='first-first-first-first'\nCANARY_API_KEY='a1b2c3d4e5f60718\\'q'\n"
    assert _redact(source)[1]["[secrets.env:CANARY_API_KEY]"] == "a1b2c3d4e5f60718\\'q"


def test_odd_variable_names_still_get_placeholders_of_their_own():
    # A name may hold "~" or "]" (python-dotenv allows both): neither may collide with another variable's
    # numbered placeholder, or end the placeholder early.
    pairs = {"A_KEY": "p" * 20 + "%", "A_KEY~1": "q" * 20, "ODD]KEY": "r" * 20}
    found, restore = known.select(pairs)
    placeholders = [n["placeholder"] for n in found]
    assert len(set(placeholders)) == len(placeholders)
    assert all(known._PLACEHOLDER.fullmatch(p) for p in placeholders)
    assert restore == {n["placeholder"]: n["text"] for n in found}
    assert {n["var"] for n in found} == set(pairs)


def test_a_value_that_holds_placeholder_text_is_never_a_needle():
    pairs = {"CANARY_API_KEY": KEY, "OTHER_TOKEN": "[secrets.env:CANARY_API_KEY]"}
    found, restore = known.select(pairs)
    assert {n["var"] for n in found} == {"CANARY_API_KEY"}
    assert "[secrets.env:CANARY_API_KEY]" not in restore.values()


def _redact(source: str) -> tuple[str, dict[str, str]]:
    """``source`` with every needle replaced, longest first, as every consumer does it; and the restore map."""
    found, restore = known.select(known.parse_dotenv(source), source)
    for needle in found:
        source = source.replace(needle["text"], needle["placeholder"])
    return source, restore


def _restored(text: str, restore: dict[str, str]) -> str:
    for placeholder, in_file in restore.items():
        text = text.replace(placeholder, in_file)
    return text


@pytest.mark.parametrize(
    ("line", "in_file"),
    [
        (f"CANARY_API_KEY={KEY}", KEY),
        (f"CANARY_API_KEY='{KEY}'", KEY),
        # The file stores escapes; the parsed value has none. Restoring the parsed value inside those quotes
        # would break the line, and an Edit's old_string would not match the file.
        ('CANARY_API_KEY="a1b2c3d4e5f60718\\"quoted\\""', 'a1b2c3d4e5f60718\\"quoted\\"'),
        ("CANARY_API_KEY='a1b2c3d4e5f60718\\'quoted\\''", "a1b2c3d4e5f60718\\'quoted\\'"),
        ("CANARY_API_KEY='a1b2c3d4e5f6071829+\\\\'", "a1b2c3d4e5f6071829+\\\\"),
        (
            'CANARY_API_KEY="line-one-of-the-key\\nline-two-of-the-key"',
            "line-one-of-the-key\\nline-two-of-the-key",
        ),
    ],
)
def test_the_restore_text_is_what_the_file_holds(line, in_file):
    source = f"OTHER=x\n{line}\n"
    redacted, restore = _redact(source)
    assert restore["[secrets.env:CANARY_API_KEY]"] == in_file
    assert _restored(redacted, restore) == source


def _quoted(value: str, style: str) -> str:
    """``value`` as a dotenv line in one quoting style (python-dotenv's escapes for each)."""
    if style == "single":
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    body = value.replace("\\", "\\\\").replace('"', '\\"')
    if style == "double-escaped-newline":
        body = body.replace("\n", "\\n")
    return '"' + body + '"'


@pytest.mark.parametrize("style", ["single", "double", "double-escaped-newline"])
def test_redacting_then_restoring_a_file_gives_the_same_bytes(style):
    """Seeded values over the characters quoting has to escape: read the file, redact it, write it back."""
    rng = random.Random(4439)
    alphabet = "abcXYZ019_-+/=.: #'\"\\\n"
    for _ in range(1500):
        value = "".join(rng.choice(alphabet) for _ in range(rng.randint(16, 48))).strip()
        if len(value) < known.MIN_LEN or "\n\n" in value:
            continue
        source = f"FIRST=1\nCANARY_API_KEY={_quoted(value, style)}\nLAST=2\n"
        assert known.parse_dotenv(source)["CANARY_API_KEY"] == value, source
        redacted, restore = _redact(source)
        assert value not in redacted and "[secrets.env:CANARY_API_KEY" in redacted, source
        assert _restored(redacted, restore) == source, source


def _bare_ok(value: str) -> bool:
    return not any(c in value for c in "'\"\\#\n ") and "${" not in value


def _files_of_several_bindings(rng: random.Random, count: int):
    """Secrets files that give one placeholder a chance to stand for two texts: a second variable holding the
    same value in other quoting, a name bound twice, and an encoded copy of a value in a line that is not a
    secret."""
    alphabet = "abcXYZ019_-+/=.:%&'\"\\ "
    made = 0
    while made < count:
        values = [
            v
            for v in (
                "".join(rng.choice(alphabet) for _ in range(rng.randint(16, 30))).strip()
                for _ in range(rng.randint(1, 3))
            )
            if len(v) >= known.MIN_LEN and "${" not in v
        ]
        if not values:
            continue

        def line(name: str, value: str) -> str:
            styles = ["single", "double"] + (["bare"] if _bare_ok(value) else [])
            style = rng.choice(styles)
            return f"{name}={value if style == 'bare' else _quoted(value, style)}"

        lines = [line(f"V{i}_API_KEY", v) for i, v in enumerate(values)]
        lines.append(line("COPY_API_KEY", rng.choice(values)))
        if rng.random() < 0.5:
            lines.append(line("V0_API_KEY", values[0]))
        copied = rng.choice(values)
        form = rng.choice(
            [
                copied,
                urllib.parse.quote(copied, safe=""),
                json.dumps(copied)[1:-1],
                base64.b64encode(copied.encode()).decode(),
            ]
        )
        lines.append(f"# was: {form}")
        rng.shuffle(lines)
        made += 1
        yield "FIRST=1\n" + "\n".join(lines) + "\nLAST=2\n"


def test_redact_then_restore_is_exact_for_files_whose_texts_collide():
    """The whole file, and every run of consecutive lines (what Read with a limit, grep or sed -n hand over)."""
    for source in _files_of_several_bindings(random.Random(4439), 300):
        found, restore = known.select(known.parse_dotenv(source), source)
        lines = source.splitlines(keepends=True)
        chunks = [source] + [
            "".join(lines[i:j]) for i in range(len(lines)) for j in range(i + 1, len(lines) + 1)
        ]
        for chunk in chunks:
            redacted = chunk
            for needle in found:
                redacted = redacted.replace(needle["text"], needle["placeholder"])
            assert _restored(redacted, restore) == chunk, source


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (
            'é-abcdefghijklmnop"quoted"',
            'é-abcdefghijklmnop\\"quoted\\"',
        ),  # JavaScript, jq: non-ASCII kept
        ("https://hooks.example/a/b?x=1&y=2", "https:\\/\\/hooks.example\\/a\\/b?x=1&y=2"),  # PHP
        ("https://hooks.example/a/b?x=1&y=2", "https://hooks.example/a/b?x=1\\u0026y=2"),  # Go
        ("p&ss<word>-0123456789", "p&amp;ss&lt;word&gt;-0123456789"),  # HTML
    ],
)
def test_the_forms_other_encoders_write_are_needles(value, shown):
    assert shown in {n["text"] for n in known.needles({"SERVICE_WEBHOOK": value})}


def test_the_password_inside_a_credential_url_is_a_needle_alone():
    texts = {
        n["text"]
        for n in known.needles(
            {
                "DATABASE_URL": "postgres://app:s3cret%2Fpassw0rd-long@db.example:5432/app",
                "MIRROR_URL": "https://ghx_0123456789abcdef0123@git.example/org/repo.git",
            }
        )
    }
    assert {"s3cret%2Fpassw0rd-long", "s3cret/passw0rd-long", "ghx_0123456789abcdef0123"} <= texts
    # The extracted parts get the same JSON spellings as a whole value (a JSON result escapes `/`).
    assert "s3cret\\/passw0rd-long" in texts


def test_load_reports_restore_texts_and_overwritten_keys(tmp_path, monkeypatch, capsys):
    path = tmp_path / "secrets.env"
    path.write_text(
        'CANARY_API_KEY="a1b2c3d4e5f60718\\"quoted\\""\n'
        "LOST_TOKEN=[secrets.env:LOST_TOKEN]\nPLAIN_URL=https://x.example/[secrets.env:GONE_KEY]\n"
    )
    monkeypatch.setenv("SECRETS_PATH", str(path))
    assert known.main(["--json", "--repo-root", str(REPO)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == known.load(known.repo_root(str(REPO)))
    assert printed["restore"]["[secrets.env:CANARY_API_KEY]"] == 'a1b2c3d4e5f60718\\"quoted\\"'
    assert printed["restore"] == {n["placeholder"]: n["text"] for n in printed["needles"]}
    assert printed["overwritten"] == ["LOST_TOKEN", "PLAIN_URL"]
    assert {n["var"] for n in printed["needles"]} == {"CANARY_API_KEY"}
    assert all(set(n) == {"var", "text", "placeholder"} for n in printed["needles"])


@pytest.mark.parametrize("prefix_len", range(6))
def test_a_base64_spelling_is_found_at_every_alignment(prefix_len):
    texts = [n["text"] for n in known.needles({"CANARY_API_KEY": KEY})]
    blob = base64.b64encode(b"u" * prefix_len + KEY.encode() + b":trailer").decode()
    assert any(t in blob for t in texts if t != KEY)


def test_url_and_json_spellings():
    value = 'abc/def+ghi=jkl"mno\\pqr stu'
    texts = {n["text"] for n in known.needles({"API_KEY": value})}
    assert {
        value,
        "abc%2Fdef%2Bghi%3Djkl%22mno%5Cpqr%20stu",  # percent-encoded
        "abc%2Fdef%2Bghi%3Djkl%22mno%5Cpqr+stu",  # form-encoded
        'abc/def+ghi=jkl\\"mno\\\\pqr stu',  # JSON-escaped
    } <= texts


def test_base64url_spelling():
    value = "??>>??>>??>>??>>??>>"  # its base64 holds both + and /
    plain = base64.b64encode(value.encode()).decode()
    urlsafe = base64.urlsafe_b64encode(value.encode()).decode()
    assert "+" in plain and "/" in plain
    texts = {n["text"] for n in known.needles({"API_KEY": value})}
    assert any(t in plain for t in texts if t != value)
    assert any(t in urlsafe and t not in plain for t in texts)


def test_needles_are_longest_first():
    lengths = [
        len(n["text"])
        for n in known.needles({"CANARY_API_KEY": KEY, "CANARY_SERVICE_TOKEN": TOKEN})
    ]
    assert lengths == sorted(lengths, reverse=True)


def test_a_crlf_files_multiline_value_is_matched_and_restored_with_its_line_ends(
    tmp_path, monkeypatch
):
    path = tmp_path / "secrets.env"
    path.write_bytes(
        b'MULTI_SECRET="first-line-of-the-secret\r\nsecond-line-of-the-secret"\r\nNEXT=1\r\n'
    )
    monkeypatch.setenv("SECRETS_PATH", str(path))
    result = known.load(known.repo_root(str(REPO)))
    in_file = "first-line-of-the-secret\r\nsecond-line-of-the-secret"
    assert result["restore"]["[secrets.env:MULTI_SECRET]"] == in_file
    texts = {n["text"] for n in result["needles"]}
    # As `cat` shows it, and as the runtime holds it.
    assert {in_file, in_file.replace("\r\n", "\n")} <= texts


def test_load_uses_the_runtime_resolver(secrets_file):
    result = known.load(known.repo_root(str(REPO)))
    assert result["path"] == str(secrets_file)
    assert {n["var"] for n in result["needles"]} == {"CANARY_API_KEY", "CANARY_SERVICE_TOKEN"}


def test_missing_file_gives_no_needles(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_PATH", str(tmp_path / "absent.env"))
    result = known.load(known.repo_root(str(REPO)))
    assert (result["needles"], result["restore"], result["overwritten"]) == ([], {}, [])


def test_unreadable_file_exits_2_and_prints_no_value(secrets_file, capsys):
    secrets_file.chmod(0)
    try:
        if os.access(secrets_file, os.R_OK):
            pytest.skip("running as a user that can read a mode-000 file")
        assert known.main(["--json", "--repo-root", str(REPO)]) == 2
    finally:
        secrets_file.chmod(0o600)
    out = capsys.readouterr()
    assert KEY not in out.out + out.err


def test_summary_mode_prints_no_value(secrets_file, capsys):
    assert known.main(["--repo-root", str(REPO)]) == 0
    assert KEY not in capsys.readouterr().out


def test_a_credential_url_the_url_parser_rejects_still_loads_and_is_matched_whole():
    # urlsplit raises on an unbalanced bracket; that must not cost the other values their redaction.
    broken = "redis://:pa[ssword1234567@cache.example:6379/0"
    found = {n["text"]: n["var"] for n in known.needles({"CACHE_URL": broken, "A_KEY": "k" * 20})}
    assert found[broken] == "CACHE_URL" and found["k" * 20] == "A_KEY"


def test_plugin_manifest_wires_the_mod_and_the_publish_guard():
    hooks = json.loads((BIN.parent / "hooks" / "hooks.json").read_text())
    assert hooks["modules"] == ["./register.js"]
    (entry,) = hooks["hooks"]["PreToolUse"]
    assert entry["matcher"] == "^(Monitor|Bash)$"
    assert "bin/publish_guard.py" in entry["hooks"][0]["command"]


def test_a_name_holding_a_bracket_gets_a_placeholder_that_holds_none():
    found, _ = known.select({"ODD[KEY": "r" * 20})
    placeholders = [n["placeholder"] for n in found]
    assert "[secrets.env:ODD_KEY]" in placeholders
    assert all(p.startswith("[secrets.env:ODD_KEY") and p.count("[") == 1 for p in placeholders)
