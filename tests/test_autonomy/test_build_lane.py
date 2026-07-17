"""Tests for BuildLane — the capability-build lane control service.

Uses the real ``db`` fixture (full schema) and the real recommendation
parser + dispatcher plan validator, faking only the two runtime
collaborators (approval gate, task dispatcher).
"""

from __future__ import annotations

import json

from genesis.autonomy.build_lane import BuildLane
from genesis.autonomy.dispatcher import _validate_plan_content
from genesis.db.crud import build_candidates, task_states

# --------------------------------------------------------------------------
# Fakes + helpers
# --------------------------------------------------------------------------


class FakeGate:
    """Records ensure_approval calls; drives get_request/mark_consumed."""

    def __init__(self):
        self.ensure_calls = []
        self._status = {}  # request_id -> status
        self._consumed = set()
        self._counter = 0

    async def ensure_approval(self, **kwargs):
        self._counter += 1
        request_id = f"req-{self._counter}"
        self.ensure_calls.append(kwargs)
        self._status[request_id] = "pending"
        return ("pending", request_id, "approval pending")

    def set_status(self, request_id, status):
        self._status[request_id] = status

    async def get_request(self, request_id):
        if request_id not in self._status:
            return None
        return {"id": request_id, "status": self._status[request_id]}

    async def mark_consumed(self, request_id):
        if request_id in self._consumed:
            return False
        self._consumed.add(request_id)
        return True


class FakeDispatcher:
    def __init__(self, *, fail=False):
        self.calls = []
        self._fail = fail
        self._counter = 0

    async def submit(self, plan_path, description, *, source="user"):
        self.calls.append((plan_path, description, source))
        if self._fail:
            raise RuntimeError("boom")
        self._counter += 1
        return f"t-{self._counter:012d}"


class FakeItem:
    def __init__(self, file_path="New Genesis Capabilities.md"):
        self.file_path = file_path


_GOOD_SPEC = {
    "requirements": ["Add a widget", "Wire the widget"],
    "steps": [
        {"type": "code", "description": "Write widget.py"},
        {"type": "test", "description": "Add widget test"},
        {"type": "verification", "description": "Run the test"},
    ],
    "success_criteria": ["widget test passes"],
    "risks": ["widget conflicts with gadget"],
    "intended_paths": ["src/genesis/skills/widget/"],
}


def _eval_text(title, verdict, *, build_spec=None, next_step="Produce a widget"):
    """Build a single-item evaluation with a build verdict block."""
    lines = [
        f"## 1. {title}",
        "",
        "### Recommendation",
        "",
        "```yaml",
        "action: BUILD",
        f'next_step: "{next_step}"',
        "effort: Small",
        "scope: V4",
        "confidence: high",
        "architecture_impact: extends",
        f"verdict: {verdict}",
        'verdict_reason: "clear fit"',
    ]
    if build_spec is not None:
        # Inline JSON flow mapping is valid YAML as build_spec's value.
        lines.append(f"build_spec: {json.dumps(build_spec)}")
    lines += ["```", ""]
    return "\n".join(lines)


def _lane(db, gate=None, dispatcher=None, *, enabled=True):
    return BuildLane(
        db=db,
        dispatcher=dispatcher or FakeDispatcher(),
        approval_gate=gate or FakeGate(),
        enabled=enabled,
    )


# --------------------------------------------------------------------------
# item_key
# --------------------------------------------------------------------------


class TestItemKey:
    def test_same_title_same_key(self):
        assert BuildLane.item_key("My Skill") == BuildLane.item_key("My Skill")

    def test_case_and_whitespace_normalized(self):
        assert BuildLane.item_key("  My Skill ") == BuildLane.item_key("my skill")

    def test_edited_title_new_key(self):
        assert BuildLane.item_key("My Skill") != BuildLane.item_key("My Other Skill")

    def test_url_titles_dedupe_on_normalized_url(self):
        a = BuildLane.item_key("Cool tool https://example.com/x?utm_source=z")
        b = BuildLane.item_key("Cool tool https://example.com/x")
        assert a == b


# --------------------------------------------------------------------------
# handle_eval routing
# --------------------------------------------------------------------------


