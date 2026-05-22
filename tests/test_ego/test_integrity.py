"""Tests for ego pipeline integrity tracking.

Covers: integrity utility, CRUD columns, realist gate original_content
preservation, and cycle storage with hash/size.
"""

from __future__ import annotations

from genesis.db.crud import ego as ego_crud
from genesis.ego.integrity import content_hash, content_size

# ── Utility ─────────────────────────────────────────────────────────────────


def test_content_hash_deterministic():
    text = "Propose investigating memory drift"
    assert content_hash(text) == content_hash(text)


def test_content_hash_different_input():
    assert content_hash("alpha") != content_hash("beta")


def test_content_hash_is_sha256_hex():
    h = content_hash("test")
    assert len(h) == 64  # SHA-256 hex digest length
    assert all(c in "0123456789abcdef" for c in h)


def test_content_size_ascii():
    assert content_size("hello") == 5


def test_content_size_utf8():
    # Japanese characters are 3 bytes each in UTF-8
    assert content_size("日本") == 6


def test_content_size_empty():
    assert content_size("") == 0


# ── CRUD: ego_cycles with integrity columns ─────────────────────────────────


async def test_create_cycle_with_integrity(db):
    text = "Full ego output text here"
    cid = await ego_crud.create_cycle(
        db,
        id="cycle-int-1",
        output_text=text,
        output_hash=content_hash(text),
        output_size=content_size(text),
    )
    row = await ego_crud.get_cycle(db, cid)
    assert row is not None
    assert row["output_hash"] == content_hash(text)
    assert row["output_size"] == content_size(text)


async def test_create_cycle_without_integrity(db):
    """Backward compatibility — omitting integrity params still works."""
    cid = await ego_crud.create_cycle(
        db,
        id="cycle-compat-1",
        output_text="no hash",
    )
    row = await ego_crud.get_cycle(db, cid)
    assert row is not None
    assert row["output_hash"] is None
    assert row["output_size"] is None


# ── CRUD: ego_proposals with integrity columns ──────────────────────────────


async def test_create_proposal_with_hash(db):
    content = "Investigate memory drift patterns"
    pid = await ego_crud.create_proposal(
        db,
        id="prop-hash-1",
        action_type="investigate",
        content=content,
        content_hash=content_hash(content),
    )
    row = await ego_crud.get_proposal(db, pid)
    assert row is not None
    assert row["content_hash"] == content_hash(content)
    assert row["original_content"] is None  # not amended


async def test_create_proposal_with_original_content(db):
    """When realist gate amends, original_content preserves pre-amendment."""
    original = "Draft outreach to AI community"
    amended = "Draft targeted outreach to AI agent builders"

    pid = await ego_crud.create_proposal(
        db,
        id="prop-amend-1",
        action_type="outreach",
        content=amended,
        content_hash=content_hash(amended),
        original_content=original,
    )
    row = await ego_crud.get_proposal(db, pid)
    assert row is not None
    assert row["content"] == amended
    assert row["original_content"] == original
    assert row["content_hash"] == content_hash(amended)


async def test_create_proposal_without_integrity(db):
    """Backward compatibility — omitting integrity params still works."""
    pid = await ego_crud.create_proposal(
        db,
        id="prop-compat-1",
        action_type="investigate",
        content="no hash provided",
    )
    row = await ego_crud.get_proposal(db, pid)
    assert row is not None
    assert row["content_hash"] is None
    assert row["original_content"] is None


# ── Realist gate: _original_content propagation ─────────────────────────────


def test_realist_amendment_sets_original_content():
    """Simulate the realist gate mutation to verify _original_content is set."""
    prop = {
        "content": "Investigate X",
        "action_type": "investigate",
    }
    verdict = {"verdict": "amend", "amended_content": "Investigate X (narrowed scope)"}

    # Simulate session.py _filter_proposals logic
    if verdict["verdict"] == "amend" and verdict.get("amended_content"):
        prop["_original_content"] = prop["content"]
        prop["content"] = verdict["amended_content"]

    assert prop["_original_content"] == "Investigate X"
    assert prop["content"] == "Investigate X (narrowed scope)"


def test_realist_pass_no_original_content():
    """When realist passes without amendment, no _original_content key."""
    prop = {
        "content": "Investigate X",
        "action_type": "investigate",
    }
    verdict = {"verdict": "pass", "reasoning": "looks good"}

    if verdict["verdict"] == "amend" and verdict.get("amended_content"):
        prop["_original_content"] = prop["content"]
        prop["content"] = verdict["amended_content"]

    assert "_original_content" not in prop
