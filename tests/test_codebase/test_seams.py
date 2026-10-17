"""Tests for the seams view: Protocol/ABC seams and the classes that implement them."""

from __future__ import annotations

import re
from pathlib import Path

import aiosqlite
import pytest

from genesis.codebase.indexer import index_codebase
from genesis.codebase.seams import REGISTRIES, find_seams, load_seams

REPO_ROOT = Path(__file__).resolve().parents[2]


def _cls(module: str, name: str, signature: str | None, doc: str | None = None) -> dict:
    return {"module_path": module, "name": name, "signature": signature, "docstring": doc}


def _meth(module: str, parent: str, name: str, signature: str) -> dict:
    return {"module_path": module, "parent_class": parent, "name": name, "signature": signature}


def _by_seam(seams: list[dict]) -> dict[str, dict]:
    return {s["seam"]: s for s in seams}


def _names(seam: dict) -> list[tuple[str, str, str]]:
    return [(i["name"], i["match"], i["confidence"]) for i in seam["implementers"]]


# ─── find_seams: pure, over index rows ───────────────────────────────────────


def _provider_rows() -> tuple[list[dict], list[dict]]:
    """A two-method Protocol and classes that do and do not fit it."""
    classes = [
        _cls("p/proto.py", "Provider", "class Provider(Protocol)", "Something to call.\nMore."),
        _cls("p/a.py", "Nominal", "class Nominal(Provider)"),
        _cls("p/b.py", "Structural", "class Structural"),
        _cls("p/c.py", "MissingOne", "class MissingOne"),
        _cls("p/d.py", "WrongArity", "class WrongArity"),
        _cls("p/d2.py", "TooFew", "class TooFew"),
        _cls("p/e.py", "SyncInstead", "class SyncInstead"),
        _cls("p/f.py", "Varargs", "class Varargs"),
        _cls("p/g.py", "Defaults", "class Defaults"),
        _cls("p/h.py", "_Private", "class _Private"),
    ]
    methods = [
        _meth("p/proto.py", "Provider", "invoke", "async def invoke(self, request: dict) -> dict"),
        _meth("p/proto.py", "Provider", "check_health", "async def check_health(self) -> str"),
        _meth("p/b.py", "Structural", "invoke", "async def invoke(self, request)"),
        _meth("p/b.py", "Structural", "check_health", "async def check_health(self)"),
        _meth("p/c.py", "MissingOne", "invoke", "async def invoke(self, request)"),
        _meth("p/d.py", "WrongArity", "invoke", "async def invoke(self, a, b)"),
        _meth("p/d.py", "WrongArity", "check_health", "async def check_health(self)"),
        _meth("p/d2.py", "TooFew", "invoke", "async def invoke(self)"),
        _meth("p/d2.py", "TooFew", "check_health", "async def check_health(self)"),
        _meth("p/e.py", "SyncInstead", "invoke", "def invoke(self, request)"),
        _meth("p/e.py", "SyncInstead", "check_health", "def check_health(self)"),
        _meth("p/f.py", "Varargs", "invoke", "async def invoke(self, *args)"),
        _meth("p/f.py", "Varargs", "check_health", "async def check_health(self)"),
        _meth("p/g.py", "Defaults", "invoke", "async def invoke(self, request, extra=1)"),
        _meth("p/g.py", "Defaults", "check_health", "async def check_health(self, deep=False)"),
        _meth("p/h.py", "_Private", "invoke", "async def invoke(self, request)"),
        _meth("p/h.py", "_Private", "check_health", "async def check_health(self)"),
    ]
    return classes, methods


def test_find_seams_matches_nominal_and_call_compatible_structural() -> None:
    classes, methods = _provider_rows()
    seams, unparsed = find_seams(classes, methods)

    assert unparsed == 0
    seam = _by_seam(seams)["Provider"]
    assert seam["module"] == "p/proto.py"
    assert seam["methods"] == ["check_health", "invoke"]
    assert seam["use_when"] == "Something to call."
    # Nominal first; structural by module. Missing method, too many or too few
    # parameters, async mismatch and the private class are all left out.
    assert _names(seam) == [
        ("Nominal", "nominal", "high"),
        ("Structural", "structural", "high"),
        ("Varargs", "structural", "high"),
        ("Defaults", "structural", "high"),
    ]


