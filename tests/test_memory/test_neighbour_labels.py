"""Short labels for graph neighbours (memory spec MW-11, labelled links).

A session that sees a related memory used to get a bare id. These tests pin the
label contract: a few cleaned words of the neighbour's own text, and NO label at
all for a neighbour whose text must not be shown unasked: a credential entry,
external or unknown-origin text, garbage, a missing point, or a hidden memory the
caller did not opt into. Two reads fail CLOSED (every label withheld): the
credential lookup and, when hidden memories were not asked for, visibility.
"""

from __future__ import annotations

from types import SimpleNamespace

import aiosqlite
import pytest

from genesis.memory.provenance import label_text, neighbour_labels

FAKE_SECRET = "fakeLabelSecret-Zq9x"


# ── label_text: the pure text contract ────────────────────────────────────


def test_label_takes_the_first_words():
    assert label_text("Deploy guard refuses a dirty tree before restart", max_words=6) == (
        "Deploy guard refuses a dirty tree…"
    )


def test_short_text_is_not_marked_cut():
    assert label_text("Qdrant runs on port 6333", max_words=6) == "Qdrant runs on port 6333"


def test_extraction_prefix_and_whitespace_are_dropped():
    assert label_text("[discovery]   the  hook\n\ttimes out", max_words=6) == "the hook times out"


def test_brackets_never_reach_the_label():
    # The hook prints labels inside a one-line [tag]; a stray bracket would end it.
    label = label_text("see [issue] (old) {x} <y> here", max_words=10)
    assert label is not None
    assert not set(label) & set("[](){}<>")
    assert label == "see issue old x y here"


def test_markdown_markers_and_bullets_are_dropped():
    assert label_text("# Compute ## Available - **Ollama** `x` ---", max_words=10) == (
        "Compute Available Ollama x"
    )


def test_label_stays_one_line():
    label = label_text("first line]\nsecond line", max_words=10)
    assert label is not None
    assert "\n" not in label and "]" not in label


def test_char_cap_cuts_on_a_word():
    label = label_text("alpha " * 40, max_words=40, max_chars=20)
    assert label is not None
    assert len(label) <= 21  # 20 plus the ellipsis
    assert label.endswith("…")
    assert "alph…" not in label  # never mid-word


@pytest.mark.parametrize(
    "content",
    [None, "", "   ", '{"drift_detected": true, "x": 1}', "---\ntype: note\n", "[x] "],
)
def test_garbage_or_empty_has_no_label(content):
    assert label_text(content, max_words=6) is None


# ── neighbour_labels: the batched lookup and its exclusions ───────────────


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    await conn.execute(
        "CREATE TABLE memory_metadata (memory_id TEXT PRIMARY KEY, invalid_at TEXT, "
        "deprecated INTEGER, origin_class TEXT)"
    )
    await conn.execute(
        "CREATE TABLE knowledge_units (id TEXT, qdrant_id TEXT, project_type TEXT, domain TEXT)"
    )
    await conn.execute("CREATE INDEX idx_ku_q ON knowledge_units(qdrant_id)")
    try:
        yield conn
    finally:
        await conn.close()


class FakeQdrant:
    def __init__(self, points: dict[str, dict], *, fail: bool = False, extra: dict | None = None):
        self.points = points
        self.fail = fail
        self.extra = extra or {}
        self.calls: list[dict] = []

    def retrieve(self, collection_name, ids, with_payload):
        self.calls.append({"collection": collection_name, "ids": list(ids), "with": with_payload})
        if self.fail:
            raise RuntimeError("qdrant down")
        out = [SimpleNamespace(id=i, payload=self.points[i]) for i in ids if i in self.points]
        out += [SimpleNamespace(id=i, payload=p) for i, p in self.extra.items()]
        return out


async def _meta(db, mid, *, origin="first_party", deprecated=0, invalid_at=None):
    await db.execute(
        "INSERT INTO memory_metadata VALUES (?, ?, ?, ?)", (mid, invalid_at, deprecated, origin)
    )


async def test_an_ordinary_neighbour_gets_a_label(db):
    await _meta(db, "a")
    q = FakeQdrant({"a": {"content": "Hook budget is eight seconds from start"}})
    assert await neighbour_labels(db, q, ["a"], include_hidden=False, max_words=6) == {
        "a": "Hook budget is eight seconds from…"
    }
    assert q.calls and q.calls[0]["collection"] == "episodic_memory"


