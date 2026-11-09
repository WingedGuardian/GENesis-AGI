"""File intent, independent URL expectations and request-context ownership."""

import asyncio
import copy
import json
from functools import partial
from pathlib import Path
from queue import Queue
from threading import Thread

import pytest

from genesis import caller_context
from genesis.knowledge.processors.registry import ContentProcessorRegistry
from genesis.mcp.shared_paths import (
    IDENTITY_META,
    SharedPathError,
    original_identity,
    prepare_params,
    probe_resume_file,
    request_paths,
)


def parameters(name, argument, value):
    return {"name": name, "arguments": {argument: value, "project_type": "fixture"},
            "_meta": {"fixture.nested": {"values": [1, "keep"]}}}


def test_url_intent_matches_the_original_registry_corpus(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = Path(__file__).parents[1] / "fixtures/mcp_shared_paths/url_routes.json"
    cases = json.loads(path.read_text())["cases"]
    assert len(cases) == 900
    for case in cases:
        value = case["source"]
        supplied = parameters("knowledge_ingest_source", "source", value)
        before = copy.deepcopy(supplied)
        prepared = prepare_params("memory", "tools/call", supplied)
        is_url = case["processor"] in {"WebProcessor", "YouTubeProcessor"}
        actual = value if is_url else str(Path(value).absolute())
        assert prepared["arguments"]["source"] == actual, case
        with request_paths("memory", prepared["name"], prepared["arguments"], prepared["_meta"], versioned=True):
            assert original_identity(prepared["name"], "source", actual) == (None if is_url else value), case
        assert supplied == before
        assert prepared["_meta"]["fixture.nested"] == before["_meta"]["fixture.nested"]


@pytest.mark.parametrize("service,name,argument,value,expected", [
    ("health", "evo_run", "golden_set_path", "golden.jsonl", "cwd/golden.jsonl"),
    ("health", "evo_run", "golden_set_path", "~/golden.jsonl", "cwd/~/golden.jsonl"),
    ("health", "evo_run", "golden_set_path", "", ""),
    ("health", "experiment_run", "golden_set_path", " ", "cwd/ "),
    ("health", "skill_replay_run", "suite_path", "suite.jsonl", "cwd/suite.jsonl"),
    ("health", "skill_replay_run", "suite_path", "", ""),
    ("health", "task_submit", "plan_path", " ~/plan.md ", "home/plan.md"),
    ("health", "task_submit", "plan_path", " \t ", " \t "),
    ("memory", "document_index", "path", "", "cwd"),
    ("memory", "document_index", "path", "~/doc.pdf", "home/doc.pdf"),
    ("memory", "document_query", "path_or_doc_id", "", "cwd"),
    ("memory", "document_query", "path_or_doc_id", "pi-fixture", "pi-fixture"),
    ("memory", "knowledge_ingest_source", "source", "", "cwd"),
    ("memory", "knowledge_ingest_batch", "directory", "", "cwd"),
    ("memory", "knowledge_ingest_batch", "directory", "~/docs", "cwd/~/docs"),
    ("memory", "resume_review", "resume_source", "resume.txt", "cwd/resume.txt"),
    ("memory", "resume_review", "resume_source", "literal resume text", "literal resume text"),
    ("memory", "resume_review", "resume_source", "", "cwd"),
])
def test_field_specific_file_meaning(tmp_path, monkeypatch, service, name, argument, value, expected):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "resume.txt").write_text("fixture")
    if expected == "cwd" or expected.startswith("cwd/"):
        expected = str(tmp_path / expected.removeprefix("cwd").lstrip("/"))
    elif expected.startswith("home/"):
        expected = str(tmp_path / "home" / expected.removeprefix("home/"))
    prepared = prepare_params(service, "tools/call", parameters(name, argument, value))
    actual = prepared["arguments"][argument]
    assert actual == expected
    with request_paths(service, name, prepared["arguments"], prepared["_meta"], versioned=True):
        if name == "resume_review":
            assert probe_resume_file(actual) == Path(actual).is_absolute()
        elif name in {"knowledge_ingest_source", "knowledge_ingest_batch"}:
            assert original_identity(name, argument, actual) == value


def test_falsey_registered_url_processor_keeps_precedence():
    class Processor:
        def __bool__(self):
            return False

        def can_handle(self, value):
            return True

        async def process(self, value, **kwargs):
            pytest.fail("URL routing executed its processor")

    registry, processor = ContentProcessorRegistry(), Processor()
    registry.register_url_pattern(processor, "fixture")
    assert registry.get_url_processor("fixture") is processor
    assert registry.get_processor("fixture") is processor


def test_old_endpoint_rejects_ambiguous_files_but_keeps_explicit_inputs():
    for name, argument, value in [
        ("document_index", "path", "relative.pdf"),
        ("knowledge_ingest_source", "source", "relative.txt"),
        ("knowledge_ingest_batch", "directory", ""),
        ("resume_review", "resume_source", "relative or text"),
    ]:
        with pytest.raises(SharedPathError, match="absolute path or updated forwarder"), \
                request_paths("memory", name, {argument: value}, None, versioned=False):
            pytest.fail("relative input reached the body")
    for name, argument, value in [
        ("document_index", "path", "/fixture/doc.pdf"),
        ("document_query", "path_or_doc_id", "pi-fixture"),
        ("knowledge_ingest_source", "source", "https://example.invalid/doc.pdf"),
    ]:
        with request_paths("memory", name, {argument: value}, None, versioned=False):
            pass  # These inputs need no identity hint or caller-cwd guess.