def test_find_seams_recognises_every_seam_spelling() -> None:
    classes = [
        _cls("s.py", "Plain", "class Plain(Protocol)"),
        _cls("s.py", "Dotted", "class Dotted(typing.Protocol)"),
        _cls("s.py", "Generic", "class Generic(Protocol[T])"),
        _cls("s.py", "Abstract", "class Abstract(ABC)"),
        _cls("s.py", "AbcModule", "class AbcModule(abc.ABC)"),
        _cls("s.py", "Meta", "class Meta(metaclass=ABCMeta)"),
        _cls("s.py", "NotASeam", "class NotASeam(Base)"),
        _cls("s.py", "_Hidden", "class _Hidden(Protocol)"),
    ]
    seams, _ = find_seams(classes, [])
    assert sorted(_by_seam(seams)) == [
        "AbcModule",
        "Abstract",
        "Dotted",
        "Generic",
        "Meta",
        "Plain",
    ]


def test_find_seams_methodless_seam_takes_nominal_implementers_only() -> None:
    classes = [
        _cls("m.py", "Marker", "class Marker(Protocol)"),
        _cls("m.py", "Tagged", "class Tagged(Marker)"),
        _cls("m.py", "Unrelated", "class Unrelated"),
    ]
    seams, _ = find_seams(classes, [])
    assert _names(_by_seam(seams)["Marker"]) == [("Tagged", "nominal", "high")]


def test_find_seams_property_with_setter_still_matches() -> None:
    """A getter and a setter share a name; one compatible definition is enough."""
    classes = [
        _cls("m.py", "Module", "class Module(Protocol)"),
        _cls("x.py", "Impl", "class Impl"),
    ]
    methods = [
        _meth("m.py", "Module", "enabled", "def enabled(self) -> bool"),
        _meth("m.py", "Module", "run", "async def run(self) -> None"),
        _meth("x.py", "Impl", "enabled", "def enabled(self) -> bool"),
        _meth("x.py", "Impl", "enabled", "def enabled(self, value: bool) -> None"),
        _meth("x.py", "Impl", "run", "async def run(self) -> None"),
    ]
    seams, _ = find_seams(classes, methods)
    assert _names(_by_seam(seams)["Module"]) == [("Impl", "structural", "high")]


def test_find_seams_single_method_structural_links_are_low_confidence() -> None:
    classes = [
        _cls("c.py", "Collector", "class Collector(Protocol)"),
        _cls("d.py", "Real", "class Real"),
    ]
    methods = [
        _meth("c.py", "Collector", "collect", "async def collect(self)"),
        _meth("d.py", "Real", "collect", "async def collect(self)"),
    ]
    seams, _ = find_seams(classes, methods)
    assert _names(_by_seam(seams)["Collector"]) == [("Real", "structural", "low")]


def test_find_seams_same_named_seams_and_implementers_keep_their_modules() -> None:
    classes = [
        _cls("one/base.py", "Store", "class Store(Protocol)"),
        _cls("two/base.py", "Store", "class Store(ABC)"),
        _cls("one/impl.py", "Disk", "class Disk(Store)"),
        _cls("one/twin.py", "Twin", "class Twin(Store)"),
        _cls("two/twin.py", "Twin", "class Twin(Store)"),
    ]
    seams, _ = find_seams(classes, [])
    stores = [s for s in seams if s["seam"] == "Store"]
    assert [s["module"] for s in stores] == ["one/base.py", "two/base.py"]
    for store in stores:
        # A name defined twice cannot say which base a subclass means.
        assert {(i["name"], i["module"], i["confidence"]) for i in store["implementers"]} == {
            ("Disk", "one/impl.py", "low"),
            ("Twin", "one/twin.py", "low"),
            ("Twin", "two/twin.py", "low"),
        }


