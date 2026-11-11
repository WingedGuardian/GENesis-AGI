#!/usr/bin/env python3
"""Finite process-private source capture; supplies identity, never launch authority.

Compile and hash the same buffers (transcript_analytics.scrub's existing pattern).
The fixed isolated bootstrap is a declared trust root. A sealed capsule preserves
bytes across handoff; its seals do not authenticate its producer. Reservation and
supervised recovery remain the queue's responsibility. Describe is read-only;
captured shell handoffs retain the existing owner's admission and readiness.
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
import shlex
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
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
    ("scope", "_genesis_code_intel_scope", "scripts/lib/code_intel_scope.py"),
    ("legacy", "_genesis_index_marker_legacy", "scripts/lib/index_marker_legacy.py"),
    ("recovery", "_genesis_index_marker_recovery", "scripts/lib/index_marker_recovery.py"),
    ("legs", "_genesis_index_marker_legs", "scripts/lib/index_marker_legs.py"),
    ("native", "_genesis_index_marker_execution", "scripts/lib/index_marker_execution.py"),
    ("cli", "_genesis_index_marker_cli", "scripts/lib/index_marker_cli.py"),
    ("queue", "_genesis_index_marker", "scripts/lib/index_marker.py"),
    ("queued", "_genesis_code_intel_queued_worker", "scripts/lib/code_intel_queued_worker.py"),
    ("gitnexus", "_genesis_code_intel_gitnexus", "scripts/lib/code_intel_gitnexus.py"),
    ("owner", "_genesis_code_intel_owner", "scripts/lib/code_intel_owner.py"),
    ("owner_admission", "_genesis_code_intel_owner_admission", "scripts/lib/code_intel_owner_admission.py"),
    ("managed", "_genesis_codebase_managed", "scripts/codebase_managed.py"),
)
SHELL = (
    "scripts/lib/code_intel_index.sh",
    "scripts/lib/gitnexus_version.sh",
    "scripts/lib/proc_pressure.sh",
    "scripts/lib/code_intel_scope.sh",
)
FILE_LIMIT = 1024 * 1024
CAPSULE_LIMIT = 4 * FILE_LIMIT
RUNTIME_ROLES = ('reporter',)
SOURCE_ROLES = ('owner',)
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

# Only an inherited private capsule enters these fixed roles. Seals
# preserve bytes, not producer trust. Helpers are assembled after full validation.
RESUME_BOOTSTRAP = """import base64,fcntl,hashlib,importlib.util,json,os,pathlib,sys
if fcntl.fcntl(0,fcntl.F_GET_SEALS)&15!=15: raise ValueError('unsealed source')
capsule=os.fstat(0)
for fd in (1,2):
 other=os.fstat(fd)
 if (capsule.st_dev,capsule.st_ino)==(other.st_dev,other.st_ino):
  raise ValueError('source capsule aliases a standard output')