class TestHandleEvalBuild:
    async def test_build_creates_candidate_card_and_plan(self, db, monkeypatch, tmp_path):
        monkeypatch.setattr("genesis.autonomy.build_lane._PLANS_DIR", tmp_path)
        gate = FakeGate()
        lane = _lane(db, gate=gate)

        n = await lane.handle_eval(
            evaluation_text=_eval_text("Widget Skill", "build", build_spec=_GOOD_SPEC),
            batch_id="batch-123",
            item=FakeItem(),
            response_path="/tmp/eval.md",
        )
        assert n == 1

        key = BuildLane.item_key("Widget Skill")
        row = await build_candidates.get_open_by_item_key(db, key)
        assert row is not None
        assert row["verdict"] == "build"
        assert row["approval_request_id"] == "req-1"
        assert row["batch_id"] == "batch-123"
        assert row["plan_path"] and tmp_path.as_posix() in row["plan_path"]

        # Card sent via the gate with build_greenlight semantics.
        assert len(gate.ensure_calls) == 1
        call = gate.ensure_calls[0]
        assert call["action_type"] == "build_greenlight"
        assert call["invocation"] is None
        assert call["subsystem"] == "build_lane"
        extra = call["extra_context"]
        assert extra["title"] == "Widget Skill"
        assert extra["steps_count"] == 3
        assert extra["intended_paths"] == ["src/genesis/skills/widget/"]

        # Materialized plan is dispatcher-valid.
        from pathlib import Path
        _validate_plan_content(Path(row["plan_path"]))

    async def test_permanent_dedup_across_reevals(self, db, monkeypatch, tmp_path):
        monkeypatch.setattr("genesis.autonomy.build_lane._PLANS_DIR", tmp_path)
        gate = FakeGate()
        lane = _lane(db, gate=gate)

        first = await lane.handle_eval(
            evaluation_text=_eval_text("Widget Skill", "build", build_spec=_GOOD_SPEC),
            batch_id="b1", item=FakeItem(),
        )
        # Re-eval same title, different next_step — must NOT re-card.
        second = await lane.handle_eval(
            evaluation_text=_eval_text(
                "Widget Skill", "build", build_spec=_GOOD_SPEC,
                next_step="A slightly different sentence",
            ),
            batch_id="b2", item=FakeItem(),
        )
        assert first == 1
        assert second == 0
        assert len(gate.ensure_calls) == 1

    async def test_decided_item_not_recarded(self, db, monkeypatch, tmp_path):
        """After a candidate is decided, a later eval must not re-card it."""
        monkeypatch.setattr("genesis.autonomy.build_lane._PLANS_DIR", tmp_path)
        gate = FakeGate()
        lane = _lane(db, gate=gate)
        await lane.handle_eval(
            evaluation_text=_eval_text("Widget Skill", "build", build_spec=_GOOD_SPEC),
            batch_id="b1", item=FakeItem(),
        )
        key = BuildLane.item_key("Widget Skill")
        row = await build_candidates.get_open_by_item_key(db, key)
        await build_candidates.record_user_decision(db, row["id"], user_decision="approved")

        # Same item re-evaluated after the build was approved — no new card/row.
        n = await lane.handle_eval(
            evaluation_text=_eval_text("Widget Skill", "build", build_spec=_GOOD_SPEC),
            batch_id="b2", item=FakeItem(),
        )
        assert n == 0
        assert len(gate.ensure_calls) == 1

    async def test_malformed_spec_downgrades_to_needs_discussion(self, db, monkeypatch, tmp_path):
        monkeypatch.setattr("genesis.autonomy.build_lane._PLANS_DIR", tmp_path)
        gate = FakeGate()
        lane = _lane(db, gate=gate)
        bad_spec = dict(_GOOD_SPEC, steps=[])  # empty required list
        await lane.handle_eval(
            evaluation_text=_eval_text("Widget Skill", "build", build_spec=bad_spec),
            batch_id="b1", item=FakeItem(),
        )
        key = BuildLane.item_key("Widget Skill")
        row = await build_candidates.get_any_by_item_key(db, key)
        assert row["verdict"] == "needs_discussion"
        assert row["plan_path"] is None
        assert row["approval_request_id"] is None
        assert gate.ensure_calls == []  # no card for a downgrade

    async def test_missing_spec_on_build_downgrades(self, db, monkeypatch, tmp_path):
        monkeypatch.setattr("genesis.autonomy.build_lane._PLANS_DIR", tmp_path)
        gate = FakeGate()
        lane = _lane(db, gate=gate)
        await lane.handle_eval(
            evaluation_text=_eval_text("Widget Skill", "build", build_spec=None),
            batch_id="b1", item=FakeItem(),
        )
        key = BuildLane.item_key("Widget Skill")
        row = await build_candidates.get_any_by_item_key(db, key)
        assert row["verdict"] == "needs_discussion"
        assert gate.ensure_calls == []