def test_find_seams_caps_implementers_and_says_how_many_were_left_out() -> None:
    classes = [_cls("b.py", "Base", "class Base(ABC)")] + [
        _cls(f"i{n}.py", f"Impl{n}", f"class Impl{n}(Base)") for n in range(3)
    ]
    seams, _ = find_seams(classes, [], cap=2)
    seam = _by_seam(seams)["Base"]
    assert len(seam["implementers"]) == 2
    assert seam["implementers_omitted"] == 1

    one, _ = find_seams(classes, [], name="Base", cap=2)
    assert len(one[0]["implementers"]) == 3
    assert "implementers_omitted" not in one[0]


def test_find_seams_name_selects_one_seam() -> None:
    classes, methods = _provider_rows()
    classes.append(_cls("o.py", "Other", "class Other(ABC)"))
    seams, _ = find_seams(classes, methods, name="Other")
    assert [s["seam"] for s in seams] == ["Other"]


def test_find_seams_skips_and_counts_unparseable_rows() -> None:
    """One malformed stored signature must not fail the whole view."""
    classes, methods = _provider_rows()
    classes.append(_cls("z.py", "Broken", "class Broken(("))
    methods.append(_meth("p/b.py", "Structural", "extra", "def extra(self, ("))
    seams, unparsed = find_seams(classes, methods)
    assert unparsed == 2
    assert ("Structural", "structural", "high") in _names(_by_seam(seams)["Provider"])


def test_find_seams_ignores_classes_not_yet_reindexed() -> None:
    """A NULL class signature is pending, not a seam and not a parse failure."""
    seams, unparsed = find_seams([_cls("n.py", "Pending", None)], [])
    assert seams == []
    assert unparsed == 0


def test_find_seams_sub_protocols_extend_rather_than_implement() -> None:
    """A Protocol subclassing a seam is 'extends'; a Protocol never matches structurally."""
    classes = [
        _cls("t.py", "Tool", "class Tool(Protocol)"),
        _cls("t.py", "SearchTool", "class SearchTool(Tool, Protocol)"),
        _cls("t.py", "LookalikeProto", "class LookalikeProto(Protocol)"),
        _cls("t.py", "Adapter", "class Adapter"),
    ]
    methods = [
        _meth("t.py", "Tool", "invoke", "async def invoke(self, req)"),
        _meth("t.py", "Tool", "check", "async def check(self)"),
        _meth("t.py", "LookalikeProto", "invoke", "async def invoke(self, req)"),
        _meth("t.py", "LookalikeProto", "check", "async def check(self)"),
        _meth("t.py", "Adapter", "invoke", "async def invoke(self, req)"),
        _meth("t.py", "Adapter", "check", "async def check(self)"),
    ]
    seams, _ = find_seams(classes, methods)
    assert _names(_by_seam(seams)["Tool"]) == [
        ("SearchTool", "extends", "high"),
        ("Adapter", "structural", "high"),
    ]


def test_find_seams_base_name_shared_with_an_ordinary_class_is_ambiguous() -> None:
    """One seam named Store plus an unrelated class named Store: a subclass could mean either."""
    classes = [
        _cls("a/base.py", "Store", "class Store(Protocol)"),
        _cls("b/store.py", "Store", "class Store"),
        _cls("b/disk.py", "Disk", "class Disk(Store)"),
    ]
    seams, _ = find_seams(classes, [])
    assert _names(_by_seam(seams)["Store"]) == [("Disk", "nominal", "low")]


