"""Backend constraints must precede durable frozen publication authority."""

import copy
import time

import pytest

from tests.test_scripts.test_transcript_publication import publication as publication


def _engine(publication, tmp_path, *, backend="smb", spool=None, scratch=None):
    module, original, _, binding, backup = publication
    engine = module.Publication(spool or tmp_path / "new-spool", scratch or original.scratch,
                                b"testpass", time.monotonic() + 7200)
    return engine, dict(binding, backend=backend), backup


@pytest.mark.parametrize("character", ['"', ";", "\\"])
@pytest.mark.parametrize("directory,pattern", [
    ("memory", "name{}.gpg"), ("eval", "nested{}/name.gpg"),
    ("creds", "nested/name{}.gpg"), ("config_overrides", "name{}.local.yaml"),
    ("data/qdrant", "collection{}.snapshot.gpg"),
])
def test_required_name_rejects_before_pending_and_corrected_retry_succeeds(
    publication, tmp_path, character, directory, pattern,
):
    engine, binding, backup = _engine(publication, tmp_path)
    path = backup / directory / pattern.format(character)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic encrypted input")
    qdrant = path.name.removesuffix(".snapshot.gpg") if directory == "data/qdrant" else ""
    snapshot = binding["host"] + "/20261018T000000Z"
    with pytest.raises(ValueError, match="refusing smb path"):
        engine.create(binding, snapshot, backup, qdrant, [])
    assert not engine.pending.exists()
    assert not list(engine.spool.glob(".freeze-*"))
    path.unlink()
    corrected = backup / directory / pattern.format("safe")
    corrected.parent.mkdir(parents=True, exist_ok=True)
    corrected.write_bytes(b"corrected synthetic input")
    qdrant = corrected.name.removesuffix(".snapshot.gpg") if directory == "data/qdrant" else ""
    doc = engine.create(binding, snapshot, backup, qdrant, [])
    suffix = "qdrant/" + corrected.name if qdrant else corrected.relative_to(backup).as_posix()
    assert any(row["remote"].endswith(suffix) for row in doc["rows"])
    assert all(character not in row["remote"] for row in doc["rows"])
    assert engine.open(binding, backup / "transcripts")["snapshot"] == snapshot


@pytest.mark.parametrize("character", ['"', ";", "\\", "\r", "\n"])
@pytest.mark.parametrize("root", ["spool", "scratch"])
def test_smb_transfer_roots_reject_before_initialization(publication, tmp_path, character, root):
    path = tmp_path / (root + character + "root")
    path.mkdir()
    engine, binding, backup = _engine(publication, tmp_path, **{root: path})
    with pytest.raises(ValueError, match="refusing smb path"):
        engine.create(binding, binding["host"] + "/20261018T000000Z", backup, "", [])
    assert not engine.pending.exists()
    assert not list(engine.spool.glob(".freeze-*"))


@pytest.mark.parametrize("character", ['"', ";", "\\"])
def test_new_unsupported_extra_is_a_gap_without_an_unusable_row(publication, tmp_path, character):
    engine, binding, backup = _engine(publication, tmp_path)
    name = "extra" + character + ".tar.gpg"
    (backup / "extra").mkdir()
    (backup / "extra" / name).write_bytes(b"synthetic extra")
    doc = engine.create(binding, binding["host"] + "/20261018T000000Z", backup, "", [name])
    assert doc["gaps"] == ["skipped " + name + " (freeze failed)"]
    assert not any(row["role"] == "extra" for row in doc["rows"])
    assert engine.open(binding, backup / "transcripts")["gaps"] == doc["gaps"]


@pytest.mark.parametrize("failed", [False, True])
def test_preexisting_optional_smb_state_preserves_terminal_gap_only(publication, tmp_path, failed):
    # Construct the exact old authenticated shape through the still-valid local
    # producer, then select SMB as the old producer did; no legacy code vendored.
    engine, binding, backup = _engine(publication, tmp_path, backend="local")
    name = "extra;legacy.tar.gpg"
    (backup / "extra").mkdir()
    (backup / "extra" / name).write_bytes(b"legacy synthetic extra")
    doc = engine.create(binding, binding["host"] + "/20261018T000000Z", backup, "", [name])
    binding["backend"] = "smb"
    doc["binding"] = copy.deepcopy(binding)
    extra = next(row for row in doc["rows"] if row["role"] == "extra")
    extra["failed"] = failed
    engine.checkpoint(doc)
    journal = (engine.pending / "journal.gpg").read_bytes()
    if failed:
        reopened = engine.open(binding, backup / "transcripts")
        assert (engine.pending / "journal.gpg").read_bytes() == journal
        reopened["claimed"] = True
        for row in reopened["rows"]:
            if row["required"]:
                if row["frozen"] is None:
                    engine.freeze_row(row)
                engine.download(row).write_bytes(engine.frozen(row).read_bytes())
                engine.compare(reopened, row, True)
        assert engine.next(reopened)[0] == "marker"
        assert b"skipped extra;legacy.tar.gpg (upload failed)" in engine.marker(reopened)
    else:
        with pytest.raises(ValueError, match="refusing smb path"):
            engine.open(binding, backup / "transcripts")
        assert (engine.pending / "journal.gpg").read_bytes() == journal
    assert not list(engine.spool.glob("retired-*"))


@pytest.mark.parametrize("character", ['"', ";", "\\"])
def test_preexisting_required_smb_state_is_retained_after_source_removal(publication, tmp_path, character):
    engine, binding, backup = _engine(publication, tmp_path, backend="local")
    (backup / "memory").mkdir()
    source = backup / "memory" / ("old" + character + ".gpg")
    source.write_bytes(b"original synthetic ciphertext")
    doc = engine.create(binding, binding["host"] + "/20261018T000000Z", backup, "", [])
    binding["backend"] = "smb"
    doc["binding"] = copy.deepcopy(binding)
    engine.checkpoint(doc)
    journal = (engine.pending / "journal.gpg").read_bytes()
    row = next(row for row in doc["rows"] if row["remote"].endswith(source.name))
    frozen = engine.frozen(row).read_bytes()
    source.unlink()
    with pytest.raises(ValueError, match="refusing smb path"):
        engine.open(binding, backup / "transcripts")
    assert (engine.pending / "journal.gpg").read_bytes() == journal
    assert engine.frozen(row).read_bytes() == frozen
    assert not list(engine.spool.glob("retired-*"))


@pytest.mark.parametrize("character", ['"', ";", "\\"])
def test_local_keeps_names_and_transfer_roots_supported_by_its_backend(publication, tmp_path, character):
    scratch = tmp_path / ("scratch" + character)
    scratch.mkdir()
    engine, binding, backup = _engine(publication, tmp_path, backend="local",
                                      spool=tmp_path / ("spool" + character), scratch=scratch)
    (backup / "memory").mkdir()
    name = "local" + character + ".gpg"
    (backup / "memory" / name).write_bytes(b"synthetic local ciphertext")
    doc = engine.create(binding, binding["host"] + "/20261018T000000Z", backup, "", [])
    assert any(row["remote"].endswith(name) for row in doc["rows"])
    assert engine.open(binding, backup / "transcripts")["snapshot"] == doc["snapshot"]


def test_smb_unc_binding_is_an_argv_argument_not_a_command_path(publication, tmp_path):
    engine, binding, backup = _engine(publication, tmp_path)
    binding["root"] = "//synthetic-server/share;literal"
    doc = engine.create(binding, binding["host"] + "/20261018T000000Z", backup, "", [])
    assert engine.open(binding, backup / "transcripts")["binding"] == doc["binding"]
