"""Procedure writes never store a credential in their text (#4859). All values here
are synthetic."""

from __future__ import annotations

import json

import pytest

from genesis.db.crud import procedural
from genesis.learning.procedural.credential_scrub import scrub_text, scrub_value

FAKE = "Zq7-fake-Secret!9"


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        (f"run sshpass -p '{FAKE}' ssh user@host.example", "ssh user@host.example"),
        (f"run sshpass -p {FAKE} ssh h", "ssh h"),
        (f"log in with password {FAKE} then sync", "then sync"),
        (f"password '{FAKE}' then go", "then go"),
        (f"passphrase {FAKE}", "passphrase"),
        (f"the password: {FAKE}", "the password"),
        ("export EXAMPLE_API_KEY=abcdef0123456789abcdef && make", "&& make"),
    ],
)
def test_credential_values_are_redacted_and_the_rest_survives(text, kept):
    cleaned, found = scrub_text(text)
    assert FAKE not in cleaned and "abcdef0123456789abcdef" not in cleaned
    assert "redacted" in cleaned.lower() and kept in cleaned and found


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        # sshpass -p's argument is always the password, whatever its length.
        ("sshpass -p ab ssh h", "ab"),
        ("sshpass -p a ssh h", " a "),
        ("sshpass -p 'q' ssh h", "'q'"),
        ("sshpass -p'ab' ssh h", "'ab'"),
        ("sshpass -p '$lit9' ssh h", "$lit9"),  # single quotes: literal, not a variable
        # A quoted value holding the other quote kind is redacted whole.
        ("sshpass -p 'Zq7\"fake!9' ssh h", "Zq7"),
        ('sshpass -p "Zq7\'fake!9" ssh h', "Zq7"),
        ("password 'Zq7\"fake!9' then go", "Zq7"),
        ('password "Zq7\'fake!9" then go', "Zq7"),
        # A $ after the first character is a symbol, not a variable.
        ("password abcde$ then go", "abcde"),
        # Options before -p; -P keeps its prompt argument.
        ("sshpass -e -p Zq7k ssh h", "Zq7k"),
        ("sshpass -v -p Zq7k ssh h", "Zq7k"),
        ("sshpass -o x -p Zq7k ssh h", "Zq7k"),
        # A newline, a backslash-newline or a non-breaking space before the value.
        ("sshpass -p\nZq7k ssh h", "Zq7k"),
        ("sshpass -p \\\nZq7k ssh h", "Zq7k"),
        ("sshpass -p Zq7k ssh h", "Zq7k"),
    ],
)
def test_short_mixed_quote_and_dollar_values_are_redacted(text, secret):
    cleaned, found = scrub_text(text)
    assert secret not in cleaned and "[redacted]" in cleaned and found
    assert cleaned.split("[redacted]")[1].strip() in ("ssh h", "then go")


def test_the_sshpass_prompt_option_keeps_its_argument():
    cleaned, _ = scrub_text("sshpass -P assword: -p Zq7k ssh h")
    assert cleaned == "sshpass -P assword: -p [redacted] ssh h"


@pytest.mark.parametrize("prompt", ['"Enter password:"', "'pass word'", '""', "-Pprompt"])
def test_a_quoted_prompt_with_a_space_does_not_hide_the_password(prompt):
    opt = prompt if prompt.startswith("-P") else f"-P {prompt}"
    cleaned, _ = scrub_text(f"sshpass {opt} -p Zq7wXvk ssh h")
    assert cleaned == f"sshpass {opt} -p [redacted] ssh h"


def test_a_blank_line_after_dash_p_is_not_read_as_its_value():
    text = "sshpass -p\n\nThe next paragraph"
    assert scrub_text(text) == (text, [])


def test_a_variable_reference_keeps_the_rest_of_the_command():
    """The shared redactor may take the reference itself (accepted: a lost name,
    never a leaked value), but the command around it survives."""
    cleaned, _ = scrub_text("export GITHUB_TOKEN=$GH_TOKEN && gh pr list && echo done")
    assert cleaned.endswith("&& gh pr list && echo done")


