"""Read original kernel ownership, independently from completion and manager state.

The caller supplies a retained registration and a real owned-wait result. These
functions neither open the queue nor stop/start anything. Proc injection is a
Python fixture seam only; production callers use the actual /proc.
"""
from __future__ import annotations

import math
import re
import time
import uuid
from copy import deepcopy
from pathlib import Path, PurePosixPath


def _boot(proc: Path) -> str:
    return str(uuid.UUID((proc / "sys/kernel/random/boot_id").read_text().strip()))


def _root(baseline, proof, proc):
    _, root, version = proof._ADMISSION["resolve_cgroup"](proc / "self/cgroup", proc / "self/mountinfo")
    if (version != 2 or proof._identity(root) != baseline["root"]
            or proof.mount_digest(proc / "self/mountinfo") != baseline["mounts_sha256"]):
        raise ValueError("original cgroup root or mount mapping changed")


def _population(path, proof):
    fields = {}
    for row in path.read_text().splitlines():
        name, raw = row.split()
        if name in fields:
            raise ValueError("duplicate cgroup event")
        fields[name] = proof._NUMBER(raw, name)
    if fields.get("populated") not in (0, 1):
        raise ValueError("recursive population unavailable")
    return fields["populated"]


def cleanup_observation(registration, registration_sha256, proof, *, owned_wait, proc=Path("/proc")):
    """Fresh original-subtree proof, never an exited-foreground inference.

    Identity fields retain the original reporter as their subject, including its
    recorded membership. Counters and scope policy are read now; this does not
    assert that the old reporter is alive or is the current observing process.
    """
    baseline = registration["facts"]
    if type(owned_wait) is not bool or not proof._facts(baseline):
        raise ValueError("invalid original registration or owned wait")
    old_boot, current_boot = baseline["reporter"]["boot_id"], _boot(proc)
    result = {"registration_sha256": registration_sha256}
    if current_boot != old_boot:
        return {**result, "state": "rebooted", "old_boot_id": old_boot,
                "new_boot_id": current_boot, "observed_ns": time.monotonic_ns()}
    _root(baseline, proof, proc)
    leaf, root = Path(baseline["leaf"]["path"]), Path(baseline["root"]["path"])
    if (root not in leaf.parents or leaf.name != registration["unit"] + ".scope"
            or [item["identity"]["path"] for item in baseline["ancestors"]]
            != [str(path) for path in leaf.parents][:list(leaf.parents).index(root) + 1]):
        raise ValueError("original scope ancestry changed")
    try:
        identity = proof._identity(leaf)
    except FileNotFoundError:
        _root(baseline, proof, proc)
        if _boot(proc) != current_boot:
            raise ValueError("boot changed during cleanup observation") from None
        return {**result, "state": "removed", "root": baseline["root"],
                "mounts_sha256": baseline["mounts_sha256"], "boot_id": current_boot,
                "absent": True, "observed_ns": time.monotonic_ns()}
    if identity != baseline["leaf"]:
        raise ValueError("original scope inode changed")
    # Observe emptiness before counters so the last owned descendant's OOM is
    # included. A populated sample stays unresolved even if tasks then exit.
    populated = _population(leaf / "cgroup.events", proof)
    facts = deepcopy(baseline)
    facts["memory_max"] = proof._NUMBER((leaf / "memory.max").read_text(), "memory.max")
    facts["swap_max"] = proof._NUMBER((leaf / "memory.swap.max").read_text(), "memory.swap.max")
    facts["events"] = proof._events(leaf / "memory.events")
    facts["local_events"] = proof._events(leaf / "memory.events.local")
    for item in facts["ancestors"]:
        path = Path(item["identity"]["path"])
        if proof._identity(path) != item["identity"]:
            raise ValueError("original ancestor inode changed")
        try:
            item.update(events=proof._events(path / "memory.events"), error=None)
        except (OSError, ValueError):
            item.update(events=None, error="ancestor_events_unavailable")
    _root(baseline, proof, proc)
    if proof._identity(leaf) != identity or _boot(proc) != current_boot:
        raise ValueError("original ownership changed during cleanup observation")
    proof._complete(baseline, facts)
    return {**result, "state": "quiescent" if populated == 0 and owned_wait else "unresolved",
            "facts": facts, "populated": populated, "owned_wait": owned_wait,
            "observed_ns": time.monotonic_ns()}