class TestHandleEvalOtherVerdicts:
    async def test_dont_build_records_row_no_card(self, db):
        gate = FakeGate()
        lane = _lane(db, gate=gate)
        await lane.handle_eval(
            evaluation_text=_eval_text("Bad Idea", "dont_build"),
            batch_id="b1", item=FakeItem(),
        )
        key = BuildLane.item_key("Bad Idea")
        row = await build_candidates.get_any_by_item_key(db, key)
        assert row["verdict"] == "dont_build"
        assert row["plan_path"] is None
        assert gate.ensure_calls == []

    async def test_needs_discussion_records_row_no_card(self, db):
        gate = FakeGate()
        lane = _lane(db, gate=gate)
        await lane.handle_eval(
            evaluation_text=_eval_text("Fuzzy Idea", "needs_discussion"),
            batch_id="b1", item=FakeItem(),
        )
        key = BuildLane.item_key("Fuzzy Idea")
        row = await build_candidates.get_any_by_item_key(db, key)
        assert row["verdict"] == "needs_discussion"
        assert gate.ensure_calls == []

    async def test_no_verdict_ignored(self, db):
        gate = FakeGate()
        lane = _lane(db, gate=gate)
        # Ordinary (non-build) recommendation: no verdict field.
        text = (
            "## 1. Some Article\n\n### Recommendation\n\n"
            "```yaml\naction: ADOPT\nnext_step: read it\nscope: V4\n```\n"
        )
        n = await lane.handle_eval(evaluation_text=text, batch_id="b1", item=FakeItem())
        assert n == 0
        assert gate.ensure_calls == []


class TestDisabled:
    async def test_handle_eval_noop_when_disabled(self, db):
        gate = FakeGate()
        lane = _lane(db, gate=gate, enabled=False)
        n = await lane.handle_eval(
            evaluation_text=_eval_text("Widget Skill", "build", build_spec=_GOOD_SPEC),
            batch_id="b1", item=FakeItem(),
        )
        assert n == 0
        assert gate.ensure_calls == []
        assert await build_candidates.list_recent(db) == []

    async def test_poll_noop_when_disabled(self, db):
        lane = _lane(db, enabled=False)
        await lane.poll_pending()  # must not raise


# --------------------------------------------------------------------------
# _materialize_plan
# --------------------------------------------------------------------------


class TestMaterializePlan:
    def test_valid_spec_has_exact_sections(self, monkeypatch, tmp_path):
        monkeypatch.setattr("genesis.autonomy.build_lane._PLANS_DIR", tmp_path)
        lane = BuildLane(db=None, dispatcher=None, approval_gate=None, enabled=True)
        path = lane._materialize_plan("abcdef1234", "Widget Skill", _GOOD_SPEC)
        assert path is not None
        text = (tmp_path / __import__("os").path.basename(path)).read_text()
        for header in ("## Requirements", "## Steps",
                       "## Success Criteria", "## Risks and Failure Modes"):
            assert f"\n{header}\n" in text
        assert "(code) Write widget.py" in text
        assert "(verification) Run the test" in text

    def test_missing_required_list_returns_none(self):
        lane = BuildLane(db=None, dispatcher=None, approval_gate=None, enabled=True)
        assert lane._materialize_plan("k", "T", dict(_GOOD_SPEC, risks=[])) is None
        assert lane._materialize_plan("k", "T", None) is None
        assert lane._materialize_plan("k", "T", {}) is None


# --------------------------------------------------------------------------
# poll_pending — card resolution
# --------------------------------------------------------------------------


async def _card(db, monkeypatch, tmp_path, gate, dispatcher):
    """Create a carded build candidate; return its row id + request id."""
    monkeypatch.setattr("genesis.autonomy.build_lane._PLANS_DIR", tmp_path)
    lane = BuildLane(db=db, dispatcher=dispatcher, approval_gate=gate, enabled=True)
    await lane.handle_eval(
        evaluation_text=_eval_text("Widget Skill", "build", build_spec=_GOOD_SPEC),
        batch_id="b1", item=FakeItem(),
    )
    key = BuildLane.item_key("Widget Skill")
    row = await build_candidates.get_open_by_item_key(db, key)
    return lane, row["id"], row["approval_request_id"]