@pytest.mark.parametrize(
    "text",
    [
        "password: $uperman99 then",
        "PASSWORD=$uperman99 then",
        'DB_PASSWORD="$uperman99" then',
        "TOKEN='$Zq7fakeSecret9' then",
    ],
)
def test_a_dollar_prefixed_literal_under_a_label_is_redacted(text):
    cleaned, _ = scrub_text(text)
    assert "uperman99" not in cleaned and "Zq7fakeSecret9" not in cleaned
    assert cleaned.endswith(" then")


def test_a_value_under_a_credential_key_is_redacted_and_other_members_survive():
    found: list[str] = []
    value = {
        "password": "x7!Secret",
        "secret": "abc",
        "token": 5,
        "userPassword": "q",
        "api_key": "short",
        "passphrase": "$Zq7wXv1k",  # a $-prefixed value under a key is still a value
        "description": "then check the log",
        "times_hit": 3,
        "transient": True,
    }
    out = scrub_value([value], found)[0]
    for key in ("password", "secret", "token", "userPassword", "api_key", "passphrase"):
        assert out[key] == "[redacted]", key
    assert out["description"] == "then check the log" and out["times_hit"] == 3
    assert out["transient"] is True and found


@pytest.mark.parametrize(
    "text",
    [
        "password reset flow",
        "store the password in a password manager",
        'sshpass -p "$SSHPASS" ssh host',
        "sshpass -p $(pass show edge) ssh host",
        "a password-protected archive",
        "reset password via /login",
        "find the password reference_lookup entry",
        "sshpass -p ${SSHPASS} ssh host",
        "log in with password $HOME_PW then sync",
        "the password manager costs $5",
    ],
)
def test_ordinary_text_and_variable_references_are_left_alone(text):
    assert scrub_text(text) == (text, [])


def _row(**over):
    base = dict(
        id="p1",
        task_type="t",
        principle="use the edge box",
        tools_used=["Bash"],
        context_tags=["x"],
        created_at="2026-11-17T00:00:00+00:00",
        steps=[f"sshpass -p '{FAKE}' ssh host", "then check the log"],
    )
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_create_stores_redacted_steps_as_valid_json(db):
    await procedural.create(db, **_row())
    row = await procedural.get_by_id(db, "p1")
    steps = row["steps"] if isinstance(row["steps"], list) else json.loads(row["steps"])
    assert FAKE not in json.dumps(steps) and steps[1] == "then check the log"


@pytest.mark.asyncio
async def test_update_redacts_a_json_string_field_without_breaking_it(db):
    await procedural.create(db, **_row(steps=["fine"]))
    await procedural.update(db, "p1", steps=json.dumps([f"login with password {FAKE}", "b"]))
    row = await procedural.get_by_id(db, "p1")
    steps = row["steps"] if isinstance(row["steps"], list) else json.loads(row["steps"])
    assert steps == ["login with password [redacted]", "b"]


@pytest.mark.asyncio
async def test_upsert_redacts_too(db):
    await procedural.upsert(db, **{k: v for k, v in _row().items() if k != "scenario"})
    row = await procedural.get_by_id(db, "p1")
    assert FAKE not in json.dumps(row, default=str)


@pytest.mark.asyncio
async def test_a_clean_procedure_is_stored_unchanged(db):
    await procedural.create(db, **_row(steps=["git status", "pytest tests/x.py"]))
    row = await procedural.get_by_id(db, "p1")
    steps = row["steps"] if isinstance(row["steps"], list) else json.loads(row["steps"])
    assert steps == ["git status", "pytest tests/x.py"] and row["principle"] == "use the edge box"