@pytest.mark.parametrize(
    ("project", "domain"),
    [
        ("reference", "reference.credentials"),
        (" Reference ", " REFERENCE.CREDENTIALS "),  # the canonical test trims and folds
    ],
)
async def test_a_credential_entry_never_gets_a_label(db, project, domain):
    await _meta(db, "cred")
    await _meta(db, "ok")
    await db.execute("INSERT INTO knowledge_units VALUES ('u1', 'cred', ?, ?)", (project, domain))
    q = FakeQdrant(
        {
            "cred": {"content": f"Router login Value: {FAKE_SECRET}"},
            "ok": {"content": "plain note"},
        }
    )
    labels = await neighbour_labels(db, q, ["cred", "ok"], include_hidden=True, max_words=12)
    assert labels == {"ok": "plain note"}
    assert FAKE_SECRET not in repr(labels)


async def test_a_non_credential_reference_is_labelled(db):
    await _meta(db, "url")
    await db.execute(
        "INSERT INTO knowledge_units VALUES ('u2', 'url', 'reference', 'reference.url')"
    )
    q = FakeQdrant({"url": {"content": "Dashboard address for the host"}})
    assert await neighbour_labels(db, q, ["url"], include_hidden=False, max_words=12) == {
        "url": "Dashboard address for the host"
    }


@pytest.mark.parametrize("stored", ["external_untrusted", "something_unknown"])
async def test_external_or_unknown_stored_origin_gets_no_label(db, stored):
    await _meta(db, "x", origin=stored)
    # The payload claims first-party; the STORED origin is what counts.
    q = FakeQdrant({"x": {"content": "text from an outside source", "origin_class": "first_party"}})
    assert await neighbour_labels(db, q, ["x"], include_hidden=False, max_words=6) == {}


async def test_external_payload_origin_without_stored_row_gets_no_label(db):
    q = FakeQdrant({"x": {"content": "scraped text", "origin_class": "external_untrusted"}})
    assert await neighbour_labels(db, q, ["x"], include_hidden=False, max_words=6) == {}


async def test_garbage_and_missing_neighbours_get_no_label(db):
    await _meta(db, "g")
    await _meta(db, "gone")
    q = FakeQdrant({"g": {"content": '{"operation": "x", "tags": []}'}})
    assert await neighbour_labels(db, q, ["g", "gone"], include_hidden=False, max_words=6) == {}


async def test_an_id_qdrant_returns_unasked_is_ignored(db):
    await _meta(db, "a")
    q = FakeQdrant(
        {"a": {"content": "asked for"}},
        extra={"z": {"content": "never asked", "origin_class": "first_party"}},
    )
    assert await neighbour_labels(db, q, ["a"], include_hidden=False, max_words=6) == {
        "a": "asked for"
    }


async def test_a_hidden_neighbour_is_labelled_only_when_the_caller_opted_in(db):
    await _meta(db, "old", deprecated=1)
    await _meta(db, "expired", invalid_at="2000-01-01T00:00:00+00:00")
    await _meta(db, "live")
    q = FakeQdrant(
        {
            "old": {"content": "old rule"},
            "expired": {"content": "expired fact"},
            "live": {"content": "live"},
        }
    )
    ids = ["old", "expired", "live"]
    assert await neighbour_labels(db, q, ids, include_hidden=False, max_words=6) == {"live": "live"}
    assert await neighbour_labels(db, q, ids, include_hidden=True, max_words=6) == {
        "old": "old rule",
        "expired": "expired fact",
        "live": "live",
    }


async def test_a_failed_credential_read_withholds_every_label(db):
    await _meta(db, "a")
    await db.execute("DROP TABLE knowledge_units")
    q = FakeQdrant({"a": {"content": "plain note"}})
    assert await neighbour_labels(db, q, ["a"], include_hidden=True, max_words=6) is None


async def test_a_failed_visibility_read_withholds_labels_unless_hidden_was_asked(db):
    await db.execute("DROP TABLE memory_metadata")
    await db.execute(
        "CREATE TABLE memory_metadata (memory_id TEXT PRIMARY KEY, origin_class TEXT)"
    )  # no visibility columns: the predicate query fails
    await db.execute("INSERT INTO memory_metadata VALUES ('a', 'first_party')")
    q = FakeQdrant({"a": {"content": "plain note"}})
    assert await neighbour_labels(db, q, ["a"], include_hidden=False, max_words=6) is None
    assert await neighbour_labels(db, q, ["a"], include_hidden=True, max_words=6) == {
        "a": "plain note"
    }


async def test_a_failed_content_read_returns_no_labels(db):
    await _meta(db, "a")
    q = FakeQdrant({}, fail=True)
    assert await neighbour_labels(db, q, ["a"], include_hidden=False, max_words=6) == {}


async def test_no_ids_means_no_reads(db):
    q = FakeQdrant({})
    assert await neighbour_labels(db, q, [], include_hidden=False, max_words=6) == {}
    assert q.calls == []


