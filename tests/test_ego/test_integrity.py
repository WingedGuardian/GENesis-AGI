"""Tests for ego pipeline integrity tracking.

Covers: integrity utility, CRUD columns, realist gate original_content
preservation, and cycle storage with hash/size.
"""

from __future__ import annotations

from genesis.db.crud import ego as ego_crud
from genesis.ego.integrity import (
    canonical_json,
    chained_hash,
    content_hash,
    content_size,
    verify_chain,
)

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


# ── Proposal dedup: content_hash enforcement in create_batch ────────────────


async def test_create_batch_skips_exact_duplicate(db):
    """create_batch skips proposals whose content_hash matches a pending one."""
    from genesis.ego.proposals import ProposalWorkflow

    wf = ProposalWorkflow(db=db)

    # Create first batch — should succeed
    _, ids1 = await wf.create_batch(
        [{"content": "Investigate star spike", "action_type": "investigate"}],
        cycle_id="cycle-1",
        ego_source="user_ego_cycle",
    )
    assert len(ids1) == 1

    # Create second batch with identical content — should be deduped
    _, ids2 = await wf.create_batch(
        [{"content": "Investigate star spike", "action_type": "investigate"}],
        cycle_id="cycle-2",
        ego_source="user_ego_cycle",
    )
    assert len(ids2) == 0  # deduped — no new proposals created


async def test_create_batch_allows_different_content(db):
    """create_batch allows proposals with different content."""
    from genesis.ego.proposals import ProposalWorkflow

    wf = ProposalWorkflow(db=db)

    _, ids1 = await wf.create_batch(
        [{"content": "Investigate star spike", "action_type": "investigate"}],
        cycle_id="cycle-1",
        ego_source="user_ego_cycle",
    )
    assert len(ids1) == 1

    _, ids2 = await wf.create_batch(
        [{"content": "Draft Discord milestone post", "action_type": "dispatch"}],
        cycle_id="cycle-2",
        ego_source="user_ego_cycle",
    )
    assert len(ids2) == 1  # different content — allowed


async def test_create_batch_allows_after_rejection(db):
    """Re-proposing content that was rejected should succeed."""
    from genesis.ego.proposals import ProposalWorkflow

    wf = ProposalWorkflow(db=db)

    _, ids1 = await wf.create_batch(
        [{"content": "Investigate star spike", "action_type": "investigate"}],
        cycle_id="cycle-1",
        ego_source="user_ego_cycle",
    )
    assert len(ids1) == 1

    # Reject the proposal
    await ego_crud.resolve_proposal(db, ids1[0], status="rejected")

    # Re-propose — should succeed since the original is no longer pending
    _, ids2 = await wf.create_batch(
        [{"content": "Investigate star spike", "action_type": "investigate"}],
        cycle_id="cycle-2",
        ego_source="user_ego_cycle",
    )
    assert len(ids2) == 1  # allowed — prior proposal was rejected


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


# ── Hash chain functions (Verified Autonomy L8) ──────────────────────────────


def test_canonical_json_key_order_independent():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_canonical_json_deterministic():
    d = {"action_type": "investigate", "description": "test"}
    assert canonical_json(d) == canonical_json(d)


def test_canonical_json_no_whitespace():
    result = canonical_json({"a": 1, "b": 2})
    assert " " not in result


def test_chained_hash_deterministic():
    h1 = chained_hash("abc123", "prev456")
    h2 = chained_hash("abc123", "prev456")
    assert h1 == h2


def test_chained_hash_genesis_sentinel():
    """First record in chain uses 'genesis' as sentinel."""
    h = chained_hash("abc123", None)
    expected_payload = "genesis:abc123"
    import hashlib
    assert h == hashlib.sha256(expected_payload.encode()).hexdigest()


def test_chained_hash_different_previous():
    h1 = chained_hash("content", "prev_a")
    h2 = chained_hash("content", "prev_b")
    assert h1 != h2


def test_chained_hash_different_content():
    h1 = chained_hash("content_a", "prev")
    h2 = chained_hash("content_b", "prev")
    assert h1 != h2


def test_verify_chain_empty():
    valid, idx = verify_chain([])
    assert valid is True
    assert idx == -1


def test_verify_chain_single_record():
    ch = content_hash("hello")
    chain = chained_hash(ch, None)
    records = [{"content_hash": ch, "previous_hash": None, "chain_hash": chain}]
    valid, idx = verify_chain(records)
    assert valid is True
    assert idx == -1


def test_verify_chain_three_records():
    records = []
    prev = None
    for text in ["first", "second", "third"]:
        ch = content_hash(text)
        chain = chained_hash(ch, prev)
        records.append({
            "content_hash": ch,
            "previous_hash": prev,
            "chain_hash": chain,
        })
        prev = chain

    valid, idx = verify_chain(records)
    assert valid is True
    assert idx == -1


def test_verify_chain_tampered_content():
    """Modifying content of a record breaks the chain."""
    records = []
    prev = None
    for text in ["first", "second", "third"]:
        ch = content_hash(text)
        chain = chained_hash(ch, prev)
        records.append({
            "content_hash": ch,
            "previous_hash": prev,
            "chain_hash": chain,
        })
        prev = chain

    # Tamper with second record's content
    records[1]["content_hash"] = content_hash("TAMPERED")

    valid, idx = verify_chain(records)
    assert valid is False
    assert idx == 1  # chain breaks at the tampered record


def test_verify_chain_tampered_previous_hash():
    """Modifying previous_hash link breaks the chain."""
    records = []
    prev = None
    for text in ["first", "second", "third"]:
        ch = content_hash(text)
        chain = chained_hash(ch, prev)
        records.append({
            "content_hash": ch,
            "previous_hash": prev,
            "chain_hash": chain,
        })
        prev = chain

    # Break the link between records 1 and 2
    records[2]["previous_hash"] = "bogus_hash"

    valid, idx = verify_chain(records)
    assert valid is False
    assert idx == 2


def test_verify_chain_skips_pre_migration():
    """Records with chain_hash=None (pre-migration) are skipped."""
    pre_migration = {"content_hash": "old", "previous_hash": None, "chain_hash": None}

    ch = content_hash("new")
    chain = chained_hash(ch, None)
    post_migration = {"content_hash": ch, "previous_hash": None, "chain_hash": chain}

    valid, idx = verify_chain([pre_migration, post_migration])
    assert valid is True
    assert idx == -1