@pytest.mark.asyncio
async def test_a_scrub_failure_withholds_the_field_and_still_writes(db, monkeypatch):
    """Protecting a secret never drops a write: the field is withheld, the row lands."""
    from genesis.learning.procedural import credential_scrub

    def boom(text):
        raise credential_scrub.ScrubFailed("unavailable")

    monkeypatch.setattr(credential_scrub, "_shared_scrub", boom)
    await procedural.create(db, **_row(steps=["git status"]))
    row = await procedural.get_by_id(db, "p1")
    steps = row["steps"] if isinstance(row["steps"], list) else json.loads(row["steps"])
    assert steps == [procedural._WITHHELD] and row["principle"] == procedural._WITHHELD


@pytest.mark.asyncio
async def test_the_scrub_keeps_non_ascii_in_a_json_field_it_re_dumps(db):
    """update() with a JSON string is parsed, scrubbed and re-dumped; the re-dump must
    not rewrite the text (create()'s own json.dumps is unchanged by this PR)."""
    await procedural.create(db, **_row(steps=["fine"]))
    await procedural.update(db, "p1", steps=json.dumps(["vérifier le café"], ensure_ascii=False))
    raw = await (await db.execute("SELECT steps FROM procedural_memory WHERE id='p1'")).fetchone()
    assert "vérifier" in raw[0]


@pytest.mark.asyncio
async def test_a_json_field_with_surrounding_whitespace_is_still_parsed(db):
    """Whitespace before the document used to send it down the prose path."""
    await procedural.create(db, **_row(steps=["fine"]))
    doc = "  \n" + json.dumps([{"password": "x7!Secret", "note": "keep"}]) + "\n "
    await procedural.update(db, "p1", steps=doc)
    row = await procedural.get_by_id(db, "p1")
    assert json.loads(row["steps"]) == [{"password": "[redacted]", "note": "keep"}]


@pytest.mark.asyncio
async def test_a_repeated_credential_condition_counts_one_failure_mode(db):
    """The stored description is scrubbed; the incoming one is compared scrubbed too."""
    from genesis.learning.procedural.operations import record_failure

    await procedural.create(db, **_row(steps=["fine"]))
    condition = f"auth failed for sshpass -p '{FAKE}' ssh host"
    for _ in range(2):
        assert await record_failure(db, "p1", condition=condition)
    modes = json.loads((await procedural.get_by_id(db, "p1"))["failure_modes"])
    assert len(modes) == 1 and modes[0]["times_hit"] == 2
    assert FAKE not in json.dumps(modes)


@pytest.mark.asyncio
async def test_record_failure_survives_a_withheld_failure_modes_field(db, monkeypatch):
    from genesis.learning.procedural import credential_scrub
    from genesis.learning.procedural.operations import record_failure

    await procedural.create(db, **_row(steps=["fine"]))

    def boom(text):
        raise credential_scrub.ScrubFailed("unavailable")

    monkeypatch.setattr(credential_scrub, "_shared_scrub", boom)
    assert await record_failure(db, "p1", condition="timeout")
    monkeypatch.undo()
    assert await record_failure(db, "p1", condition="timeout")
    modes = json.loads((await procedural.get_by_id(db, "p1"))["failure_modes"])
    assert modes[-1]["description"] == "timeout"


# More quoted spans than the shared scrubber's budget (64): it withholds the text.
OVERSIZED = '"a" ' * 70


@pytest.mark.asyncio
async def test_one_oversized_step_is_withheld_alone(db):
    await procedural.create(db, **_row(steps=["git status", OVERSIZED, "pytest tests/x.py"]))
    row = await procedural.get_by_id(db, "p1")
    assert json.loads(row["steps"]) == ["git status", procedural._WITHHELD, "pytest tests/x.py"]


@pytest.mark.asyncio
async def test_one_oversized_failure_mode_is_withheld_alone(db):
    from genesis.learning.procedural.operations import record_failure

    await procedural.create(db, **_row(steps=["fine"]))
    for condition in ("timeout", "timeout", OVERSIZED):
        assert await record_failure(db, "p1", condition=condition)
    modes = json.loads((await procedural.get_by_id(db, "p1"))["failure_modes"])
    assert modes[0]["description"] == "timeout" and modes[0]["times_hit"] == 2
    assert modes[1]["description"] == procedural._WITHHELD and modes[1]["times_hit"] == 1