async def test_a_credential_point_without_its_row_still_gets_no_label(db):
    # A failed cleanup can leave the point with no knowledge_units row.
    await _meta(db, "head")
    await _meta(db, "tag")
    q = FakeQdrant(
        {
            "head": {"content": f"[reference.credentials] Router Value: {FAKE_SECRET}"},
            "tag": {
                "content": f"Router Value: {FAKE_SECRET}",
                "tags": ["reference", "credentials"],
            },
        }
    )
    assert await neighbour_labels(db, q, ["head", "tag"], include_hidden=False, max_words=12) == {}
    assert "tags" in q.calls[0]["with"]


@pytest.mark.parametrize("stored", [None, ""])
async def test_unknown_origin_gets_no_label(db, stored):
    await _meta(db, "x", origin=stored)
    # An empty stored origin is a value, not a missing one: it is not a safe
    # origin, so the payload's first_party claim must not stand in for it.
    payload_origin = "first_party" if stored == "" else None
    q = FakeQdrant({"x": {"content": "a note of unknown origin", "origin_class": payload_origin}})
    assert await neighbour_labels(db, q, ["x"], include_hidden=False, max_words=6) == {}


async def test_a_null_stored_origin_falls_back_to_the_payload(db):
    await _meta(db, "x", origin=None)
    q = FakeQdrant({"x": {"content": "a first party note", "origin_class": "first_party"}})
    assert await neighbour_labels(db, q, ["x"], include_hidden=False, max_words=6) == {
        "x": "a first party note"
    }


def test_control_and_format_characters_are_dropped():
    label = label_text("abc‮def​ ghi\x1b[31m jkl\x00", max_words=10)
    assert label is not None
    assert all(c.isprintable() and c not in "‮​" for c in label)


@pytest.mark.parametrize(
    "content",
    [
        f"\ufeff[reference.credentials] Router Value: {FAKE_SECRET}",
        f"\u200b[reference.credentials] Router Value: {FAKE_SECRET}",
        f"[ reference.credentials] Router Value: {FAKE_SECRET}",
        f"**[reference.credentials]** Router Value: {FAKE_SECRET}",
        f"\uff3breference.credentials\uff3d Router Value: {FAKE_SECRET}",  # fullwidth
        f"[reference .credentials] Router Value: {FAKE_SECRET}",
        f"[REFERENCE.CREDENTIALS] Router Value: {FAKE_SECRET}",
        f"\x00[reference.credentials] Router Value: {FAKE_SECRET}",
        f"- [reference.credentials] Router Value: {FAKE_SECRET}",
        f"&gt; [reference.credentials] Router Value: {FAKE_SECRET}",
        f"\u200d\u00a0[reference.credentials] Router Value: {FAKE_SECRET}",
    ],
)
async def test_a_disguised_credential_header_still_gets_no_label(db, content):
    await _meta(db, "head")
    q = FakeQdrant({"head": {"content": content}})
    assert await neighbour_labels(db, q, ["head"], include_hidden=False, max_words=12) == {}


@pytest.mark.parametrize(
    "tags",
    [
        '["reference", "credentials"]',
        '"credentials"',
        '{"a": 1}',
        7,
        {"a": 1},
        ("reference", "credentials"),
        {"credentials"},
    ],
)
async def test_odd_tag_shapes_never_let_a_credential_through(db, tags):
    await _meta(db, "t")
    q = FakeQdrant({"t": {"content": f"Router Value: {FAKE_SECRET}", "tags": tags}})
    assert await neighbour_labels(db, q, ["t"], include_hidden=False, max_words=12) == {}


async def test_a_malformed_origin_costs_only_its_own_label(db):
    await _meta(db, "bad", origin=None)
    await _meta(db, "ok")
    q = FakeQdrant(
        {"bad": {"content": "odd", "origin_class": ["first_party"]}, "ok": {"content": "fine note"}}
    )
    assert await neighbour_labels(db, q, ["bad", "ok"], include_hidden=False, max_words=6) == {
        "ok": "fine note"
    }


async def test_absent_tags_do_not_block_an_ordinary_label(db):
    await _meta(db, "n")
    q = FakeQdrant({"n": {"content": "an ordinary note", "tags": None}})
    assert await neighbour_labels(db, q, ["n"], include_hidden=False, max_words=6) == {
        "n": "an ordinary note"
    }


async def test_a_pathological_tag_string_does_not_raise(db):
    await _meta(db, "p")
    q = FakeQdrant({"p": {"content": "an ordinary note", "tags": "[" * 100_000}})
    assert await neighbour_labels(db, q, ["p"], include_hidden=False, max_words=6) == {
        "p": "an ordinary note"
    }