n=capsule.st_size
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
raise SystemExit(m.main(sys.argv[2:]))
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
    _runtime_json: bytes | None = field(default=None, repr=False)

    @property
    def files(self):
        return _checksums(self.buffers)

    @property
    def runtime_value(self):
        """Return an independent private value; diagnostics expose only source."""
        return None if self._runtime_json is None else json.loads(self._runtime_json)

    def with_runtime(self, value):
        """Bind transport structure; each runtime role must validate authority."""
        fields = {"context", "reservation", "policy", "provider_options", "environment_recipe"}
        if self.profile != "runner":
            raise ValueError("runtime capsule requires the runner profile")
        if (not isinstance(value, dict) or set(value) != fields
                or any(not isinstance(value[key], dict) for key in fields)):
            raise ValueError("invalid private runtime envelope")
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        result = replace(self, _runtime_json=raw)
        result._payload()  # The existing bound includes private runtime values.
        return result

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
                if key == "queued":
                    module.__dict__["_CAPTURED_CLEANUP"] = loaded["scope"].cleanup_observation
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
        value = dict(
            version=1,
            root=str(self.root),
            profile=self.profile,
            commit=self.commit,
            files=self.files,
            buffers={
                name: base64.b64encode(raw).decode("ascii")
                for name, raw in self.buffers.items()
            },
        )
        if self._runtime_json is not None:
            value.update(version=2, runtime=self.runtime_value)
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if len(payload) > CAPSULE_LIMIT:
            raise ValueError("oversized source capsule")
        return payload

    @property
    def transport_sha256(self):
        return hashlib.sha256(self._payload()).hexdigest()

    def resume_command(self, action="describe", *arguments):
        if action not in ("describe", "shell", "queue", "available", *SOURCE_ROLES, *RUNTIME_ROLES):
            raise ValueError("unsupported captured role")
        if action in SOURCE_ROLES and (self._runtime_json is not None or self.profile != 'runner'):
            raise ValueError('initial owner requires source-only runner input')
        if action in RUNTIME_ROLES and (self._runtime_json is None or self.profile != 'runner'):
            raise ValueError('executable role requires private runner runtime')
        if self._runtime_json is not None and action not in ('describe', *RUNTIME_ROLES):
            raise ValueError("runtime capsule cannot use a source-only role")
        return [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            RESUME_BOOTSTRAP,
            self.transport_sha256,
            action,
            self.profile,
            *arguments,
        ]

    def shell_prelude(self, role):
        names = {
            "entry": "scripts/lib/code_intel_index.sh",
            "runner": "scripts/code_intel_runner.sh",
        }
        if role not in names or (role == "runner" and self.profile != "runner"):
            raise ValueError("unsupported captured shell role")
        lines = [
            "readonly _CI_ORIGIN=" + shlex.quote(str(self.root / names[role])),
            "readonly _CI_EXPECTED_ENTRY=" + shlex.quote(str(self.root / names["entry"])),
        ]
        cases = []
        for index, name in enumerate(SHELL[1:]):
            body = self.buffers[name].decode("utf-8")
            if "\0" in body:
                raise ValueError("NUL in captured shell helper")
            variable = "_CI_BUFFER_" + str(index)
            lines.append("readonly " + variable + "=" + shlex.quote(body))
            cases.append(
                name.rsplit("/", 1)[1] + ") builtin source <(printf '%s' \"$" + variable + '");;'
            )
        lines.append('_ci_source() { case "$1" in ' + "\n".join(cases) + "\n*) return 2;; esac; }")
        functions = [
            ("_ci_entry", "shell", ("entry",)),
            ("_ci_queue", "queue", ()),
            ("_ci_available", "available", ()),
        ]
        if role == 'runner':
            functions.append(('_ci_owner', 'owner', ()))
        for function, action, arguments in functions:
            lines.append(
                function
                + "() { "
                + shlex.join(self.resume_command(action, *arguments))
                + ' "$@"; }'
            )
        lines.append("readonly -f _ci_source " + ' '.join(row[0] for row in functions))
        return "\n".join(lines) + "\n"

    def shell_command(self, role, arguments):
        prelude = self.shell_prelude(role)
        name = (
            "scripts/code_intel_runner.sh"
            if role == "runner"
            else "scripts/lib/code_intel_index.sh"
        )
        body = self.buffers[name].decode("utf-8")
        if "\0" in body:
            raise ValueError("NUL in captured shell body")
        # -p ignores startup files, imported functions and inherited shell options.
        # Bash5.2.21: oracle8/8, -p8/8, ordinary1/8 across the registered8-cell sweep.
        return [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-p",
            "-c",
            prelude + body,
            str(self.root / name),
            *arguments,
        ]

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
            or type(value.get("version")) is not int
            or value["version"] not in (1, 2)
        ):
            raise ValueError("unsupported source capsule")
        fields = {"version", "root", "profile", "commit", "files", "buffers"}
        if value["version"] == 2:
            fields.add("runtime")
        if set(value) != fields:
            raise ValueError("unsupported source capsule fields")
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
        snapshot = Snapshot(root, value["profile"], value["commit"], MappingProxyType(buffers))
        return snapshot.with_runtime(value["runtime"]) if value["version"] == 2 else snapshot
    except (OSError, TypeError, KeyError, RecursionError) as error:
        raise ValueError("invalid source capsule") from error


