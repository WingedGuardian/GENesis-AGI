#!/usr/bin/env python3
"""Finite process-private source capture; supplies identity, never launch authority.

Compile and hash the same buffers (transcript_analytics.scrub's existing pattern).
The fixed isolated bootstrap is a declared trust root. A sealed capsule preserves
bytes across handoff; its seals do not authenticate its producer. Reservation and
supervised recovery remain the queue's responsibility. No provider runs here.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

SELF = "scripts/lib/code_intel_source.py"
# Dependency order and names are closed. Extending the queued runtime requires
# explicitly updating this inventory, assembly and its drift tests together.
MODULES = (
    ("units", "managed_units", "scripts/lib/managed_units.py"),
    ("admission", "code_intel_cbm_admission", "scripts/lib/code_intel_cbm_admission.py"),
    ("systemd", "_genesis_util_systemd", "src/genesis/util/systemd.py"),
    ("worker", "code_intel_cbm_worker", "scripts/lib/code_intel_cbm_worker.py"),
    ("unit", "codebase_managed_unit", "scripts/lib/codebase_managed_unit.py"),
    ("proof", "_genesis_code_intel_execution", "scripts/lib/code_intel_execution.py"),
    ("legacy", "_genesis_index_marker_legacy", "scripts/lib/index_marker_legacy.py"),
    ("legs", "_genesis_index_marker_legs", "scripts/lib/index_marker_legs.py"),
    ("native", "_genesis_index_marker_execution", "scripts/lib/index_marker_execution.py"),
    ("cli", "_genesis_index_marker_cli", "scripts/lib/index_marker_cli.py"),
    ("queue", "_genesis_index_marker", "scripts/lib/index_marker.py"),
    ("managed", "_genesis_codebase_managed", "scripts/codebase_managed.py"),
)
SHELL = (
    "scripts/lib/code_intel_index.sh",
    "scripts/lib/gitnexus_version.sh",
    "scripts/lib/proc_pressure.sh",
)
FILE_LIMIT = 1024 * 1024
CAPSULE_LIMIT = 4 * FILE_LIMIT
SEALS = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
# Read entry once BEFORE compiling its substantive code, register for dataclasses,
# and retain its original origin. -I -S excludes caller imports and site hooks.
BOOTSTRAP = """import importlib.util,pathlib,sys
p=pathlib.Path(sys.argv[1]).resolve(strict=True)
with p.open('rb') as f: raw=f.read(1048577)
if len(raw)>1048576: raise ValueError('oversized source entry')
s=importlib.util.spec_from_file_location('_genesis_code_intel_source',p)
m=importlib.util.module_from_spec(s);sys.modules[s.name]=m
m.__dict__['_ENTRY_BYTES']=raw
exec(compile(raw,str(p),'exec',dont_inherit=True),m.__dict__)
raise SystemExit(m.main(sys.argv[2:]))
"""

# Only an inherited private capsule enters this fixed describe role. Seals
# preserve bytes, not producer trust. Helpers are assembled after full validation.
RESUME_BOOTSTRAP = """import base64,fcntl,hashlib,importlib.util,json,os,pathlib,sys
if fcntl.fcntl(0,fcntl.F_GET_SEALS)&15!=15: raise ValueError('unsealed source')
n=os.fstat(0).st_size
if not 0<n<=4194304: raise ValueError('invalid source size')
payload=os.pread(0,n,0)
if len(payload)!=n or hashlib.sha256(payload).hexdigest()!=sys.argv[1]:
 raise ValueError('source handoff differs from captured command')
v=json.loads(payload);key='scripts/lib/code_intel_source.py'
raw=base64.b64decode(v['buffers'][key],validate=True)
if len(raw)>1048576 or hashlib.sha256(raw).hexdigest()!=v['files'][key]:
 raise ValueError('invalid source entry')
