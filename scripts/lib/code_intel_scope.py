"""Read original kernel ownership, independently from completion and manager state.

The caller supplies a retained registration and a real owned-wait result. These
functions neither open the queue nor stop/start anything. Proc injection is a
Python fixture seam only; production callers use the actual /proc.
"""
from __future__ import annotations

import math
import re
import sys
import time
import uuid
from copy import deepcopy
from pathlib import Path, PurePosixPath


def _boot(proc: Path) -> str:
    return str(uuid.UUID((proc / "sys/kernel/random/boot_id").read_text().strip()))


def owner_ended(owner, proof, *, proc=Path('/proc')):
    """Positive old-lifetime end, never an arbitrary process-read exception.

    proc_pid_stat(5): state3, starttime22. A terminal leader can retain live
    threads, so a matching Z/X/x is not a proved-ended process either.
    https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html
    """
    if (not isinstance(owner, dict) or set(owner) != {'pid', 'start_ticks', 'boot_id'}
            or type(owner['pid']) is not int or not 0 < owner['pid'] < 2**32
            or type(owner['start_ticks']) is not int or not 0 < owner['start_ticks'] <= 2**63 - 1
            or not isinstance(owner['boot_id'], str)
            or str(uuid.UUID(owner['boot_id'])) != owner['boot_id']):
        raise ValueError('invalid original process identity')
    boot = _boot(proc)
    if boot != owner['boot_id']:
        return True
    directory = proc / str(owner['pid'])
    try:
        identity = proof['_process_identity'](directory, owner['pid'])
    except FileNotFoundError:
        try:
            directory.stat()
        except FileNotFoundError:
            ended = True  # Entire original PID entry is absent, not just stat.
        else:
            raise ValueError('original process stat unavailable') from None
    else:
        if identity[1] <= 0 or identity[3] not in ('R', 'S', 'D', 'Z', 'T', 't', 'X', 'x', 'K', 'W', 'P', 'I'):
            raise ValueError('original process state or start unavailable')
        ended = identity[1] != owner['start_ticks']
    if _boot(proc) != boot:
        raise ValueError('boot changed during owner-lifetime observation')
    return ended


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
        # Absence below a matching original ancestor proves removal; absence
        # through a replaced or unreadable ancestor proves only lost visibility.
        for item in reversed(baseline["ancestors"]):
            try:
                ancestor = proof._identity(Path(item["identity"]["path"]))
            except FileNotFoundError:
                break
            if ancestor != item["identity"]:
                raise ValueError("original ancestor inode changed") from None
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

    def __init__(self, bus, property_value, *, deadline, clock=time.monotonic, require_runtime_limit=False):
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("invalid manager observation deadline")
        if type(require_runtime_limit) is not bool:
            raise ValueError("invalid runtime policy requirement")
        self.bus, self.property = bus, property_value
        self.deadline, self.clock = deadline, clock
        self.require_runtime_limit = require_runtime_limit

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

    def require_slice_capacity(self, name, observe):
        """Observe a loaded active destination; never load or start a slice."""
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_.\\-]+\.slice", name) is None:
            raise ValueError("invalid destination slice")
        path = self._object('GetUnit', 's', name)

        def identity():
            properties = self._properties(path, 'Unit')
            result = self._identity(properties)
            job = self.property(properties, 'Job', '(uo)')
            if (result[0] != name or self.property(properties, 'Following', 's')
                    or self.property(properties, 'ActiveState', 's') != 'active'
                    or self.property(properties, 'SubState', 's') != 'active'
                    or not isinstance(job, list) or len(job) != 2
                    or type(job[0]) is not int or job != [0, '/']):
                raise ValueError('destination slice is not the stable active unit')
            return result

        first = identity()
        group = self.property(self._properties(path, 'Slice'), 'ControlGroup', 's')
        target = PurePosixPath(group)
        if (not target.is_absolute() or '\0' in group or '..' in target.parts or target.name != name):
            raise ValueError('destination slice cgroup unavailable')
        observe(group)
        current_group = self.property(self._properties(path, 'Slice'), 'ControlGroup', 's')
        if identity() != first or current_group != group or self._object('GetUnit', 's', name) != path:
            raise ValueError('destination slice changed during capacity observation')

    def require_no_batch(self, cgroup_empty):
        """Observe known managed scopes before reservation; this is no exclusion.

        Caller holds the canonical global/repository locks and has not created
        this attempt's scope. See systemd v255 reply_unit_info/list_units_filtered
        for the complete tuple and unfiltered empty-state selection.
        """
        value = self._call('/org/freedesktop/systemd1', 'org.freedesktop.systemd1.Manager',
                           'ListUnitsByPatterns', 'asas', '0', '1', 'code-intel-*.scope')
        data = value['data']
        if (value['type'] != 'a(ssssssouso)' or not isinstance(data, list)
                or len(data) != 1 or not isinstance(data[0], list) or not callable(cgroup_empty)):
            raise ValueError('global batch census unavailable')
        names, objects = set(), set()
        for row in data[0]:
            if (not isinstance(row, list) or len(row) != 10
                    or any(not isinstance(field, str) or '\0' in field
                           for i, field in enumerate(row) if i != 7)
                    or type(row[7]) is not int or row[7] != 0 or row[8:] != ['', '/']
                    or row[2] != 'loaded' or row[5]
                    or (row[3], row[4]) not in (('inactive', 'dead'), ('failed', 'failed'))
                    or re.fullmatch(r'code-intel-[a-f0-9]{12}-(?:cbm|gitnexus)-[0-9]+\.scope', row[0]) is None
                    or re.fullmatch(r'/org/freedesktop/systemd1/unit/[A-Za-z0-9_]+', row[6]) is None
                    or row[0] in names or row[6] in objects):
                raise ValueError('global batch census contains an unproven lifetime')
            names.add(row[0])
            objects.add(row[6])
        for row in data[0]:
            path = self._object('GetUnit', 's', row[0])
            if path != row[6]:
                raise ValueError('global batch object changed')
            first = self._terminal_unit(self._properties(path, 'Unit'), row)
            group = self.property(self._properties(path, 'Scope'), 'ControlGroup', 's')
            target = PurePosixPath(group)
            if group and (not target.is_absolute() or '..' in target.parts or target.name != row[0]):
                raise ValueError('global batch cgroup identity unavailable')
            cgroup_empty(group)  # Recursive population/removal; failed alone can retain tasks.
            current_group = self.property(self._properties(path, 'Scope'), 'ControlGroup', 's')
            last = self._terminal_unit(self._properties(path, 'Unit'), row)
            if first != last or current_group != group or self._object('GetUnit', 's', row[0]) != path:
                raise ValueError('global batch identity changed during absence proof')

    def _terminal_unit(self, properties, row):
        identity = self._identity(properties)
        job = self.property(properties, 'Job', '(uo)')
        if (identity[0] != row[0] or self.property(properties, 'Following', 's')
                or self.property(properties, 'ActiveState', 's') != row[3]
                or self.property(properties, 'SubState', 's') != row[4]
                or not isinstance(job, list) or len(job) != 2
                or type(job[0]) is not int or job != [0, '/']):
            raise ValueError('global batch is no longer the terminal unqueued unit')
        return identity

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
        return self._registration(unit, facts, identity, properties, wall_seconds)

    def original(self, registration, facts, *, object_path, wall_seconds, proof):
        """Observe an original subtree after foreground exit; grant no action."""
        if (type(wall_seconds) is not int or not 0 < wall_seconds <= 2**63 - 1
                or not proof._facts(registration["facts"]) or not proof._facts(facts)):
            raise ValueError("invalid original scope facts or wall cap")
        proof._complete(registration["facts"], facts)
        unit = registration["unit"]
        identity, properties, path = self._snapshot("GetUnit", ("s", unit + ".scope"), "Scope")
        current = self._registration(unit, facts, identity, properties, wall_seconds)
        if (path != object_path or current["invocation_id"] != registration["invocation_id"]
                or current["stop_usec"] != registration["stop_usec"]):
            raise ValueError("original scope invocation, object or stop policy changed")
        return current

    def _registration(self, unit, facts, identity, properties, wall_seconds):
        leaf = PurePosixPath(facts["leaf"]["path"])
        group = self.property(properties, "ControlGroup", "s")
        target = PurePosixPath(group)
        native_id = self.property(properties, "ControlGroupId", "t")
        stop = self.property(properties, "TimeoutStopUSec", "t")
        if (identity[0] != unit + ".scope" or leaf.name != identity[0]
                # kernfs exposes the full native ID as st_ino on 64-bit Linux.
                # An enclosing GetUnitByPID result or matching names is weaker.
                or sys.maxsize <= 2**32 or native_id == 0
                or native_id != facts["leaf"]["inode"]
                or facts["leaf"]["device"] != facts["root"]["device"]
                or self.property(properties, "Slice", "s") != leaf.parent.name
                or not target.is_absolute() or ".." in target.parts or "\0" in group
                or target.name != leaf.name or target.parent.name != leaf.parent.name
                or not 0 < stop <= 2**63 - 1 or 2 * stop > wall_seconds * 1_000_000
                or self.property(properties, "MemoryMax", "t") != 8 * 1024**3
                or self.property(properties, "MemorySwapMax", "t") != 0
                or self.property(properties, "KillMode", "s") != "control-group"
                or self.property(properties, "SendSIGKILL", "b") is not True):
            raise ValueError("scope identity or supported stop policy mismatch")
        if self.require_runtime_limit:
            runtime = self.property(properties, "RuntimeMaxUSec", "t")
            if (runtime == 2**64 - 1 or runtime != wall_seconds * 1_000_000
                    or self.property(properties, "RuntimeRandomizedExtraUSec", "t") != 0
                    or self.property(properties, "FinalKillSignal", "i") != 9):
                raise ValueError("scope effective runtime policy mismatch")
        return {"unit": unit, "invocation_id": identity[1], "facts": deepcopy(facts), "stop_usec": stop}
