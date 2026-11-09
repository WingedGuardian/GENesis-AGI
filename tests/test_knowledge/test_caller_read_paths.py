"""Read-location changes preserve ordinary manifest, cache and tree identities."""

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from genesis.knowledge.manifest import ManifestManager
from genesis.knowledge.orchestrator import KnowledgeOrchestrator
from genesis.knowledge.processors.base import ProcessedContent
from genesis.knowledge.processors.registry import ContentProcessorRegistry
from genesis.knowledge.processors.text import TextProcessor


def orchestrator(tmp_path, processor=None, *, extension=".txt", tree=None):
    registry = ContentProcessorRegistry()
    registry.register_extensions(processor or TextProcessor(), [extension, ".md"])
    distill = AsyncMock(return_value=[])
    pipeline = type("SyntheticPipeline", (), {"distill": distill, "last_extraction_ratio": 1})()
    manifest = ManifestManager(root=tmp_path / "manifest")
    return KnowledgeOrchestrator(registry=registry, manifest=manifest, distillation=pipeline,
                                 tree_index_client=tree), manifest, distill


@pytest.mark.parametrize("explicit", [False, True], ids=["ordinary", "split-read"])
@pytest.mark.parametrize("state", ["fresh", "unchanged", "changed", "legacy", "unreachable"])
async def test_original_cache_and_copied_bytes_follow_read_location(tmp_path, monkeypatch, explicit, state):
    caller, server = tmp_path / "caller", tmp_path / "server"
    caller.mkdir()
    server.mkdir()
    actual = caller / "note.txt"
    actual.write_text("CALLER current")
    (server / "note.txt").write_text("SERVER collision")
    orch, manifest, distill = orchestrator(tmp_path)
    if state != "fresh":
        previous = "CALLER current" if state == "unchanged" else "previous content"
        extracted = manifest.save_extracted_text("note.txt", previous, "text")
        manifest.add_source("note.txt", source_type="text", extracted_path=extracted,
                            unit_ids=["prior-unit"], content_hash=None if state == "legacy" else
                            hashlib.sha256(previous.encode()).hexdigest()[:32])
    if state == "unreachable":
        actual.unlink()  # The owned fixture only; leave server lookalike in place.
    monkeypatch.chdir(server if explicit else caller)
    result = await orch.ingest_source("note.txt", project_type="synthetic",
                                      **({"read_path": str(actual)} if explicit else {}))
    assert result.source == "note.txt"
    assert [row["source"] for row in manifest.list_sources()] == ["note.txt"]
    if state in {"unchanged", "unreachable"}:
        assert result.unit_ids == ["prior-unit"] and result.source_type == "cached"
        assert manifest.get_units_for_source("note.txt") == ["prior-unit"]
        distill.assert_not_awaited()
        if state == "unreachable":
            assert result.quality_flags == ["duplicate_source", "source_unreachable_served_cache"]
    else:
        content = distill.await_args.args[0]
        assert content.text == "CALLER current" and content.source_path == "note.txt"
        saved = manifest.list_sources()[0]
        assert Path(saved["extracted_path"]).read_text() == "CALLER current"
        assert Path(saved["original_path"]).read_text() == "CALLER current"


@pytest.mark.parametrize("explicit", [False, True], ids=["ordinary", "split-read"])
@pytest.mark.parametrize("spelling", ["", ".", "folder", "link"])
async def test_batch_spelling_order_extensions_and_symlink_policy(tmp_path, monkeypatch, explicit, spelling):
    caller, server = tmp_path / "caller", tmp_path / "server"
    caller.mkdir()
    server.mkdir()
    actual = caller if spelling in {"", "."} else caller / "folder"
    actual.mkdir(exist_ok=True)
    if spelling == "link":
        (caller / "link").symlink_to(actual, target_is_directory=True)
        actual = caller / "link"
    names = ["a.txt", "b.TXT", "nested/c.md"]
    for name in names + ["ignore.xyz"]:
        file = actual / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("CALLER " + name)
    (actual / "file-link.txt").symlink_to(actual / "a.txt")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "not-traversed.txt").write_text("OUTSIDE")
    (actual / "directory-link").symlink_to(outside, target_is_directory=True)
    orch, manifest, distill = orchestrator(tmp_path)
    monkeypatch.chdir(server if explicit else caller)
    results = await orch.ingest_batch(spelling, project_type="synthetic",
                                      **({"read_directory": str(actual)} if explicit else {}))
    identities = [str(Path(spelling) / name) for name in names]
    assert [row.source for row in results] == identities
    assert {row["source"] for row in manifest.list_sources()} == set(identities)
    assert [(call.args[0].source_path, call.args[0].text) for call in distill.await_args_list] == [
        (identity, "CALLER " + name) for identity, name in zip(identities, names, strict=True)]


