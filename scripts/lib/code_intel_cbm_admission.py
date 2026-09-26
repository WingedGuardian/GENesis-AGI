"""Refuse a Codebase batch unless its *running* scope has room to finish.

This runs in the transient scope immediately before the indexer. It has no
authority to change limits; all ambiguity is a refusal. The caller's cgroup
is deliberately irrelevant because systemd-run may put the scope elsewhere.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

MAX_NUMBER = 10**18 - 1


class AdmissionRefused(ValueError):
    """Capacity or scope identity could not be proved."""


def number(value: str, label: str, *, maximum: int = MAX_NUMBER) -> int:
    value = value.strip()
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)", value) or len(value) > 18:
        raise AdmissionRefused(f"invalid {label}")
    result = int(value)
    if result > maximum:
        raise AdmissionRefused(f"oversized {label}")
    return result


def read_number(path: Path) -> int:
    try:
        return number(path.read_text(), str(path))
    except OSError as exc:
        raise AdmissionRefused(f"cannot read {path}") from exc


def _mount_path(raw: str) -> Path:
    # mountinfo escapes spaces, tabs, newlines and backslashes as octal bytes.
    return Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), raw))


def resolve_cgroup(self_path: Path, mountinfo_path: Path) -> tuple[Path, Path, int]:
    """Return the leaf, broadest visible mount root, and cgroup version."""
    try:
        memberships = self_path.read_text().splitlines()
        mounts = mountinfo_path.read_text().splitlines()
    except OSError as exc:
        raise AdmissionRefused("cannot read cgroup membership or mounts") from exc
    v2 = next((row[3:] for row in memberships if row.startswith("0::")), None)
    v1 = next(
        (row.split(":", 2)[2] for row in memberships if len(row.split(":", 2)) == 3
         and "memory" in row.split(":", 2)[1].split(",")),
        None,
    )
    version, membership = (1, v1) if v1 is not None else (2, v2)
    if membership is None or not membership.startswith("/") or ".." in Path(membership).parts:
        raise AdmissionRefused("no valid memory cgroup membership")

    candidates: list[tuple[int, int, Path, Path]] = []
    for row in mounts:
        if " - " not in row:
            continue
        left, right = row.split(" - ", 1)
        parts, fs = left.split(), right.split()
        if len(parts) < 5 or len(fs) < 3:
            continue
        if version == 2 and fs[0] != "cgroup2":
            continue
        if version == 1 and (fs[0] != "cgroup" or "memory" not in fs[2].split(",")):
            continue
        root, mount = _mount_path(parts[3]), _mount_path(parts[4])
        if not root.is_absolute() or not mount.is_absolute():
            continue
        member = Path(membership)
        if member == root or root in member.parents:
            rel = member.relative_to(root)
            mapping_rank = 0  # proven host-relative membership
        elif member == Path("/") or root != Path("/"):
            # Cgroup namespace membership is relative to its visible mount root.
            rel = member.relative_to(Path("/"))
            mapping_rank = 1  # weaker; use only when no host mapping exists
        else:
            continue
        leaf = mount / rel
        if leaf.is_dir():
            candidates.append((mapping_rank, len(root.parts), leaf, mount))
    if not candidates:
        raise AdmissionRefused("cannot map memory cgroup to a visible mount")
    # A deeper subtree mount may hide an applicable ancestor. Prefer the
    # broadest visible root and refuse conflicting aliases of that root.
    best_rank = min(item[0] for item in candidates)
    ranked = [item for item in candidates if item[0] == best_rank]
    best_depth = min(item[1] for item in ranked)
    best = [item for item in ranked if item[1] == best_depth]
    leaf, mount = best[0][2:]
    try:
        if any(not os.path.samefile(leaf, other[2]) for other in best[1:]):
            raise AdmissionRefused("ambiguous memory cgroup mounts")
    except OSError as exc:
        raise AdmissionRefused("cannot verify memory cgroup mount aliases") from exc
    return leaf, mount, version


def _working_charge(current: int, stat_path: Path, reserve: int, version: int) -> int:
    """Discount only a validated clean file LRU; malformed stats use raw charge."""
    try:
        fields = dict(line.split(None, 1) for line in stat_path.read_text().splitlines()
                      if len(line.split(None, 1)) == 2)
        prefix = "total_" if version == 1 else ""
        inactive = number(fields[prefix + "inactive_file"], "inactive_file")
        active = number(fields[prefix + "active_file"], "active_file")
        dirty = number(fields[prefix + ("dirty" if version == 1 else "file_dirty")], "dirty")
        writeback = number(
            fields[prefix + ("writeback" if version == 1 else "file_writeback")],
            "writeback",
        )
        clean = inactive + active - dirty - writeback
        if clean <= reserve or clean > current or dirty + writeback > inactive + active:
            return current
        return current - (clean - reserve)
    except (OSError, KeyError, AdmissionRefused, ValueError):
        return current


def _host_available(meminfo_path: Path) -> int:
    try:
        values = dict(line.split(":", 1) for line in meminfo_path.read_text().splitlines()
                      if ":" in line)
        total = number(values["MemTotal"].strip().split()[0], "MemTotal", maximum=10**14 - 1)
        available = number(
            values["MemAvailable"].strip().split()[0], "MemAvailable", maximum=10**14 - 1
        )
    except (OSError, KeyError, IndexError) as exc:
        raise AdmissionRefused("cannot read host available memory") from exc
    if available > total:
        raise AdmissionRefused("host available memory exceeds total")
    return available * 1024


def assess(
    cap: int,
    reserve: int,
    unit: str,
    *,
    cache_reserve: int = 2 * 1024**3,
    self_path: Path = Path("/proc/self/cgroup"),
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    meminfo_path: Path = Path("/proc/meminfo"),
) -> None:
    if not re.fullmatch(r"code-intel-[a-f0-9]{12}-cbm-[0-9]+", unit):
        raise AdmissionRefused("invalid expected scope name")
    leaf, root, version = resolve_cgroup(self_path, mountinfo_path)
    if leaf.name != unit + ".scope" or leaf == root:
        raise AdmissionRefused("worker is outside its expected scope")
    limit_file = "memory.max" if version == 2 else "memory.limit_in_bytes"
    # systemd may align a fractional MemoryMax downward, while the kernel may
    # round an unaligned write upward on readback. Accept only the adjacent
    # page boundaries and budget for the *effective* limit actually enforced.
    page_size = os.sysconf("SC_PAGE_SIZE")
    floor_cap = (cap // page_size) * page_size
    rounded_cap = ((cap + page_size - 1) // page_size) * page_size
    effective_cap = read_number(leaf / limit_file)
    if effective_cap <= 0 or effective_cap < floor_cap or effective_cap > rounded_cap:
        raise AdmissionRefused("worker scope memory limit differs from requested cap")
    if version == 2:
        if read_number(leaf / "memory.swap.max") != 0:
            raise AdmissionRefused("worker scope permits swap")
    elif read_number(leaf / "memory.memsw.limit_in_bytes") != effective_cap:
        raise AdmissionRefused("worker scope permits swap")

    parent = leaf.parent
    while root == parent or root in parent.parents:
        limit_path = parent / limit_file
        try:
            raw_limit = limit_path.read_text().strip()
        except OSError as exc:
            # Cgroup v2's filesystem root has no memory.max by definition.
            if version == 2 and parent == root and not limit_path.exists():
                raw_limit = "max"
            else:
                raise AdmissionRefused(f"cannot read {limit_path}") from exc
        if version == 1 and re.fullmatch(r"[0-9]{1,20}", raw_limit) \
                and int(raw_limit) >= 4503599627370496:
            raw_limit = "-1"  # v1's conventional unlimited sentinel
        if raw_limit not in ("max", "-1"):
            limit = number(raw_limit, str(limit_path))
            current = read_number(parent / ("memory.current" if version == 2
                                            else "memory.usage_in_bytes"))
            charge = _working_charge(current, parent / "memory.stat", cache_reserve, version)
            if limit - charge - reserve < effective_cap:
                raise AdmissionRefused(f"insufficient headroom in {parent}")
        if parent == root:
            break
        parent = parent.parent
    if _host_available(meminfo_path) - reserve < effective_cap:
        raise AdmissionRefused("insufficient host available memory")


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print("code-intel: admission requires cap, reserve, scope and cache reserve", file=sys.stderr)
        return 125
    try:
        cap = number(argv[1], "requested cap")
        reserve = number(argv[2], "sibling reserve")
        cache_reserve = number(argv[4], "cache reserve")
        assess(
            cap, reserve, argv[3],
            cache_reserve=cache_reserve,
        )
    except AdmissionRefused as exc:
        print(f"code-intel: SKIP cbm: {exc}", file=sys.stderr)
        return 125
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
