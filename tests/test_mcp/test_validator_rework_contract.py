"""Owned regressions for the four-point validator writer rework."""

import json

import pytest
from fastmcp.exceptions import ToolError

from genesis.session_awareness import pr_recording
from genesis.session_awareness.pr_evidence import NOT_DEPLOYED, Decision, Derivation
from tests.test_mcp.test_validator_stdio_writers import client
from tests.test_mcp.test_validator_verification_writer import call, row
from tests.test_mcp.test_validator_verification_writer import recording as recording
from tests.test_mcp.validator_stdio_fixture import prepare
from tests.test_scripts.test_pr_verification_closer import MERGE_SHA, _doc, _prv


@pytest.mark.parametrize("note", [None, "", "  owner note  "])
@pytest.mark.parametrize("parked", [False, True])
@pytest.mark.parametrize("prefixed", [False, True])
def test_shared_note_has_no_missing_text_or_empty_separator(note, parked, prefixed):
    gaps = (NOT_DEPLOYED + ": fixture",) if prefixed else ()
    decision = Decision("cannot-verify", Derivation("cannot-verify", gaps, ()), parked)
    parts = (["PARKED (nothing material established)"] if parked else [])
    parts += list(gaps) + ([note.strip()] if note and note.strip() else [])
    assert pr_recording.attempt_note(decision, note) == " — ".join(parts)


@pytest.mark.parametrize("note", [None, "", "owner note"])
def test_closing_note_is_absent(note):
    decision = Decision("pass-mechanical", Derivation("pass-mechanical", (), ()), False)
    assert pr_recording.attempt_note(decision, note) is None


def test_cli_uses_the_shared_note_formatter():
    assert getattr(_prv, "attempt_note", None) is pr_recording.attempt_note


@pytest.fixture
def gate_files(tmp_path, monkeypatch):
    from genesis import _config_overlay
    from genesis.mcp.health import settings
    from genesis.session_awareness import codex_validator_config

    base = tmp_path / "base"
    user = tmp_path / "user"
    base.mkdir()
    user.mkdir()
    path = base / "codex_validator.yaml"
    path.write_text("dry_run_only: true\n")
    monkeypatch.setattr(settings, "_CONFIG_DIR", base)
    monkeypatch.setattr(settings, "_USER_CONFIG_DIR", user)
    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: user)
    monkeypatch.setattr(codex_validator_config, "_base_path", lambda: path)
    return settings, codex_validator_config, path, user / "codex_validator.local.yaml"


async def update(settings, *, dry_run, changes=None):
    try:
        return await settings._impl_settings_update(
            "codex_validator", changes or {"dry_run_only": False}, dry_run=dry_run
        )
    except Exception as exc:
        pytest.fail("Settings raised instead of an actionable refusal: " + type(exc).__name__)


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("failure", ["missing", "parse", "shape", "false", "unreadable"])
async def test_bad_base_refuses_settings_and_keeps_preview(gate_files, monkeypatch, dry_run, failure):
    settings, config, base, overlay = gate_files
    overlay.write_text("dry_run_only: false\n")
    before = overlay.read_bytes()
    if failure == "missing":
        base.unlink()
    elif failure == "unreadable":
        original = type(base).read_text

        def read(path, *args, **kwargs):
            if path == base:
                raise PermissionError("owned unreadable base")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(type(base), "read_text", read)
    else:
        base.write_text({"parse": "[", "shape": "[]", "false": "dry_run_only: false"}[failure])
    result = await update(settings, dry_run=dry_run)
    assert result.get("error") == "validation failed", result
    assert result.get("validation_errors") and "base" in str(result["validation_errors"]).lower()
    assert overlay.read_bytes() == before and config.dry_run_only() is True


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("failure", ["unknown", "parse", "shape", "directory", "dangling"])
async def test_bad_retained_overlay_is_not_discarded(gate_files, dry_run, failure):
    settings, config, _, overlay = gate_files
    if failure == "directory":
        overlay.mkdir()
    elif failure == "dangling":
        overlay.symlink_to(overlay.parent / "missing")
    else:
        overlay.write_text({"unknown": "dry_run_only: true\nunknown: false", "parse": "[", "shape": "[]"}[failure])
    before = overlay.read_bytes() if overlay.is_file() else None
    result = await update(settings, dry_run=dry_run)
    assert result.get("error") == "validation failed", result
    assert config.dry_run_only() is True
    if before is not None:
        assert overlay.read_bytes() == before
    else:
        assert overlay.is_dir() if failure == "directory" else overlay.is_symlink()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("value", [False, True])
async def test_valid_settings_predict_actual_gate(gate_files, dry_run, value):
    settings, config, _, overlay = gate_files
    result = await update(settings, dry_run=dry_run, changes={"dry_run_only": value})
    assert result["status"] == ("dry_run_ok" if dry_run else "applied")
    assert result.get("merged_preview", {"dry_run_only": value}) == {"dry_run_only": value}
    assert config.dry_run_only() is (True if dry_run else value)
    assert overlay.exists() is (not dry_run)


@pytest.mark.parametrize("dry_run", [False, True])
async def test_rejected_proposal_does_not_revoke_valid_opt_in(gate_files, dry_run):
    settings, config, _, overlay = gate_files
    overlay.write_text("dry_run_only: false\n")
    before = overlay.read_bytes()
    result = await update(settings, dry_run=dry_run, changes={"dry_run_only": "false"})
    assert result.get("error") == "validation failed"
    assert overlay.read_bytes() == before and config.dry_run_only() is False


@pytest.mark.parametrize("preview", [False, True])
@pytest.mark.parametrize("failure", ["mismatch", "error"])
async def test_identity_refusal_keeps_every_ledger_field(recording, monkeypatch, preview, failure):
    from genesis.mcp.health import validator_writes

    path, _, config = recording
    config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    before = row(path)

    def lookup(repo, pr):
        assert (repo, pr) == ("owner/repo", 7)
        if failure == "error":
            raise LookupError("owned lookup unavailable")
        return "f" * 40

    monkeypatch.setattr(validator_writes, "gh_merge_commit", lookup, raising=False)
    with pytest.raises(ToolError, match="merge_commit"):
        await call(_doc(), dry_run=preview)
    assert row(path) == before


async def test_default_lookup_alias_is_consumed(recording, monkeypatch):
    from genesis.mcp.health import validator_writes

    calls = []

    def lookup(repo, pr):
        calls.append((repo, pr))
        return MERGE_SHA

    monkeypatch.setattr(validator_writes, "gh_merge_commit", lookup, raising=False)
    result = await call(_doc())
    assert result["dry_run"] and not result["recorded"]
    assert calls == [("owner/repo", 7)]


@pytest.mark.parametrize("recording", [False, True])
async def test_stdio_lookup_is_in_the_child(tmp_path, recording):
    fixture = await prepare(tmp_path, recording=recording)
    async with client(fixture, "health") as mcp:
        tool = next(t for t in await mcp.list_tools() if t.name == "pr_verification_close")
        assert set(tool.inputSchema["properties"]) == {"evidence_json", "pr", "park", "note", "verdict", "dry_run"}
        result = (await mcp.call_tool("pr_verification_close", {"pr": 7, "evidence_json": json.dumps(_doc())})).data
        assert result["recorded"] is recording
    receipt = tmp_path / "merge-lookups.jsonl"
    assert receipt.is_file(), "No child-local merge lookup receipt"
    assert [json.loads(line) for line in receipt.read_text().splitlines()] == [["owner/repo", 7]]
    assert not (tmp_path / "unexpected-gh").exists()
