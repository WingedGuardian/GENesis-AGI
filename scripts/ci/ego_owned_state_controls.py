"""Generated pure reducer population; synthetic evidence is never native work."""

import copy
import itertools
import uuid

from ego_owned_state import State
from ego_runtime_report import finite_number, valid_c_counters

from genesis.eval.qualification.evidence import Campaign, canonical

OUTCOMES = ("completed", "noop", "refused", "retry", "blocked", "uncertain")
COUNTS = (0, 1, 15, 16, 17, 64)


def check(condition):
    if condition is not True:
        raise AssertionError("pure state control invariant failed")


def population():
    return {"schema": "ego-owned-state-population/v1",
            "outcomes": [{"id": f"{kind}/{outcome}/{delta}", "kind": kind, "outcome": outcome, "delta": delta}
                         for kind, outcome, delta in itertools.product(("timer", "reactive"), OUTCOMES, (-100, 0, 100))],
            "bursts": [{"id": f"{count}/{mode}", "count": count, "mode": mode}
                       for count, mode in itertools.product(COUNTS, ("fifo", "redelivery", "altered")) if count or mode != "altered"],
            "excluded": [{"family": "burst", "count": 0, "mode": "altered", "reason": "no event exists to alter"}],
            "guards": ["unpaired-prepared", "unpaired-bound", "changed-manifest", "torn-tail", "second-writer", "changed-next"],
            "pacing": ["ladder-cap-reopen"]}


def spec(root, pin, sources, feed):
    return {"root": str(root), "executor": "claude-code", "model": "sonnet", "ego_source": "genesis_ego_cycle",
            "source_pin": pin, "sources": sources, "trajectory_id": "00000000-0000-0000-0000-000000000001",
            "reference_ids": [], "start": 100, "feed": feed, "responses": [],
            "pacing": {"base": 5, "factor": 2, "cap": 40, "hold": 1}}


def event(number=1):
    return {"id": f"external-{number}", "seq": number, "due": 100, "source": "fixture",
            "target": "genesis_ego_cycle", "type": "observation", "content": "same summary"}


def complete_population(inventory, rows):
    groups = {"outcomes", "bursts", "guards", "pacing"}
    if not isinstance(rows, dict) or set(rows) != groups:
        return False
    try:
        for group in groups:
            wanted = [v["id"] if isinstance(v, dict) else v for v in inventory[group]]
            found = [v["id"] for v in rows[group]]
            if len(found) != len(set(found)) or set(found) != set(wanted):
                return False
        return True
    except (KeyError, TypeError):
        return False


def prepare(state, journal, now):
    attempt = state.start(journal, state.choose(now), now, {"state": None, "effect": None})
    first = state.summary()
    state.bind(journal, {"attempt_id": attempt["id"], "trigger_id": state.spec["trajectory_id"],
                        "nonce": uuid.uuid4().hex, "config_sha256": "b" * 64, "prompt_sha256": "c" * 64,
                        "prompt_status": "0"})
    return first, state.summary()


def qualify_outcome(cell, row):
    try:
        settled = cell["outcome"] in {"completed", "noop", "refused"}
        halted = cell["outcome"] in {"blocked", "uncertain"}
        engaged = cell["outcome"] == "completed" or (cell["kind"] == "reactive" and settled)
        final = row["complete"]
        retry = final["retry"]
        retry_bound = (retry is None if cell["outcome"] != "retry" else
                       canonical(retry["event"]) == canonical(row["prepared"]["active"]["attempt"]["event"])
                       and finite_number(retry["at"]) and retry["at"] == final["wake_at"])
        return all([all(valid_c_counters(row[k]) for k in ("prepared", "bound", "complete", "reopened")),
                    row["prepared"]["cursor"] == row["bound"]["cursor"] == 0,
                    row["prepared"]["timer_done"] == row["bound"]["timer_done"] == 0,
                    row["prepared"]["active"]["stage"] == "prepared", row["bound"]["active"]["stage"] == "bound",
                    final["cursor"] == int(cell["kind"] == "reactive" and settled),
                    final["timer_reserved"] == int(cell["kind"] == "timer"),
                    final["timer_done"] == int(cell["kind"] == "timer" and settled),
                    final["level"] == (0 if halted or engaged else 1),
                    final["wake_at"] == (100 if halted else 100 + cell["delta"] + (0 if engaged else 5)),
                    final["halted"] == (cell["outcome"] if halted else None),
                    retry_bound, final["attempts"] == 1,
                    final["related"] == ["synthetic-pointer"], final["active"] is None,
                    type(row["observations"]) is int and row["observations"] == 1,
                    canonical(final) == canonical(row["reopened"])])
    except (KeyError, TypeError):
        return False


def outcome(root, pin, sources, cell):
    workload = spec(root, pin, sources, [event()] if cell["kind"] == "reactive" else [])
    with Campaign(root) as journal:
        state = State.restore(journal, workload)
        state.admit(journal, 100)
        first, bound = prepare(state, journal, 100)
        state.finish(journal, cell["outcome"], 100 + cell["delta"], {"related": ["synthetic-pointer"], "synthetic": True})
        row = {"id": cell["id"], "prepared": first, "bound": bound, "complete": state.summary(),
               "observations": sum(r["kind"] == "observation" for r in journal.lines)}
    with Campaign(root) as journal:
        row["reopened"] = State.restore(journal, workload).summary()
    row["passed"] = qualify_outcome(cell, row)
    return row