class ScopeManager:
    """Read existing identities using the shared typed bus primitives; no actions."""

    def __init__(self, bus, property_value, *, deadline, clock=time.monotonic):
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("invalid manager observation deadline")
        self.bus, self.property = bus, property_value
        self.deadline, self.clock = deadline, clock

    def _call(self, path, interface, method, *args):
        remaining = self.deadline - self.clock()
        if not math.isfinite(remaining) or remaining <= 0:
            raise ValueError("manager observation deadline reached")
        value = self.bus(path, interface, method, *args, timeout=min(30, remaining))
        if self.clock() >= self.deadline:
            raise ValueError("manager reply arrived after observation deadline")
        if not isinstance(value, dict) or set(value) != {"type", "data"}:
            raise ValueError("invalid typed manager observation")
        return value

    def _object(self, method, *args):
        value = self._call("/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", method, *args)
        data = value["data"]
        if (value["type"] != "o" or not isinstance(data, list) or len(data) != 1
                or not isinstance(data[0], str)
                or re.fullmatch(r"/org/freedesktop/systemd1/unit/[A-Za-z0-9_]+", data[0]) is None):
            raise ValueError("invalid existing unit object")
        return data[0]

    def _properties(self, path, kind):
        value = self._call(path, "org.freedesktop.DBus.Properties", "GetAll", "s",
                           "org.freedesktop.systemd1." + kind)
        if (value["type"] != "a{sv}" or not isinstance(value["data"], list)
                or len(value["data"]) != 1 or not isinstance(value["data"][0], dict)):
            raise ValueError("invalid existing unit properties")
        return value["data"][0]

    def _identity(self, properties):
        name = self.property(properties, "Id", "s")
        invocation = self.property(properties, "InvocationID", "ay")
        if (self.property(properties, "LoadState", "s") != "loaded"
                or not isinstance(invocation, list) or len(invocation) != 16
                or any(type(value) is not int or not 0 <= value <= 255 for value in invocation)):
            raise ValueError("existing unit invocation unavailable")
        return name, bytes(invocation).hex()

    def _snapshot(self, method, args, kind=None):
        path = self._object(method, *args)
        first = self._identity(self._properties(path, "Unit"))
        if kind is None:
            suffix = first[0].rsplit(".", 1)[-1]
            if suffix not in ("scope", "service"):
                raise ValueError("unsupported owner unit type")
            kind = suffix.capitalize()
        properties = self._properties(path, kind)
        last = self._identity(self._properties(path, "Unit"))
        if first != last or path != self._object(method, *args):
            raise ValueError("manager unit identity changed during observation")
        return first, properties, path

    def owner_slice(self, pid):
        if type(pid) is not int or not 0 < pid < 2**32:
            raise ValueError("invalid actual owner pid")
        _, properties, _ = self._snapshot("GetUnitByPID", ("u", str(pid)))
        result = self.property(properties, "Slice", "s")
        if re.fullmatch(r"[A-Za-z0-9_.\\-]+\.slice", result) is None:
            raise ValueError("actual owner slice unavailable")
        return result

    def registration(self, unit, facts, *, wall_seconds):
        if type(wall_seconds) is not int or not 0 < wall_seconds <= 2**63 - 1:
            raise ValueError("invalid effective mode wall cap")
        pid = facts["reporter"]["pid"]
        if type(pid) is not int or not 0 < pid < 2**32:
            raise ValueError("invalid actual reporter pid")
        reporter_unit = self._object("GetUnitByPID", "u", str(pid))
        identity, properties, path = self._snapshot("GetUnit", ("s", unit + ".scope"), "Scope")
        if reporter_unit != path or self._object("GetUnitByPID", "u", str(pid)) != path:
            raise ValueError("named scope does not own the actual reporter")
        leaf = PurePosixPath(facts["leaf"]["path"])
        group = self.property(properties, "ControlGroup", "s")
        target = PurePosixPath(group)
        stop = self.property(properties, "TimeoutStopUSec", "t")
        if (identity[0] != unit + ".scope" or leaf.name != identity[0]
                or self.property(properties, "Slice", "s") != leaf.parent.name
                or not target.is_absolute() or ".." in target.parts or "\0" in group
                or target.name != leaf.name or target.parent.name != leaf.parent.name
                or not 0 < stop <= 2**63 - 1 or 2 * stop > wall_seconds * 1_000_000
                or self.property(properties, "MemoryMax", "t") != 8 * 1024**3
                or self.property(properties, "MemorySwapMax", "t") != 0
                or self.property(properties, "KillMode", "s") != "control-group"
                or self.property(properties, "SendSIGKILL", "b") is not True):
            raise ValueError("scope identity or supported stop policy mismatch")
        return {"unit": unit, "invocation_id": identity[1], "facts": deepcopy(facts), "stop_usec": stop}
