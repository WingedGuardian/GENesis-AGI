"""The owned cursor/timer journal commits structure, never proposal authority."""

import copy
import uuid
from pathlib import Path

import pytest

from genesis.eval.qualification.evidence import Campaign

ROOT = Path(__file__).resolve().parents[2]
IDENTIFIER = "aaaaaaaa-1234-4567-8912-bbbbbbbbbbbb"


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/ci"))
    import ego_owned_state

    return ego_owned_state


@pytest.fixture
def workload(tmp_path):
    return {"root": str(tmp_path), "executor": "claude-code", "model": "sonnet",
            "ego_source": "genesis_ego_cycle", "source_pin": "a" * 40,
            "sources": {name: "b" * 64 for name in ("state", "loop", "context", "bridge", "cases")},
            "trajectory_id": IDENTIFIER, "reference_ids": [IDENTIFIER], "start": 100,
            "feed": [], "responses": [], "pacing": {"base": 5, "factor": 2, "cap": 40, "hold": 1}}


def event(seq=1, due=100, summary="same summary"):
    return {"id": f"event-{seq}", "seq": seq, "due": due, "source": "fixture",
            "target": "genesis_ego_cycle", "type": "observation", "content": summary}


def binding(attempt):
    return {"attempt_id": attempt["id"], "trigger_id": IDENTIFIER, "nonce": "c" * 32,
            "config_sha256": "d" * 64, "prompt_sha256": "e" * 64, "prompt_status": "0"}


def complete(state, journal, now, outcome="noop"):
    choice = state.choose(now)
    attempt = state.start(journal, choice, now, {"state": None, "effect": None})
    state.bind(journal, binding(attempt))
    state.finish(journal, outcome, now, {"artifact": "owned synthetic receipt", "related": []})
    return choice


def test_empty_feed_generates_own_timers_and_restores_ladder(module, workload, tmp_path):
    with Campaign(tmp_path / "journal") as journal:
        state = module.State.restore(journal, workload)
        times = [100, 105, 115, 135, 175, 215]
        delays = [5, 10, 20, 40, 40, 40]
        for index, (now, delay) in enumerate(zip(times, delays, strict=True), 1):
            chosen = complete(state, journal, now)
            assert chosen["kind"] == "timer" and chosen["ordinal"] == index
            assert state.cursor == 0 and state.timer_done == index
            assert state.wake_at == now + delay
    with Campaign(tmp_path / "journal") as journal:
        restored = module.State.restore(journal, workload)
        assert restored.wake_at == 255 and restored.timer_done == 6 and restored.cursor == 0
        assert restored.choose(254) is None
        assert restored.choose(255)["ordinal"] == 7


@pytest.mark.parametrize("arrival", [101, 105])
def test_reactive_arrival_preempts_backoff_and_coalesces_tie(module, workload, tmp_path, arrival):
    workload["feed"] = [event(due=arrival)]
    with Campaign(tmp_path / "journal") as journal:
        state = module.State.restore(journal, workload)
        complete(state, journal, 100)
        assert state.wait_until(100) == arrival
        state.admit(journal, arrival)
        chosen = complete(state, journal, arrival)
        assert chosen["kind"] == "reactive"
        assert state.cursor == 1 and state.timer_done == state.timer_reserved == 1
        assert state.level == 0 and state.wake_at == arrival
        assert state.choose(arrival)["ordinal"] == 2


@pytest.mark.parametrize("count", [0, 1, 15, 16, 17, 64])
def test_bursts_preserve_distinct_ids_even_with_equal_summary(module, workload, tmp_path, count):
    workload["feed"] = [event(seq=i + 1) for i in range(count)]
    with Campaign(tmp_path / "journal") as journal:
        state = module.State.restore(journal, workload)
        state.admit(journal, 100)
        length = len(journal.lines)
        state.admit(journal, 100)  # Redelivery of the same durable source.
        assert len(journal.lines) == length
        selected = [complete(state, journal, 100)["id"] for _ in range(count)]
        assert selected == [f"event-{i + 1}" for i in range(count)]
        assert state.cursor == count and state.timer_reserved == 0
    with Campaign(tmp_path / "journal") as journal:
        state = module.State.restore(journal, workload)
        state.admit(journal, 100)
        assert state.cursor == count and state.choose(100)["kind"] == "timer"


def test_retry_retains_event_and_new_intake_cannot_bypass_it(module, workload, tmp_path):
    workload["feed"] = [event(), event(seq=2, due=101)]
    with Campaign(tmp_path / "journal") as journal:
        state = module.State.restore(journal, workload)
        state.admit(journal, 100)
        complete(state, journal, 100, "retry")
        state.admit(journal, 101)
        assert state.cursor == 0 and state.choose(104) is None
        assert state.wait_until(101) == 105
        assert complete(state, journal, 105)["id"] == "event-1"
        assert state.cursor == 1 and state.choose(105)["id"] == "event-2"