@pytest.mark.parametrize("explicit", [False, True], ids=["ordinary", "split-read"])
async def test_omitted_keyword_retains_custom_processor_source(tmp_path, explicit):
    class Processor:
        async def process(self, source):
            return ProcessedContent("synthetic text", source_path="processor-owned-origin", source_type="text")

        def can_handle(self, source):
            return True

    orch, _, distill = orchestrator(tmp_path, Processor())
    await orch.ingest_source("identity.txt", project_type="synthetic",
                             **({"read_path": str(tmp_path / "read.txt")} if explicit else {}))
    assert distill.await_args.args[0].source_path == ("identity.txt" if explicit else "processor-owned-origin")


@pytest.mark.parametrize("location", ["inside", "outside", "escaping-link"])
@pytest.mark.parametrize("explicit", [False, True], ids=["ordinary", "split-read"])
async def test_tree_keys_and_home_guard_use_the_correct_values(tmp_path, monkeypatch, explicit, location):
    from genesis.knowledge import tree_index

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(tree_index, "_INDICES_DIR", tmp_path / "indices")
    actual = (home if location == "inside" else tmp_path) / "read.pdf"
    actual.write_text("CALLER PDF fixture")
    if location == "escaping-link":
        (home / "read.pdf").symlink_to(actual)
        actual = home / "read.pdf"
    caller = actual.parent
    server = tmp_path / "server"
    server.mkdir()
    (server / "read.pdf").write_text("SERVER PDF collision")

    class PDF:
        async def process(self, source):
            return ProcessedContent(Path(source).read_text(), source_type="pdf", source_path=source,
                                    metadata={"page_count": 30})

        def can_handle(self, source):
            return True

    tree = type("SyntheticPageIndex", (), {
        "upload_document": AsyncMock(return_value="pi-synthetic"),
        "get_tree": AsyncMock(return_value={"result": []}),
    })()
    orch, manifest, _ = orchestrator(tmp_path, PDF(), extension=".pdf", tree=tree)
    monkeypatch.chdir(server if explicit else caller)
    result = await orch.ingest_source("read.pdf", project_type="synthetic",
                                      **({"read_path": str(actual)} if explicit else {}))
    if location == "inside":
        assert result.tree_index_doc_id == "pi-synthetic"
        tree.upload_document.assert_awaited_once_with(str(actual) if explicit else "read.pdf")
        assert tree_index.load_tree_index("read.pdf")["doc_id"] == "pi-synthetic"
        assert manifest.get_tree_index_doc_id("read.pdf") == "pi-synthetic"
        if explicit:
            assert tree_index.load_tree_index(str(actual)) is None
    else:
        assert result.tree_index_doc_id is None
        tree.upload_document.assert_not_awaited()


@pytest.mark.parametrize("link", [False, True], ids=["file", "symlink"])
async def test_actual_pdf_extraction_keeps_original_identity_and_settles_pool(tmp_path, monkeypatch, link):
    import pymupdf

    from genesis.knowledge import pdf_extract
    from genesis.knowledge.processors.pdf import PDFProcessor

    caller, server = tmp_path / "caller", tmp_path / "server"
    caller.mkdir()
    server.mkdir()
    for folder, text in [(caller, "CALLER PDF content"), (server, "SERVER PDF collision")]:
        with pymupdf.open() as document:
            document.new_page().insert_text((72, 72), text)
            document.save(folder / "read.pdf")
    if link:
        (caller / "link.pdf").symlink_to(caller / "read.pdf")
        (server / "link.pdf").symlink_to(server / "read.pdf")
    identity = "link.pdf" if link else "read.pdf"
    orch, manifest, distill = orchestrator(tmp_path, PDFProcessor(), extension=".pdf")
    monkeypatch.chdir(server)
    try:
        result = await orch.ingest_source(identity, project_type="synthetic", read_path=str(caller / identity))
        assert result.error is None and result.source == identity
        content = distill.await_args.args[0]
        assert content.source_path == identity and "CALLER PDF content" in content.text
        assert "SERVER" not in content.text
        assert [row["source"] for row in manifest.list_sources()] == [identity]
        assert "CALLER PDF content" in Path(manifest.list_sources()[0]["extracted_path"]).read_text()
    finally:
        await pdf_extract.shutdown_pdf_pool()
    assert pdf_extract._pool is None