class TestPollCardResolution:
    async def test_approved_submits_and_marks(self, db, monkeypatch, tmp_path):
        gate, disp = FakeGate(), FakeDispatcher()
        lane, cid, rid = await _card(db, monkeypatch, tmp_path, gate, disp)
        gate.set_status(rid, "approved")
        await lane.poll_pending()

        assert len(disp.calls) == 1
        _plan, _desc, source = disp.calls[0]
        assert source == "build_lane"
        row = await build_candidates.get_by_id(db, cid)
        assert row["user_decision"] == "approved"
        assert row["outcome"] == "submitted"
        assert row["task_id"] is not None

    async def test_double_tap_no_double_submit(self, db, monkeypatch, tmp_path):
        gate, disp = FakeGate(), FakeDispatcher()
        lane, cid, rid = await _card(db, monkeypatch, tmp_path, gate, disp)
        gate.set_status(rid, "approved")
        await lane.poll_pending()
        # Second poll: candidate is decided (not open) AND mark_consumed=False.
        await lane.poll_pending()
        assert len(disp.calls) == 1

    async def test_rejected_abandons_no_submit(self, db, monkeypatch, tmp_path):
        gate, disp = FakeGate(), FakeDispatcher()
        lane, cid, rid = await _card(db, monkeypatch, tmp_path, gate, disp)
        gate.set_status(rid, "rejected")
        await lane.poll_pending()
        assert disp.calls == []
        row = await build_candidates.get_by_id(db, cid)
        assert row["user_decision"] == "rejected"
        assert row["outcome"] == "abandoned"

    async def test_pending_card_waits(self, db, monkeypatch, tmp_path):
        gate, disp = FakeGate(), FakeDispatcher()
        lane, cid, rid = await _card(db, monkeypatch, tmp_path, gate, disp)
        await lane.poll_pending()  # still pending
        assert disp.calls == []
        row = await build_candidates.get_by_id(db, cid)
        assert row["user_decision"] is None
        assert row["outcome"] == "pending"

    async def test_dispatch_failure_marks_build_failed(self, db, monkeypatch, tmp_path):
        gate, disp = FakeGate(), FakeDispatcher(fail=True)
        lane, cid, rid = await _card(db, monkeypatch, tmp_path, gate, disp)
        gate.set_status(rid, "approved")
        await lane.poll_pending()
        row = await build_candidates.get_by_id(db, cid)
        assert row["outcome"] == "build_failed"

    async def test_calibration_row_skipped_by_poll(self, db, monkeypatch, tmp_path):
        # dont_build row has no approval_request_id — poll must skip it cleanly.
        gate, disp = FakeGate(), FakeDispatcher()
        lane = BuildLane(db=db, dispatcher=disp, approval_gate=gate, enabled=True)
        await lane.handle_eval(
            evaluation_text=_eval_text("Bad Idea", "dont_build"),
            batch_id="b1", item=FakeItem(),
        )
        await lane.poll_pending()  # must not raise / submit
        assert disp.calls == []


# --------------------------------------------------------------------------
# poll_pending — outcome reconciliation
# --------------------------------------------------------------------------


async def _submitted_candidate(db, monkeypatch, tmp_path):
    gate, disp = FakeGate(), FakeDispatcher()
    lane, cid, rid = await _card(db, monkeypatch, tmp_path, gate, disp)
    gate.set_status(rid, "approved")
    await lane.poll_pending()
    row = await build_candidates.get_by_id(db, cid)
    return lane, cid, row["task_id"]


async def _set_task(db, task_id, *, phase, outputs=None):
    # The fake dispatcher creates no task_states row — stand one up here.
    # task_states enforces a valid intake token via a DB trigger.
    token = await task_states.create_intake_token(db)
    await task_states.create(
        db, task_id=task_id, description="build",
        current_phase=phase, source="build_lane", intake_token=token,
        outputs=json.dumps(outputs) if outputs is not None else None,
    )