def test_find_seams_lists_classes_missing_only_attribute_like_members() -> None:
    """A property supplied as a class attribute leaves no method row: say so, do not hide it."""
    classes = [
        _cls("h/types.py", "Host", "class Host(Protocol)"),
        _cls("h/standalone.py", "Standalone", "class Standalone"),
        _cls("h/unrelated.py", "OnlyName", "class OnlyName"),
    ]
    methods = [
        _meth("h/types.py", "Host", "name", "def name(self) -> str"),
        _meth("h/types.py", "Host", "serve", "async def serve(self, port: int)"),
        _meth("h/standalone.py", "Standalone", "serve", "async def serve(self, port)"),
        # Matches only an attribute-like member: no real evidence, never listed.
        _meth("h/unrelated.py", "OnlyName", "name", "def name(self)"),
    ]
    seams, _ = find_seams(classes, methods)
    host = _by_seam(seams)["Host"]
    assert host["implementers"] == []
    assert host["possible"] == [
        {"name": "Standalone", "module": "h/standalone.py", "missing": ["name"]}
    ]


def test_find_seams_all_attribute_like_seam_does_not_list_every_class() -> None:
    """With only attribute-like members, any class would 'lack only attributes': list none."""
    classes = [
        _cls("d/types.py", "Detector", "class Detector(Protocol)"),
        _cls("d/x.py", "Anything", "class Anything"),
        _cls("d/y.py", "Other", "class Other"),
    ]
    methods = [
        _meth("d/types.py", "Detector", "name", "def name(self) -> str"),
        _meth("d/types.py", "Detector", "detect", "def detect(self) -> bool"),
        _meth("d/y.py", "Other", "name", "def name(self) -> str"),
    ]
    seams, _ = find_seams(classes, methods)
    assert "possible" not in _by_seam(seams)["Detector"]


def test_find_seams_a_missing_real_method_is_not_possible() -> None:
    classes = [
        _cls("h/types.py", "Host", "class Host(Protocol)"),
        _cls("h/half.py", "Half", "class Half"),
    ]
    methods = [
        _meth("h/types.py", "Host", "name", "def name(self) -> str"),
        _meth("h/types.py", "Host", "serve", "async def serve(self, port: int)"),
        _meth("h/types.py", "Host", "stop", "async def stop(self)"),
        _meth("h/half.py", "Half", "serve", "async def serve(self, port)"),
    ]
    seams, _ = find_seams(classes, methods)
    assert "possible" not in _by_seam(seams)["Host"]


def _single_method_seam(seam_sig: str, impl_sigs: dict[str, str]) -> list[str]:
    """Structural implementers of a one-method seam ``run``, by class name."""
    classes = [_cls("s.py", "Seam", "class Seam(Protocol)")]
    methods = [_meth("s.py", "Seam", "run", seam_sig)]
    for name, sig in impl_sigs.items():
        classes.append(_cls(f"{name}.py", name, f"class {name}"))
        methods.append(_meth(f"{name}.py", name, "run", sig))
    seams, _ = find_seams(classes, methods)
    return [i["name"] for i in _by_seam(seams)["Seam"]["implementers"]]


def test_find_seams_requires_the_inherited_members_of_a_parent_seam() -> None:
    """A sub-Protocol's contract includes what it inherits from its parent seam."""
    classes = [
        _cls("t.py", "Tool", "class Tool(Protocol)"),
        _cls("t.py", "Embedder", "class Embedder(Tool, Protocol)"),
        _cls("a.py", "Full", "class Full"),
        _cls("b.py", "OwnOnly", "class OwnOnly"),
    ]
    methods = [
        _meth("t.py", "Tool", "invoke", "async def invoke(self, req)"),
        _meth("t.py", "Tool", "check", "async def check(self)"),
        _meth("t.py", "Embedder", "embed", "async def embed(self, text)"),
        _meth("t.py", "Embedder", "embed_many", "async def embed_many(self, texts)"),
        *(
            _meth("a.py", "Full", n, s)
            for n, s in [
                ("invoke", "async def invoke(self, req)"),
                ("check", "async def check(self)"),
                ("embed", "async def embed(self, text)"),
                ("embed_many", "async def embed_many(self, texts)"),
            ]
        ),
        _meth("b.py", "OwnOnly", "embed", "async def embed(self, text)"),
        _meth("b.py", "OwnOnly", "embed_many", "async def embed_many(self, texts)"),
    ]
    seams, _ = find_seams(classes, methods)
    embedder = _by_seam(seams)["Embedder"]
    assert embedder["methods"] == ["check", "embed", "embed_many", "invoke"]
    assert [i["name"] for i in embedder["implementers"]] == ["Full"]


