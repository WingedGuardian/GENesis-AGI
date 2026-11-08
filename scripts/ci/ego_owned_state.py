"""Private C1 cursor/timer reducer over Campaign; no approval-state simulation."""

import copy
import math
import re
import uuid

from headlong_prompt_references import canonical_id

from genesis.eval.qualification.evidence import canonical

SCHEMA = "ego-owned-journal/v1"
FIELDS = {"root", "executor", "model", "ego_source", "source_pin", "sources", "trajectory_id",
          "reference_ids", "start", "feed", "responses", "pacing"}


def keys(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError("invalid C1 fields")


def finite(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("invalid C1 time")
    return value


def text(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("invalid C1 text")
    return value


def validate(spec):
    keys(spec, FIELDS)
    for name in ("root", "model"):
        text(spec[name])
    if spec["executor"] != "claude-code" or spec["ego_source"] != "genesis_ego_cycle":
        raise ValueError("unsupported C1 executor or ego")
    if not isinstance(spec["source_pin"], str) or not re.fullmatch(r"[0-9a-f]{40}", spec["source_pin"]):
        raise ValueError("invalid C1 source pin")
    keys(spec["sources"], {"state", "loop", "context", "bridge", "cases"})
    if any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in spec["sources"].values()):
        raise ValueError("invalid C1 source hashes")
    canonical_id(spec["trajectory_id"])
    refs = spec["reference_ids"]
    if not isinstance(refs, list) or any(canonical_id(v) != v for v in refs) or len(set(refs)) != len(refs):
        raise ValueError("invalid C1 references")
    finite(spec["start"])
    pacing = spec["pacing"]
    keys(pacing, {"base", "factor", "cap", "hold"})
    if (any(type(v) is not int or v <= 0 for v in pacing.values()) or pacing["factor"] < 2
            or pacing["cap"] < pacing["base"] or pacing["hold"] != 1):
        raise ValueError("unsupported C1 pacing; hold must be one")
    if not isinstance(spec["feed"], list) or not isinstance(spec["responses"], list):
        raise ValueError("invalid C1 workload lists")
    seen, prior = set(), -math.inf
    for index, event in enumerate(spec["feed"], 1):
        keys(event, {"id", "seq", "due", "source", "target", "type", "content"})
        for name in ("id", "source", "type", "content"):
            text(event[name])
        due = finite(event["due"])
        if (event["id"] in seen or type(event["seq"]) is not int or event["seq"] != index
                or event["target"] != spec["ego_source"] or due < prior):
            raise ValueError("invalid C1 ordered event feed")
        seen.add(event["id"])
        prior = due
    for response in spec["responses"]:
        keys(response, {"kind", "backend_port", "scenario", "fault", "fixture_header_id"})
        if (response["kind"] not in {"echo", "approved", "pending", "provider_error", "handler_error"}
                or type(response["backend_port"]) is not int or not 0 < response["backend_port"] < 65536
                or response["fault"] not in (None, "observation", "final")):
            raise ValueError("invalid C1 scripted response")
        text(response["scenario"])
        if response["fixture_header_id"] is not None:
            canonical_id(response["fixture_header_id"])
    canonical(spec)  # No nonfinite nested evidence is serializable.


class State:
    def __init__(self, spec):
        validate(spec)
        self.spec = copy.deepcopy(spec)
        self.cursor = self.timer_reserved = self.timer_done = self.level = self.attempts = 0
        self.wake_at = spec["start"]
        self.intake, self.active, self.retry, self.halted = set(), None, None, None
        self.related, self.observations = [], []

    @classmethod
    def restore(cls, journal, spec):
        if journal.torn_tail:
            raise ValueError("torn C1 journal")
        if not journal.lines:
            validate(spec)
            journal.append("manifest", schema=SCHEMA, data=spec)
        return cls.replay(journal.lines, spec)

    @classmethod
    def replay(cls, lines, spec):
        state = cls(spec)
        if not lines or lines[0].get("kind") != "manifest" or canonical(lines[0].get("data")) != canonical(spec):
            raise ValueError("C1 manifest mismatch")
        for index, row in enumerate(lines):
            keys(row, {"kind", "at", "schema", "data"})
            if row["schema"] != SCHEMA or not isinstance(row["at"], str):
                raise ValueError("invalid C1 journal schema")
            if index:
                state.apply(row["kind"], row["data"])
        return state

    def summary(self):
        return {"cursor": self.cursor, "timer_reserved": self.timer_reserved, "timer_done": self.timer_done,
                "level": self.level, "wake_at": self.wake_at, "attempts": self.attempts,
                "active": copy.deepcopy(self.active), "retry": copy.deepcopy(self.retry), "halted": self.halted,
                "intake": sorted(self.intake), "related": list(self.related)}

    def record(self, journal, kind, data):
        following = copy.deepcopy(self)
        following.apply(kind, data)  # Validate before writing or exposing a transition.
        journal.append(kind, schema=SCHEMA, data=data)
        self.__dict__.update(following.__dict__)

    def admit(self, journal, now):
        finite(now)
        for event in self.spec["feed"]:
            if event["due"] <= now and event["id"] not in self.intake:
                self.record(journal, "acknowledgement", {"phase": "intake", "event": event})

    def choose(self, now):
        finite(now)
        if self.active or self.halted:
            return None
        if self.retry:
            return copy.deepcopy(self.retry["event"]) if now >= self.retry["at"] else None
        if self.cursor < len(self.spec["feed"]):
            event = self.spec["feed"][self.cursor]
            if event["id"] in self.intake:
                return {**event, "kind": "reactive"}
        if now >= self.wake_at:
            ordinal = self.timer_reserved + 1
            return {"kind": "timer", "id": f"timer:{ordinal}", "ordinal": ordinal,
                    "content": "continuity orbit wake", "source": "ego-owned-loop", "type": "monolith-wake"}
        return None

    def wait_until(self, now):
        finite(now)
        if self.active or self.halted:
            return None
        if self.retry:
            return max(now, self.retry["at"])
        if self.choose(now):
            return now
        future = [v["due"] for v in self.spec["feed"] if v["id"] not in self.intake]
        return max(now, min([self.wake_at, *future]))

    def start(self, journal, event, now, before):
        attempt = {"id": uuid.uuid4().hex, "number": self.attempts + 1,
                   "event": event, "started_at": finite(now), "before": before}
        self.record(journal, "dispatch", {"stage": "prepared", "attempt": attempt})
        return copy.deepcopy(attempt)

    def bind(self, journal, binding):
        self.record(journal, "dispatch", {"stage": "bound", "binding": binding})

    def finish(self, journal, outcome, now, evidence):
        data = {"attempt_id": self.active["attempt"]["id"], "outcome": outcome,
                "observed_at": finite(now), "evidence": evidence}
        preview = copy.deepcopy(self)
        preview.complete(data)
        data["next"] = preview.summary()
        self.record(journal, "observation", data)

    def apply(self, kind, data):
        if not isinstance(data, dict):
            raise ValueError("invalid C1 journal payload")
        if kind == "acknowledgement":
            keys(data, {"phase", "event"})
            if data["phase"] != "intake" or canonical(data["event"]) not in [canonical(v) for v in self.spec["feed"]]:
                raise ValueError("invalid C1 intake")
            self.intake.add(data["event"]["id"])
        elif kind == "dispatch" and data.get("stage") == "prepared":
            keys(data, {"stage", "attempt"})
            a = data["attempt"]
            keys(a, {"id", "number", "event", "started_at", "before"})
            if (self.active or self.halted or not re.fullmatch(r"[0-9a-f]{32}", text(a["id"]))
                    or type(a["number"]) is not int or a["number"] != self.attempts + 1
                    or canonical(a["event"]) != canonical(self.choose(a["started_at"]))):
                raise ValueError("invalid C1 prepared transition")
            keys(a["before"], {"state", "effect"})
            self.attempts = a["number"]
            if a["event"]["kind"] == "timer":
                self.timer_reserved = max(self.timer_reserved, a["event"]["ordinal"])
            self.active = copy.deepcopy(data)
        elif kind == "dispatch" and data.get("stage") == "bound":
            keys(data, {"stage", "binding"})
            b = data["binding"]
            keys(b, {"attempt_id", "trigger_id", "nonce", "config_sha256", "prompt_sha256", "prompt_status"})
            if not self.active or self.active["stage"] != "prepared" or b["attempt_id"] != self.active["attempt"]["id"]:
                raise ValueError("invalid C1 bound transition")
            canonical_id(b["trigger_id"])
            for name, length in (("nonce", 32), ("config_sha256", 64), ("prompt_sha256", 64)):
                if not re.fullmatch(r"[0-9a-f]{" + str(length) + "}", text(b[name])):
                    raise ValueError("invalid C1 binding digest")
            text(b["prompt_status"])
            self.active.update(stage="bound", binding=copy.deepcopy(b))
        elif kind == "observation":
            keys(data, {"attempt_id", "outcome", "observed_at", "evidence", "next"})
            self.complete(data)
            if canonical(self.summary()) != canonical(data["next"]):
                raise ValueError("invalid C1 committed transition")
            self.observations.append(copy.deepcopy(data))
        else:
            raise ValueError("invalid C1 journal transition")

    def complete(self, data):
        if (not self.active or self.active["stage"] != "bound"
                or data["attempt_id"] != self.active["attempt"]["id"]
                or data["outcome"] not in {"completed", "noop", "refused", "retry", "blocked", "uncertain"}):
            raise ValueError("invalid C1 observation transition")
        finite(data["observed_at"])
        related = data["evidence"]["related"]
        if not isinstance(related, list) or any(not isinstance(v, str) or not v for v in related):
            raise ValueError("invalid C1 related-memory pointers")
        self.related = list(related)
        event, outcome = self.active["attempt"]["event"], data["outcome"]
        if outcome in {"uncertain", "blocked"}:
            self.halted = outcome
        else:
            engaged = outcome == "completed" or (event["kind"] == "reactive" and outcome != "retry")
            self.level = 0 if engaged else self.level + 1
            p = self.spec["pacing"]
            delay = 0 if self.level == 0 else p["base"]
            for _ in range(max(0, self.level - 1)):
                delay = min(p["cap"], delay * p["factor"])
                if delay == p["cap"]:
                    break
            self.wake_at = data["observed_at"] + delay
            self.retry = {"event": copy.deepcopy(event), "at": self.wake_at} if outcome == "retry" else None
            if outcome != "retry":
                if event["kind"] == "reactive":
                    self.cursor += 1
                else:
                    self.timer_done = event["ordinal"]
        self.active = None