@pytest.mark.parametrize("phase", ["prepared", "bound", "uncertain", "blocked"])
def test_unpaired_or_halted_work_cannot_restart(module, workload, tmp_path, phase):
    with Campaign(tmp_path / "journal") as journal:
        state = module.State.restore(journal, workload)
        attempt = state.start(journal, state.choose(100), 100, {"state": None, "effect": None})
        if phase != "prepared":
            state.bind(journal, binding(attempt))
        if phase in {"uncertain", "blocked"}:
            state.finish(journal, phase, 100, {"related": []})
    with Campaign(tmp_path / "journal") as journal:
        state = module.State.restore(journal, workload)
        assert state.cursor == 0 and state.choose(999) is None and state.wait_until(999) is None
        assert state.active is not None or state.halted == phase


def test_torn_tail_refused_even_after_valid_completion(module, workload, tmp_path):
    with Campaign(tmp_path / "journal") as journal:
        state = module.State.restore(journal, workload)
        complete(state, journal, 100)
    path = tmp_path / "journal/answers.jsonl"
    with path.open("ab") as stream:
        stream.write(b'{"kind":')
    with Campaign(tmp_path / "journal") as journal, pytest.raises(ValueError, match="torn"):
        module.State.restore(journal, workload)


def test_false_cursor_transition_cannot_be_replayed(module, workload, tmp_path):
    workload["feed"] = [event()]
    with Campaign(tmp_path / "journal") as journal:
        state = module.State.restore(journal, workload)
        state.admit(journal, 100)
        complete(state, journal, 100)
        lines = journal.lines
    lines[-1]["data"]["next"]["cursor"] = 99
    with pytest.raises(ValueError, match="transition"):
        module.State.replay(lines, workload)


@pytest.mark.parametrize("field", ["intake", "prepared", "cursor"])
def test_boolean_cannot_impersonate_sequence_or_cursor(module, workload, tmp_path, field):
    workload["feed"] = [event()]
    with Campaign(tmp_path / "journal") as journal:
        state = module.State.restore(journal, workload)
        state.admit(journal, 100)
        complete(state, journal, 100)
        rows = journal.lines
    if field == "intake":
        rows[1]["data"]["event"]["seq"] = True
    elif field == "prepared":
        rows[2]["data"]["attempt"]["event"]["seq"] = True
    else:
        rows[-1]["data"]["next"]["cursor"] = True
    with pytest.raises(ValueError):
        module.State.replay(rows, workload)


@pytest.mark.parametrize("defect", ["duplicate_id", "sequence", "target", "past_order", "nan", "model", "source", "reference", "extra"])
def test_invalid_workload_is_rejected_before_state(module, workload, defect):
    workload["feed"] = [event(), event(2)]
    if defect == "duplicate_id":
        workload["feed"][1]["id"] = "event-1"
    elif defect == "sequence":
        workload["feed"][1]["seq"] = 9
    elif defect == "target":
        workload["feed"][1]["target"] = "user_ego_cycle"
    elif defect == "past_order":
        workload["feed"][1]["due"] = 99
    elif defect == "nan":
        workload["start"] = float("nan")
    elif defect == "model":
        workload["model"] = ""
    elif defect == "source":
        workload["source_pin"] = "unbound"
    elif defect == "reference":
        workload["reference_ids"].append("unissued")
    else:
        workload["unexpected"] = True
    with pytest.raises(ValueError):
        module.State(workload)


def test_manifest_change_and_illegal_second_writer_are_refused(module, workload, tmp_path):
    with Campaign(tmp_path / "journal") as journal:
        module.State.restore(journal, workload)
        with pytest.raises(ValueError, match="another"), Campaign(tmp_path / "journal"):
            pass
    changed = copy.deepcopy(workload)
    changed["model"] = "haiku"
    with Campaign(tmp_path / "journal") as journal, pytest.raises(ValueError, match="manifest"):
        module.State.restore(journal, changed)


def test_failed_journal_append_never_publishes_in_memory_cursor(module, workload, tmp_path, monkeypatch):
    with Campaign(tmp_path / "journal") as journal:
        state = module.State.restore(journal, workload)
        attempt = state.start(journal, state.choose(100), 100, {"state": None, "effect": None})
        state.bind(journal, binding(attempt))
        before = state.summary()

        def failed(*args, **kwargs):
            raise OSError("owned short journal write")

        monkeypatch.setattr(journal, "append", failed)
        with pytest.raises(OSError, match="short journal"):
            state.finish(journal, "completed", 100, {"related": []})
        assert state.summary() == before


def test_binding_must_follow_prepared_attempt(module, workload, tmp_path):
    with Campaign(tmp_path / "journal") as journal:
        state = module.State.restore(journal, workload)
        with pytest.raises(ValueError, match="bound"):
            state.bind(journal, binding({"id": uuid.uuid4().hex}))