def test_find_seams_diamond_takes_the_first_base_like_python() -> None:
    """D(B, C) inherits B's version of a member both parents declare, as Python's MRO does."""
    classes = [
        _cls("s.py", "B", "class B(Protocol)"),
        _cls("s.py", "C", "class C(Protocol)"),
        _cls("s.py", "D", "class D(B, C, Protocol)"),
        _cls("i.py", "Impl", "class Impl"),
    ]
    methods = [
        _meth("s.py", "B", "m", "def m(self, x)"),
        _meth("s.py", "C", "m", "def m(self, x, y)"),
        _meth("i.py", "Impl", "m", "def m(self, x)"),
    ]
    seams, _ = find_seams(classes, methods)
    assert [i["name"] for i in _by_seam(seams)["D"]["implementers"]] == ["Impl"]


def test_find_seams_does_not_inherit_from_an_ambiguous_parent_name() -> None:
    """Two unrelated seams named A: B(A) cannot know which one it extends."""
    classes = [
        _cls("a.py", "A", "class A(Protocol)"),
        _cls("u.py", "A", "class A(Protocol)"),
        _cls("b.py", "B", "class B(A, Protocol)"),
        _cls("i.py", "Impl", "class Impl"),
    ]
    methods = [
        _meth("a.py", "A", "run", "def run(self)"),
        _meth("u.py", "A", "zzz", "def zzz(self, q)"),
        _meth("b.py", "B", "go", "def go(self, q)"),
        _meth("i.py", "Impl", "go", "def go(self, q)"),
    ]
    seams, _ = find_seams(classes, methods)
    b = _by_seam(seams)["B"]
    assert b["methods"] == ["go"]
    assert b["contract_ambiguous"] is True
    assert [i["name"] for i in b["implementers"]] == ["Impl"]


def test_find_seams_an_optional_seam_argument_cannot_be_required_by_the_implementer() -> None:
    """Callers may omit an argument the seam makes optional, so the implementer must too."""
    found = _single_method_seam(
        "def run(self, x=0)",
        {"Required": "def run(self, x)", "Optional": "def run(self, x=1)"},
    )
    assert found == ["Optional"]


def test_find_seams_respects_keyword_only_contracts() -> None:
    """Callers pass the seam's keyword-only arguments by name; implementers must accept them."""
    found = _single_method_seam(
        "async def run(self, q, *, mode, retries=1)",
        {
            "IgnoresKeywords": "async def run(self, q)",
            "AcceptsKeywords": "async def run(self, q, *, mode, retries=3)",
            "AcceptsKwargs": "async def run(self, q, **kw)",
            "AsPositional": "async def run(self, q, mode=None, retries=1)",
            "RequiresExtra": "async def run(self, q, *, mode, retries=1, extra)",
            "RequiresOptional": "async def run(self, q, *, mode, retries)",
        },
    )
    assert sorted(found) == ["AcceptsKeywords", "AcceptsKwargs", "AsPositional"]


# ─── load_seams: the view over a real index ──────────────────────────────────


def _write_tree(root: Path) -> None:
    pkg = root / "src" / "genesis"
    pkg.mkdir(parents=True)
    (pkg / "proto.py").write_text(
        "from typing import Protocol\n"
        "class SearchProvider(Protocol):\n"
        '    """A ToolProvider specialized for web search."""\n'
        "    async def search(self, query: str) -> list: ...\n"
        "    async def invoke(self, request: dict) -> dict: ...\n"
    )
    (pkg / "exa.py").write_text(
        "class ExaAdapter:\n"
        "    async def search(self, query, limit=5): return []\n"
        "    async def invoke(self, request): return {}\n"
    )