def burst(root, pin, sources, cell):
    workload = spec(root, pin, sources, [event(i) for i in range(1, cell["count"] + 1)])
    with Campaign(root) as journal:
        state = State.restore(journal, workload)
        state.admit(journal, 100)
        original = len(journal.lines)
        if cell["mode"] == "redelivery":
            state.admit(journal, 100)
            check(len(journal.lines) == original)
        if cell["mode"] == "altered":
            changed = {**event(), "content": "changed duplicate"}
            try:
                state.record(journal, "acknowledgement", {"phase": "intake", "event": changed})
            except ValueError:
                check(len(journal.lines) == original)
            else:
                raise AssertionError("changed duplicate accepted")
        chosen = []
        for _ in workload["feed"]:
            chosen.append(state.choose(100)["id"])
            prepare(state, journal, 100)
            state.finish(journal, "noop", 100, {"related": [], "synthetic": True})
        check(chosen == [e["id"] for e in workload["feed"]])
        check(state.cursor == cell["count"] and state.timer_reserved == state.timer_done == 0)
        summary = state.summary()
    with Campaign(root) as journal:
        check(canonical(State.restore(journal, workload).summary()) == canonical(summary))
    return {"id": cell["id"], "passed": True, "cursor": summary["cursor"], "events": chosen}


def guard(root, pin, sources, name):
    workload = spec(root, pin, sources, [])
    with Campaign(root) as journal:
        state = State.restore(journal, workload)
        if name.startswith("unpaired"):
            attempt = state.start(journal, state.choose(100), 100, {"state": None, "effect": None})
            if name.endswith("bound"):
                state.bind(journal, {"attempt_id": attempt["id"], "trigger_id": workload["trajectory_id"],
                                    "nonce": "a" * 32, "config_sha256": "b" * 64, "prompt_sha256": "c" * 64, "prompt_status": "0"})
        elif name == "second-writer":
            try:
                with Campaign(root):
                    raise AssertionError("second writer accepted")
            except ValueError:
                pass
        else:
            prepare(state, journal, 100)
            state.finish(journal, "noop", 100, {"related": [], "synthetic": True})
        rows = journal.lines
    if name == "torn-tail":
        with (root / "answers.jsonl").open("ab") as stream:
            stream.write(b'{"kind":')
    if name == "changed-manifest":
        workload["model"] = "different"
    if name == "changed-next":
        rows[-1]["data"]["next"]["timer_done"] = 99
    try:
        with Campaign(root) as journal:
            restored = State.replay(rows, workload) if name == "changed-next" else State.restore(journal, workload)
    except ValueError:
        check(name in {"changed-manifest", "changed-next", "torn-tail"})
    else:
        check(name.startswith("unpaired") or name == "second-writer")
        if name.startswith("unpaired"):
            check(restored.choose(10000) is restored.wait_until(10000) is None)
    return {"id": name, "passed": True}


def sweep(root, pin, sources):
    root.mkdir()
    inventory = population()
    rows = {"outcomes": [outcome(root / f"outcome-{i}", pin, sources, c) for i, c in enumerate(inventory["outcomes"])],
            "bursts": [burst(root / f"burst-{i}", pin, sources, c) for i, c in enumerate(inventory["bursts"])],
            "guards": [guard(root / f"guard-{i}", pin, sources, c) for i, c in enumerate(inventory["guards"])]}
    with Campaign(root / "pacing") as journal:
        state = State.restore(journal, spec(root / "pacing", pin, sources, []))
        for now, delay in zip((100, 105, 115, 135, 175, 215), (5, 10, 20, 40, 40, 40), strict=True):
            prepare(state, journal, now)
            state.finish(journal, "noop", now, {"related": [], "synthetic": True})
            check(state.wake_at == now + delay and state.cursor == 0)
    with Campaign(root / "pacing") as journal:
        restored = State.restore(journal, spec(root / "pacing", pin, sources, []))
        check(restored.wake_at == 255 and restored.timer_done == 6)
    rows["pacing"] = [{"id": "ladder-cap-reopen", "passed": True}]
    calibration = []
    for cell, row in zip(inventory["outcomes"], rows["outcomes"], strict=True):
        check(qualify_outcome(cell, row))
        for field in ("cursor", "wake_at", "active", "observations"):
            changed = copy.deepcopy(row)
            if field == "observations":
                changed[field] = 0
            elif field == "active":
                changed["prepared"][field] = None
            else:
                changed["complete"][field] = 999
            check(not qualify_outcome(cell, changed))
            calibration.append({"id": cell["id"], "mutant": field, "rejected": True})
    check(complete_population(inventory, rows) and all(r["passed"] for group in rows.values() for r in group))
    return {"schema": "ego-owned-state-controls/v1", "status": "pass", "inventory": inventory,
            "rows": rows, "calibration": calibration, "scope": "pure reducer with synthetic observations; no native/policy/model evidence"}