def _runner_lock_fds():
    """Keep only the canonical inherited runner lock in its entry handoff."""
    base = Path(os.environ.get("GENESIS_HOME") or str(Path.home() / ".genesis"))
    if not base.is_absolute():
        raise ValueError("captured runner requires an absolute lock namespace")
    held = os.fstat(8)
    named = (base / "locks/code-intel-runner.lock").stat(follow_symlinks=False)
    if (not stat.S_ISREG(held.st_mode) or not stat.S_ISREG(named.st_mode)
            or (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino)):
        raise ValueError("captured runner lock identity differs")
    # Same inherited open-file description preserves the existing lock; an
    # unlocked legitimate descriptor may acquire it, but a competitor refuses.
    fcntl.flock(8, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return (8,)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("describe", "shell", "queue", "available", *SOURCE_ROLES, *RUNTIME_ROLES))
    parser.add_argument("profile", choices=("entry", "runner"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if "_CAPSULE_FD" in globals():
        captured = restore(
            globals()["_CAPSULE_FD"], globals()["_ENTRY_BYTES"], globals()["_CAPSULE_SHA256"]
        )
        with open(os.devnull, "rb") as stream:
            os.dup2(stream.fileno(), 0)
    else:
        captured = capture(Path(__file__).parents[2], args.profile, globals().get("_ENTRY_BYTES"))
    if args.profile != captured.profile:
        parser.error("captured profile differs from command")
    if args.action in SOURCE_ROLES and (captured.runtime_value is not None or captured.profile != 'runner'):
        parser.error('initial owner requires source-only runner input')
    if args.action in RUNTIME_ROLES and (captured.runtime_value is None or captured.profile != 'runner'):
        parser.error('executable role requires private runner runtime')
    if captured.runtime_value is not None and args.action not in ('describe', *RUNTIME_ROLES):
        parser.error("runtime capsule cannot use a source-only role")
    if args.action == 'owner':
        if len(args.arguments) != 3:
            parser.error('initial owner requires context, run mode and managed config')
        try:
            _runner_lock_fds()
        except (OSError, ValueError) as error:
            print('code-intel: initial owner refused: ' + type(error).__name__, file=sys.stderr)
            return 75
        modules = captured.assemble()
        return modules['owner_admission'].main(captured, modules, *args.arguments)
    if args.action == 'reporter':
        if (len(args.arguments) != 1 or not args.arguments[0].isascii()
                or not args.arguments[0].isdecimal()):
            parser.error('reporter requires its startup descriptor')
        modules = captured.assemble()
        return modules['queued'].reporter(captured, modules, int(args.arguments[0]))
    if args.action == "shell":
        if not args.arguments:
            parser.error("shell role required")
        inherited = _runner_lock_fds() if (
            captured.profile == "runner" and args.arguments[0] == "entry"
        ) else ()
        with captured.sealed() as fd:
            return subprocess.run(
                captured.shell_command(args.arguments[0], args.arguments[1:]),
                stdin=fd, pass_fds=inherited, check=False
            ).returncode
    modules = captured.assemble()
    if args.action == "queue":
        try:
            return modules["queue"].main(args.arguments)
        except Exception:
            import traceback

            traceback.print_exc()
            return 2  # The existing queue CLI's visible failure contract.
    if args.action == "available":
        if len(args.arguments) != 3:
            parser.error("available requires config, repo and persistence")
        config, repo, persistence = args.arguments
        return modules["managed"].main(
            ["--config", config, "available", "--repo", repo, "--persistence", persistence]
        )
    if args.arguments:
        parser.error("describe takes no extra arguments")
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