async def _indexed_db(root: Path) -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await index_codebase(db, root)
    return db


@pytest.mark.asyncio
async def test_load_seams_reads_the_index(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    db = await _indexed_db(tmp_path)
    try:
        view = await load_seams(db)
    finally:
        await db.close()

    assert view["level"] == "seams"
    assert view["complete"] is True
    assert view["pending_reindex"] == 0
    assert view["unparsed"] == 0
    seam = _by_seam(view["seams"])["SearchProvider"]
    assert seam["module"] == "src/genesis/proto.py"
    assert seam["use_when"] == "A ToolProvider specialized for web search."
    assert [(i["name"], i["module"]) for i in seam["implementers"]] == [
        ("ExaAdapter", "src/genesis/exa.py")
    ]
    assert {r["kind"] for r in view["registries"]} == {k for k, _, _ in REGISTRIES}


@pytest.mark.asyncio
async def test_load_seams_reports_an_index_not_yet_healed(tmp_path: Path) -> None:
    """Rows indexed before bases were recorded make the view incomplete, never empty-and-final."""
    _write_tree(tmp_path)
    db = await _indexed_db(tmp_path)
    try:
        await db.execute("UPDATE code_symbols SET signature = NULL WHERE symbol_type = 'class'")
        view = await load_seams(db)
    finally:
        await db.close()

    assert view["complete"] is False
    assert view["pending_reindex"] == 2
    assert view["seams"] == []


@pytest.mark.asyncio
async def test_load_seams_on_a_never_indexed_install_is_not_complete() -> None:
    """A fresh install before its first index run must not read as 'no seams exist'."""
    from genesis.codebase.indexer import ensure_tables

    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await ensure_tables(db)
        view = await load_seams(db)
    assert view["complete"] is False
    assert view["indexed_modules"] == 0
    assert view["seams"] == []


@pytest.mark.asyncio
async def test_seam_lookup_on_a_never_indexed_install_is_not_a_confident_no() -> None:
    from genesis.codebase.indexer import ensure_tables

    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await ensure_tables(db)
        view = await load_seams(db, name="SearchProvider")
    assert "error" in view
    assert view["complete"] is False
    assert view["indexed_modules"] == 0


@pytest.mark.asyncio
async def test_load_seams_unknown_name_is_an_error(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    db = await _indexed_db(tmp_path)
    try:
        view = await load_seams(db, name="NoSuchSeam")
    finally:
        await db.close()
    assert "error" in view
    assert "NoSuchSeam" in view["error"]
    assert view["complete"] is True  # an indexed tree: the "no" is real
    assert view["unparsed"] == 0


@pytest.mark.asyncio
async def test_load_seams_with_unparseable_rows_is_not_complete(tmp_path: Path) -> None:
    """Dropped index rows mean the answer may be missing something: say so."""
    _write_tree(tmp_path)
    db = await _indexed_db(tmp_path)
    try:
        await db.execute(
            "UPDATE code_symbols SET signature = 'class SearchProvider((' "
            "WHERE name = 'SearchProvider'"
        )
        view = await load_seams(db)
        lookup = await load_seams(db, name="SearchProvider")
    finally:
        await db.close()
    assert view["unparsed"] == 1
    assert view["complete"] is False
    assert "error" in lookup
    assert lookup["unparsed"] == 1
    assert lookup["complete"] is False


# ─── the registry list ───────────────────────────────────────────────────────

_PATH_TOKEN = re.compile(r"(?:src|config|\.claude)/[\w./-]+")


def test_every_registry_path_exists() -> None:
    """Each registry names where an instance is added; every path it names must exist."""
    assert REGISTRIES
    for kind, path, how in REGISTRIES:
        named = [path, *_PATH_TOKEN.findall(how)]
        for rel in named:
            assert (REPO_ROOT / rel.rstrip(".")).exists(), f"{kind}: {rel} does not exist"