class TestReconcile:
    async def test_pr_opened(self, db, monkeypatch, tmp_path):
        lane, cid, tid = await _submitted_candidate(db, monkeypatch, tmp_path)
        await _set_task(db, tid, phase="completed",
                        outputs={"branch": "task/abc", "pr_url": "https://x/pr/1"})
        await lane.poll_pending()
        row = await build_candidates.get_by_id(db, cid)
        assert row["outcome"] == "pr_opened"
        assert row["pr_url"] == "https://x/pr/1"
        assert row["branch"] == "task/abc"

    async def test_scope_blocked(self, db, monkeypatch, tmp_path):
        lane, cid, tid = await _submitted_candidate(db, monkeypatch, tmp_path)
        await _set_task(db, tid, phase="completed",
                        outputs={"scope_blocked": "touched src/genesis/autonomy/x",
                                 "scope_gate": '{"allowed": false}'})
        await lane.poll_pending()
        row = await build_candidates.get_by_id(db, cid)
        assert row["outcome"] == "scope_blocked"

    async def test_built_when_pushed_but_no_pr(self, db, monkeypatch, tmp_path):
        lane, cid, tid = await _submitted_candidate(db, monkeypatch, tmp_path)
        await _set_task(db, tid, phase="completed",
                        outputs={"branch": "task/abc", "pr_error": "gh boom"})
        await lane.poll_pending()
        row = await build_candidates.get_by_id(db, cid)
        assert row["outcome"] == "built"

    async def test_failed_phase(self, db, monkeypatch, tmp_path):
        lane, cid, tid = await _submitted_candidate(db, monkeypatch, tmp_path)
        await _set_task(db, tid, phase="failed", outputs={})
        await lane.poll_pending()
        row = await build_candidates.get_by_id(db, cid)
        assert row["outcome"] == "build_failed"

    async def test_still_running_stays_submitted(self, db, monkeypatch, tmp_path):
        lane, cid, tid = await _submitted_candidate(db, monkeypatch, tmp_path)
        await _set_task(db, tid, phase="executing", outputs={"branch": "task/abc"})
        await lane.poll_pending()
        row = await build_candidates.get_by_id(db, cid)
        assert row["outcome"] == "submitted"

    async def test_reconcile_survives_calibration_flood(self, db, monkeypatch, tmp_path):
        """A submitted candidate must reconcile even when >100 newer calibration
        rows exist — the reconcile query is outcome-filtered, not recency-bounded."""
        lane, cid, tid = await _submitted_candidate(db, monkeypatch, tmp_path)
        # Flood with 120 unrelated dont_build calibration rows created AFTER it.
        for i in range(120):
            await build_candidates.create(
                db, id=f"flood-{i}", item_key=f"flood-key-{i}",
                item_title=f"noise {i}", source_file="x.md", verdict="dont_build",
            )
        await _set_task(db, tid, phase="completed",
                        outputs={"branch": "task/abc", "pr_url": "https://x/pr/9"})
        await lane.poll_pending()
        row = await build_candidates.get_by_id(db, cid)
        assert row["outcome"] == "pr_opened"


# --------------------------------------------------------------------------
# WS-2 P1b: ledger prediction hook fires from BOTH create sites
# --------------------------------------------------------------------------


class TestLedgerHookWiring:
    async def _ledger_rows(self, db, candidate_id):
        from genesis.db.crud import ledger_predictions

        return await ledger_predictions.list_by_subject(
            db, action_class="build_verdict", subject_ref_id=candidate_id,
        )

    async def test_handle_build_carded_path_writes_prediction(
        self, db, monkeypatch, tmp_path,
    ):
        gate, disp = FakeGate(), FakeDispatcher()
        _lane_obj, cid, _rid = await _card(db, monkeypatch, tmp_path, gate, disp)

        (row,) = await self._ledger_rows(db, cid)
        # _eval_text stamps "confidence: high" -> stated 0.85 on a build verdict
        assert (row["metric"], row["provenance"], row["confidence"]) == (
            "user_greenlights", "stated", 0.85,
        )

    async def test_record_calibration_path_writes_complement_prediction(self, db):
        lane = _lane(db)
        await lane.handle_eval(
            evaluation_text=_eval_text("Meh Skill", "dont_build"),
            batch_id="b2", item=FakeItem(),
        )
        key = BuildLane.item_key("Meh Skill")
        cur = await db.execute(
            "SELECT id FROM build_candidates WHERE item_key = ?", (key,),
        )
        cid = (await cur.fetchone())[0]

        (row,) = await self._ledger_rows(db, cid)
        # dont_build predicts NO greenlight: "high" label inverts to 0.15
        assert (row["metric"], row["provenance"]) == ("user_greenlights", "stated")
        assert abs(row["confidence"] - 0.15) < 1e-9