p=pathlib.Path(v['root'])/key
s=importlib.util.spec_from_file_location('_genesis_code_intel_source',p)
m=importlib.util.module_from_spec(s);sys.modules[s.name]=m
m.__dict__.update(_ENTRY_BYTES=raw,_CAPSULE_FD=0,_CAPSULE_SHA256=sys.argv[1])
exec(compile(raw,str(p),'exec',dont_inherit=True),m.__dict__)
raise SystemExit(m.main(['describe',v['profile']]))
"""


def _inventory(profile):
    if profile not in ("entry", "runner"):
        raise ValueError("unsupported source profile")
    return (
        SELF,
        *(row[2] for row in MODULES),
        *SHELL,
        *(("scripts/code_intel_runner.sh",) if profile == "runner" else ()),
    )


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("source must be regular")
        raw = stream.read(FILE_LIMIT + 1)
    if len(raw) > FILE_LIMIT:
        raise ValueError("oversized source file")
    return raw


def _git(root, *args, data=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0")
    return subprocess.run(
        ["git", "-C", str(root), *args],
        input=data,
        capture_output=True,
        env=env,
        check=True,
        timeout=30,
    ).stdout


def _checksums(buffers):
    return {name: hashlib.sha256(raw).hexdigest() for name, raw in buffers.items()}


def capture(root, profile, entry_bytes):
    """Bind each captured buffer to one immutable Git tree, not endpoint equality."""
    if entry_bytes != globals().get("_ENTRY_BYTES") or not isinstance(entry_bytes, bytes):
        raise ValueError("source entry must be captured before compilation")
    root = Path(root).resolve(strict=True)
    if Path(__file__) != root / SELF:
        raise ValueError("source origin differs from checkout")
    names = _inventory(profile)
    commit = _git(root, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    if re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", commit) is None:
        raise ValueError("invalid source commit")
    buffers = {name: entry_bytes if name == SELF else _read(root / name) for name in names}
    records = _git(root, "ls-tree", "-rz", "--full-tree", commit, "--", *names).split(b"\0")
    tree = {}
    for row in filter(None, records):
        metadata, name = row.split(b"\t", 1)
        mode, kind, oid = metadata.split()
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise ValueError("unsupported source tree entry")
        tree[name.decode()] = oid.strip()
    if set(tree) != set(buffers):
        raise ValueError("source inventory differs from commit")
    for name, raw in buffers.items():
        # No -w: read-only object calculation; no filters touch captured bytes.
        # https://git-scm.com/docs/git-hash-object
        if _git(root, "hash-object", "--stdin", "--no-filters", data=raw).strip() != tree[name]:
            raise ValueError("source bytes differ from commit: " + name)
    return Snapshot(root, profile, commit, MappingProxyType(buffers))


@dataclass(frozen=True)
class Snapshot:
    root: Path
    profile: str
    commit: str
    buffers: object

    @property
    def files(self):
        return _checksums(self.buffers)

    def current_matches(self):
        """Pre-intent drift observation only; never replace retained identities."""
        try:
            return _git(self.root, "rev-parse", "HEAD").decode().strip() == self.commit and all(
                hashlib.sha256(_read(self.root / name)).hexdigest() == digest
                for name, digest in self.files.items()
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    def assemble(self):
        """Compile the closed local closure without pyc reads or foreign caches."""
        if any(alias in sys.modules for _, alias, _ in MODULES):
            raise ValueError("source module already loaded outside this assembly")
        loaded, inserted = {}, {}
        old_path = sys.path[:]
        try:
            for key, alias, name in MODULES:
                origin = self.root / name
                spec = importlib.util.spec_from_file_location(alias, origin)
                module = importlib.util.module_from_spec(spec)
                if key in ("worker", "proof"):
                    module.__dict__["_CAPTURED_ADMISSION"] = loaded["admission"].__dict__
                if key == "worker":
                    module.__dict__["_CAPTURED_MANAGED"] = lambda: loaded["managed"].__dict__
                if key == "unit":
                    module.__dict__["_CAPTURED_SYSTEMCTL_ENV"] = loaded["systemd"].systemctl_env
                if key == "managed":
                    module.__dict__["_CAPTURED_SCRIPT"] = origin
                sys.modules[alias] = inserted[alias] = module
                exec(  # noqa: S102 - fixed inventory, trusted captured Git bytes
                    compile(self.buffers[name], str(origin), "exec", dont_inherit=True),
                    module.__dict__,
                )
                loaded[key] = module
            return MappingProxyType(loaded)
        except BaseException:
            for alias, module in inserted.items():
                if sys.modules.get(alias) is module:
                    del sys.modules[alias]
            raise
        finally:
            sys.path[:] = old_path

    def _payload(self):
        payload = json.dumps(
            dict(
                version=1,
                root=str(self.root),
                profile=self.profile,
                commit=self.commit,
                files=self.files,
                buffers={
                    name: base64.b64encode(raw).decode("ascii")
                    for name, raw in self.buffers.items()
                },
            ),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if len(payload) > CAPSULE_LIMIT:
            raise ValueError("oversized source capsule")
        return payload

    @property
    def transport_sha256(self):
        return hashlib.sha256(self._payload()).hexdigest()

    def resume_command(self):
        return [sys.executable, "-I", "-S", "-B", "-c", RESUME_BOOTSTRAP, self.transport_sha256]

    @contextmanager
    def sealed(self):
        """Anonymous immutable handoff. Caller owns closure, no discovery/recovery."""
        payload = self._payload()
        fd = os.memfd_create("genesis-index-source", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        try:
            with os.fdopen(os.dup(fd), "wb") as stream:
                stream.write(payload)
            fcntl.fcntl(fd, fcntl.F_ADD_SEALS, SEALS)
            yield fd
        finally:
            os.close(fd)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate source key")
        value[key] = item
    return value


def restore(fd, entry_bytes, expected_sha256):
    """Read immutable bytes without sharing file offsets or reopening checkout."""
    try:
        if fcntl.fcntl(fd, fcntl.F_GET_SEALS) & SEALS != SEALS:
            raise ValueError("source capsule is not sealed")
        size = os.fstat(fd).st_size
        if not 0 < size <= CAPSULE_LIMIT:
            raise ValueError("invalid source capsule size")
        payload = os.pread(fd, size, 0)
        if len(payload) != size:
            raise ValueError("incomplete source capsule")
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError("source handoff differs from captured command")
        value = json.loads(payload, object_pairs_hook=_unique)
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "root", "profile", "commit", "files", "buffers"}
            or type(value["version"]) is not int
            or value["version"] != 1
        ):
            raise ValueError("unsupported source capsule")
        names = set(_inventory(value["profile"]))
        if (
            not isinstance(value["buffers"], dict)
            or set(value["buffers"]) != names
            or not isinstance(value["files"], dict)
            or set(value["files"]) != names
        ):
            raise ValueError("invalid source capsule inventory")
        buffers = {
            name: base64.b64decode(raw, validate=True) for name, raw in value["buffers"].items()
        }
        if (
            any(len(raw) > FILE_LIMIT for raw in buffers.values())
            or _checksums(buffers) != value["files"]
            or buffers[SELF] != entry_bytes
            or entry_bytes != globals().get("_ENTRY_BYTES")
        ):
            raise ValueError("source capsule identity mismatch")
        root = Path(value["root"])
        if (
            not root.is_absolute()
            or root != Path(__file__).parents[2]
            or re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", value["commit"]) is None
        ):
            raise ValueError("invalid source capsule origin/commit")
        return Snapshot(root, value["profile"], value["commit"], MappingProxyType(buffers))
    except (OSError, TypeError, KeyError, RecursionError) as error:
        raise ValueError("invalid source capsule") from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("describe",))
    parser.add_argument("profile", choices=("entry", "runner"))
    args = parser.parse_args(argv)
    if "_CAPSULE_FD" in globals():
        captured = restore(
            globals()["_CAPSULE_FD"], globals()["_ENTRY_BYTES"], globals()["_CAPSULE_SHA256"]
        )
        with open(os.devnull, "rb") as stream:
            os.dup2(stream.fileno(), 0)
    else:
        captured = capture(Path(__file__).parents[2], args.profile, globals().get("_ENTRY_BYTES"))
    captured.assemble()
    print(json.dumps(dict(commit=captured.commit, files=captured.files, automatic_authority=False)))
    return 0


if __name__ == "__main__":
    os.execv(  # noqa: S606 - fixed isolated interpreter bootstrap, no shell
        sys.executable,
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            BOOTSTRAP,
            str(Path(__file__).resolve()),
            *sys.argv[1:],
        ],
    )
