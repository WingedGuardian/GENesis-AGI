"""Procedure writes never store a credential in their text (#4859). All values here
are synthetic."""

from __future__ import annotations

import json

import pytest

from genesis.db.crud import procedural
from genesis.learning.procedural.credential_scrub import scrub_text

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


def test_a_variable_reference_keeps_the_command_whole():
    """The shared redactor used to cut such a line to its end (review finding)."""
    cleaned, _ = scrub_text("export GITHUB_TOKEN=$GH_TOKEN && gh pr list && echo done")
    assert cleaned.endswith("&& gh pr list && echo done")


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