@pytest.mark.parametrize("hint", [
    None, {}, {"argument": "source", "original": "note.txt"},
    {"argument": "source", "original": "note.txt", "actual": "/other.txt"},
    {"argument": "source", "original": None, "actual": "/fixture.txt"},
    {"argument": "directory", "original": "note.txt", "actual": "/fixture.txt"},
    {"argument": "source", "original": "https://example.invalid/", "actual": "/fixture.txt"},
    {"argument": "source", "original": "note.txt", "actual": "/fixture.txt", "extra": True},
])
def test_supplied_bad_identity_refuses_before_the_body(hint):
    with pytest.raises(SharedPathError), \
            request_paths("memory", "knowledge_ingest_source", {"source": "/fixture.txt"},
                          {IDENTITY_META: hint}, versioned=True):
        pytest.fail("bad identity reached the body")


def metadata(original="note.txt", actual="/fixture.txt"):
    return {IDENTITY_META: {"argument": "source", "original": original, "actual": actual}}


def test_unhinted_absolute_input_is_valid_and_native_hints_are_reserved():
    with request_paths("memory", "knowledge_ingest_source", {"source": "/fixture.txt"}, None, versioned=True):
        assert original_identity("knowledge_ingest_source", "source", "/fixture.txt") is None
    supplied = parameters("knowledge_ingest_source", "source", "note.txt")
    supplied["_meta"].update(metadata())
    before = copy.deepcopy(supplied)
    with pytest.raises(SharedPathError, match="reserved"):
        prepare_params("memory", "tools/call", supplied)
    assert supplied == before


def test_missing_request_metadata_is_distinct_from_an_absent_optional_hint():
    with pytest.raises(SharedPathError, match="metadata is unavailable"), \
            request_paths("memory", "knowledge_ingest_source", {"source": "/fixture.txt"}, None,
                          versioned=True, metadata_available=False):
        pytest.fail("unknown request metadata reached an identity-sensitive body")
    with request_paths("health", "health_status", {}, None, versioned=True, metadata_available=False):
        pass  # Unrelated tools do not acquire a new file-context requirement.


def test_nested_call_cannot_retarget_the_original_identity():
    with request_paths("memory", "knowledge_ingest_source", {"source": "/fixture.txt"},
                       metadata(), versioned=True):
        with pytest.raises(SharedPathError, match="Invalid caller file identity"), \
                request_paths("memory", "knowledge_ingest_source", {"source": "/other.txt"},
                              metadata(), versioned=True):
            pytest.fail("outer hint followed a changed nested argument")
        assert original_identity("knowledge_ingest_source", "source", "/fixture.txt") == "note.txt"
        with pytest.raises(SharedPathError, match="does not match"):
            original_identity("knowledge_ingest_source", "source", "/changed-after-binding.txt")


async def test_context_ownership_and_lost_scope(monkeypatch):
    monkeypatch.setattr(caller_context, "_shared_service", True)
    lookup = partial(original_identity, "knowledge_ingest_source", "source", "/fixture.txt")
    with request_paths("memory", "knowledge_ingest_source", {"source": "/fixture.txt"},
                       metadata(), versioned=True):
        assert await asyncio.to_thread(lookup) == "note.txt"
        with pytest.raises(SharedPathError, match="context is unavailable"):
            await asyncio.get_running_loop().run_in_executor(None, lookup)
        result = Queue()

        def bare():
            try:
                lookup()
            except SharedPathError:
                result.put("refused")

        thread = Thread(target=bare)
        thread.start()
        thread.join()
        assert result.get_nowait() == "refused"
    with pytest.raises(SharedPathError, match="context is unavailable"):
        lookup()
    monkeypatch.setattr(caller_context, "_shared_service", False)
    assert lookup() is None  # Ordinary stdio retains its old default.


async def test_concurrent_error_and_cancel_reset(monkeypatch):
    monkeypatch.setattr(caller_context, "_shared_service", True)

    async def call(label):
        actual = "/fixture-" + label
        with request_paths("memory", "knowledge_ingest_source", {"source": actual},
                           metadata(label, actual), versioned=True):
            await asyncio.sleep(0)
            return original_identity("knowledge_ingest_source", "source", actual)

    assert await asyncio.gather(call("A"), call("B")) == ["A", "B"]
    for error in [RuntimeError("fixture"), asyncio.CancelledError()]:
        with pytest.raises(type(error)), \
                request_paths("memory", "knowledge_ingest_source", {"source": "/fixture.txt"},
                              metadata(), versioned=True):
            raise error
        with pytest.raises(SharedPathError, match="context is unavailable"):
            original_identity("knowledge_ingest_source", "source", "/fixture.txt")
