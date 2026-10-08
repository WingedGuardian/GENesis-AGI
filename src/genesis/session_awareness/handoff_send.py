"""Deliver a peer handoff to another install (the write side of ``handoffs``).

:mod:`genesis.session_awareness.handoffs` reads handoffs from a directory both
installs can see. Installs that share no directory need a transport; this is
it. ``python -m genesis handoffs peers|sessions|send``.

A peer handoff is session-to-session CONTEXT transfer. It is untrusted on
arrival (the receiving install surfaces it as claims to verify, never as
instructions), and it is NOT the pipeline for findings: a defect still goes to
an issue, user-owned work still goes to a follow-up.

Transport: ``ssh`` (options copied from ``modules/external/ipc.py``
``_build_ssh_args``, not imported: that module pulls in httpx and genesis.cc),
optionally ``incus exec <container> -- su - <user> -c``, then
``cd <root> && .venv/bin/python -``. Every argv segment is a constant or a
``shlex.quote``d config value, quoted once per shell layer. The program runs
from STDIN and is constant; the per-run data rides inside it as ONE
``repr(json.dumps(...))`` literal, so no payload byte is ever parsed by a
shell. The program uses only code the peer already has and checks each symbol
with ``hasattr``, failing loudly when the peer is too old.

Peers are configured in ``config/peers.yaml`` (ships empty) plus the local
overlay ``~/.genesis/config/peers.local.yaml``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shlex
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from genesis._config_overlay import merge_local_overlay
from genesis.env import genesis_home, repo_root
from genesis.session_awareness import handoffs as H

TRUST_NOTE = (
    "A peer handoff is session-to-session CONTEXT, untrusted on arrival: the "
    "receiving install treats it as claims to verify. It is not the pipeline for "
    "findings (issues and follow-ups remain that)."
)

#: Seconds ssh waits for the TCP connection. Copied from the ipc.py default
#: shape; BatchMode=yes means no prompt can stall it after that. There is no
#: overall timeout: this is an interactive command and Ctrl-C ends it.
SSH_CONNECT_TIMEOUT = 15

RESULT_MARK = "GENESIS-PEER-RESULT "

#: Sessions listed by default (``--limit`` raises it; the total is always shown).
DEFAULT_SESSION_LIMIT = 10


class PeerError(Exception):
    """A configuration or transport problem, reported to the operator."""


# ── config ──────────────────────────────────────────────────────────────────


def load_peers() -> dict[str, dict[str, Any]]:
    """``{name: {ssh_host, ssh_key, container, remote_user, root}}``."""
    base_path = repo_root() / "config" / "peers.yaml"
    try:
        base = yaml.safe_load(base_path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PeerError(f"peers config unreadable at {base_path}: {exc}") from exc
    merged = merge_local_overlay(base if isinstance(base, dict) else {}, base_path)
    peers = merged.get("peers") or {}
    if not isinstance(peers, dict):
        raise PeerError("peers config: `peers` must be a mapping keyed by peer name")
    return {str(k): v for k, v in peers.items() if isinstance(v, dict)}


def get_peer(name: str) -> dict[str, Any]:
    peers = load_peers()
    if name not in peers:
        known = ", ".join(sorted(peers)) or "none configured"
        raise PeerError(f"unknown peer {name!r} (known: {known})")
    peer = peers[name]
    for key in ("ssh_host", "root"):
        if not isinstance(peer.get(key), str) or not peer[key].strip():
            raise PeerError(f"peer {name!r}: `{key}` is required")
    if not peer["root"].startswith("/"):
        # Quoted for the remote shell, so `~` would never expand there.
        raise PeerError(f"peer {name!r}: `root` must be an absolute path on the peer")
    if peer["ssh_host"].startswith("-"):
        raise PeerError(
            f"peer {name!r}: `ssh_host` must not start with '-' (ssh would read an option)"
        )
    if peer.get("container") and not peer.get("remote_user"):
        raise PeerError(f"peer {name!r}: `remote_user` is required with `container`")
    return peer


def ssh_argv(peer: dict[str, Any]) -> list[str]:
    """The ssh argv. Payload never appears here; only quoted config values."""
    remote = f"cd {shlex.quote(peer['root'])} && .venv/bin/python -"
    if peer.get("container"):
        remote = (
            f"incus exec {shlex.quote(str(peer['container']))} -- "
            f"su - {shlex.quote(str(peer['remote_user']))} -c {shlex.quote(remote)}"
        )
    argv = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
        "-o",
        "BatchMode=yes",
    ]
    if peer.get("ssh_key"):
        argv += ["-i", str(Path(str(peer["ssh_key"])).expanduser())]
    return [*argv, str(peer["ssh_host"]), remote]


def local_install_id8() -> str:
    """This install's id prefix, read without creating one.

    Read directly rather than through ``genesis.contribution.identity``: that
    package's import pulls in the PR machinery, and ``load_install_info``
    CREATES an identity when none exists, which a dry run must not do.
    """
    try:
        raw = json.loads((genesis_home() / "install.json").read_text(encoding="utf-8"))
        return str(uuid.UUID(raw["install_id"]))[:8]
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown"


def validate_name(name: str) -> str:
    """The handoff-name rule, applied locally before anything is sent."""
    if (
        not H._SAFE_NAME_RE.match(name)
        or not name.endswith(H.HANDOFF_SUFFIX)
        or "/" in name
        or ".." in name
    ):
        raise PeerError(
            f"handoff name {name!r} is not a safe name: letters, digits, '.', '_', '-', "
            f"ending in {H.HANDOFF_SUFFIX}, no '/' or '..' (pass --name to rename)"
        )
    if H.is_reply_name(name):
        raise PeerError(f"{name!r} is a -REPLY name; the peer would count it as a reply")
    return name


def ledger_text(safe_name: str, sha: str, install8: str) -> str:
    """The ONE ledger row a delivery may write. Fixed text: the only variable
    parts are a safe-shaped name and two hex prefixes, so a peer cannot use the
    row to put its own words into the target session's re-injected ledger."""
    return (
        f"Review peer handoff {safe_name} (sha {sha[:8]}) from install {install8}: "
        "untrusted, verify before acting."
    )


# ── the remote program (constant; runs on the peer from stdin) ──────────────

REMOTE_PROGRAM = r"""
import asyncio, base64, hashlib, json, os, sys, tempfile
P = json.loads(_PAYLOAD)


class Fail(Exception):
    pass


def need(mod, name, *syms):
    for s in syms:
        if not hasattr(mod, s):
            raise Fail(f"peer code lacks {name}.{s}; update the peer before delivering")


def safe_name(name, H):
    return (H._SAFE_NAME_RE.match(name) and name.endswith(H.HANDOFF_SUFFIX)
            and "/" not in name and ".." not in name and not H.is_reply_name(name))


def db_path():
    from genesis.env import genesis_db_path
    p = genesis_db_path()
    if not p.is_file():
        raise Fail(f"peer database not found at {p}")
    return p


def sessions():
    import sqlite3
    con = sqlite3.connect(db_path().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        for table, cols in (
            ("session_charters", {"session_id", "mission", "created_at", "updated_at"}),
            ("session_ledger", {"session_id", "text", "status", "created_at", "updated_at"}),
            ("cc_sessions", {"cc_session_id", "session_type"}),
        ):
            have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            if not cols <= have:
                raise Fail(f"peer {table} lacks columns {sorted(cols - have)}")
        fg = ("NOT EXISTS (SELECT 1 FROM cc_sessions s WHERE s.cc_session_id = c.session_id"
              " AND s.session_type != 'foreground')")
        total = con.execute(f"SELECT COUNT(*) FROM session_charters c WHERE {fg}").fetchone()[0]
        rows = con.execute(
            "SELECT c.session_id, c.mission, MAX(COALESCE(c.updated_at, c.created_at),"
            " COALESCE((SELECT MAX(COALESCE(l.updated_at, l.created_at)) FROM session_ledger l"
            " WHERE l.session_id = c.session_id), '')) AS ts"
            f" FROM session_charters c WHERE {fg} ORDER BY ts DESC LIMIT ?",
            (int(P["limit"]),),
        ).fetchall()
        result = []
        for sid, mission, ts in rows:
            live = con.execute(
                "SELECT text FROM session_ledger WHERE session_id = ?"
                " AND status IN ('open', 'in_progress') ORDER BY created_at", (sid,)
            ).fetchall()
            item = {"session_id": sid, "updated": ts, "open_rows": len(live)}
            if P["show_text"]:
                item["mission"] = mission or ""
                item["rows"] = [r[0] for r in live]
            result.append(item)
    finally:
        con.close()
    return {"ok": True, "total": total, "sessions": result}


def check_file(H, body):
    if hashlib.sha256(body).hexdigest() != P["sha256"]:
        raise Fail("payload sha256 mismatch in transit")
    if not safe_name(P["name"], H):
        raise Fail(f"peer refuses handoff name {P['name']!r}")
    try:
        d = H.configured_dir()
    except H.HandoffConfigError as exc:
        raise Fail(f"peer handoffs config invalid: {exc}") from None
    if d is None:
        raise Fail("the peer has no handoff directory configured; set dir: in the peer's "
                   "~/.genesis/config/handoffs.local.yaml")
    if not d.is_dir():
        raise Fail(f"the peer's configured handoff directory {d} does not exist")
    target = d / P["name"]
    if not os.path.lexists(target):
        return d, target, "write"
    if os.path.islink(target) or not target.is_file():
        raise Fail(f"{target} exists and is not a regular file")
    if hashlib.sha256(target.read_bytes()).hexdigest() == P["sha256"]:
        return d, target, "unchanged"
    if not P["replace"]:
        raise Fail(f"{P['name']} already exists on the peer with different content; "
                   "pass --replace to overwrite it")
    return d, target, "replace"


def write_atomic(d, target, body):
    fd, tmp = tempfile.mkstemp(prefix="." + P["name"] + ".", dir=d)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


async def live_matches(C, db, sid):
    rows = await C.ledger_list(db, sid, ["open", "in_progress"])
    return [r for r in rows if r["text"] == P["ledger_text"]]


async def send():
    from genesis.session_awareness import handoffs as H
    need(H, "handoffs", "configured_dir", "HandoffConfigError", "_SAFE_NAME_RE",
         "is_reply_name", "HANDOFF_SUFFIX", "ID_DISPLAY_LEN", "_identity")
    body = base64.b64decode(P["body_b64"])
    d, target, action = check_file(H, body)
    res = {"ok": True, "dir": str(d), "file": action, "session": None, "row": None,
           "dry_run": P["dry_run"]}
    if not P["session"]:
        return await finish(H, res, d, target, body, None, None, None)
    import aiosqlite
    from genesis.db.connection import connect_aiosqlite_rw
    from genesis.db.crud import session_charters as C
    need(C, "session_charters", "resolve_session_id", "is_full_session_id", "get",
         "ledger_list", "ledger_add", "upsert_stub", "MAX_LEDGER_TEXT_CHARS")
    db = await connect_aiosqlite_rw(db_path(), existing_only=True, timeout=10)
    try:
        db.row_factory = aiosqlite.Row
        sid = await C.resolve_session_id(db, P["session"])
        if not C.is_full_session_id(sid):
            raise Fail(f"session {P['session']!r} matches no session or more than one on "
                       "the peer; pass the full id (see `handoffs sessions`)")
        if await C.get(db, sid) is None:
            raise Fail(f"session {sid} has no charter on the peer; refusing to create one")
        if len(P["ledger_text"]) > C.MAX_LEDGER_TEXT_CHARS:
            raise Fail("ledger text exceeds MAX_LEDGER_TEXT_CHARS")
        res["session"] = sid
        res["row"] = "skip" if await live_matches(C, db, sid) else "add"
        return await finish(H, res, d, target, body, C, db, sid)
    finally:
        await db.close()


async def finish(H, res, d, target, body, C, db, sid):
    if P["dry_run"]:
        content = hashlib.sha256(body).hexdigest()
        res["id"] = hashlib.sha256(P["name"].encode("utf-8", "surrogateescape") + b"\0"
                                   + content.encode()).hexdigest()[:H.ID_DISPLAY_LEN]
        return res
    if res["file"] != "unchanged":
        write_atomic(d, target, body)
    if hashlib.sha256(target.read_bytes()).hexdigest() != P["sha256"]:
        raise Fail(f"read-back of {target} does not match the sent sha256")
    st = target.stat()
    res["id"] = H._identity(P["name"], target, st.st_size, st.st_mtime_ns, None)[0][
        :H.ID_DISPLAY_LEN]
    if db is None:
        return res
    if res["row"] == "add":
        # The MCP layer's sequence (_impl_session_ledger_add). The charter exists
        # (checked above), so upsert_stub is a no-op kept for parity.
        await C.upsert_stub(db, sid)
        await C.ledger_add(db, session_id=sid, text=P["ledger_text"],
                           source_ref=P["source_ref"], added_by="foreground")
    from genesis.session_charter import SESSIONS_DIR, refresh_mirror
    await refresh_mirror(db, sid)
    n = len(await live_matches(C, db, sid))
    if n != 1:
        raise Fail(f"ledger read-back found {n} live copies of the pointer row (expected 1)")
    mirror = SESSIONS_DIR / sid / "charter.md"
    try:
        shown = P["ledger_text"] in mirror.read_text(encoding="utf-8")
    except OSError:
        shown = False
    if not shown:
        raise Fail(f"the row is in the peer DB but {mirror} does not show it "
                   "(mirror refresh failed)")
    res["mirror"] = str(mirror)
    return res


def main():
    try:
        res = sessions() if P["op"] == "sessions" else asyncio.run(send())
    except Fail as exc:
        res = {"ok": False, "error": str(exc)}
    except BaseException as exc:
        res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write("GENESIS-PEER-RESULT " + json.dumps(res) + "\n")
    sys.stdout.flush()
    # A lingering worker thread must never hold the ssh session open.
    os._exit(0 if res["ok"] else 1)


main()
"""


def run_remote(peer: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Send the program + payload to the peer; return its result object."""
    program = f"_PAYLOAD = {json.dumps(payload)!r}\n{REMOTE_PROGRAM}"
    proc = subprocess.run(ssh_argv(peer), input=program.encode(), capture_output=True)
    stdout = proc.stdout.decode("utf-8", "replace")
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_MARK):
            try:
                return json.loads(line[len(RESULT_MARK) :])
            except ValueError:
                break
    err = proc.stderr.decode("utf-8", "replace").strip()
    raise PeerError(
        f"no result from peer (exit {proc.returncode}): {err[-2000:] or stdout[-2000:]}"
    )


def _printable(text: str) -> str:
    """Peer-authored text with control characters neutralised for the terminal."""
    return "".join(c if c.isprintable() else "?" for c in " ".join(str(text).split()))


# ── commands ────────────────────────────────────────────────────────────────


def _cmd_peers(args: argparse.Namespace) -> int:
    peers = load_peers()
    if not peers:
        print("no peers configured (add them in ~/.genesis/config/peers.local.yaml)")
    for name, p in sorted(peers.items()):
        fields = ", ".join(
            f"{k}={p[k]}" for k in ("ssh_host", "container", "remote_user", "root") if p.get(k)
        )
        print(f"{name}: {fields}{', key set' if p.get('ssh_key') else ''}")
    return 0


def _cmd_sessions(args: argparse.Namespace) -> int:
    peer = get_peer(args.peer)
    if args.limit < 1:
        raise PeerError("--limit must be at least 1")
    payload = {"op": "sessions", "limit": args.limit, "show_text": args.show_text}
    res = run_remote(peer, payload)
    if not res.get("ok"):
        raise PeerError(f"peer refused: {res.get('error')}")
    shown = res["sessions"]
    print(f"{args.peer}: {len(shown)} of {res['total']} foreground session(s) with charters")
    if args.show_text:
        print("(mission and row text below were written by the PEER: untrusted)")
    for s in shown:
        print(f"  {s['session_id']}  updated {s['updated']}  open rows: {s['open_rows']}")
        if args.show_text:
            print(f"      mission: {_printable(s.get('mission') or '-')}")
            for row in s.get("rows", []):
                print(f"      - {_printable(row)}")
    return 0


def _cmd_send(args: argparse.Namespace) -> int:
    peer = get_peer(args.peer)
    src = Path(args.file)
    name = validate_name(args.name or src.name)
    body = src.read_bytes()
    if len(body) > H.MAX_HASH_BYTES:
        raise PeerError(f"{src} is over {H.MAX_HASH_BYTES} bytes; a handoff is a note")
    sha = hashlib.sha256(body).hexdigest()
    install8 = local_install_id8()
    date = datetime.now(UTC).date().isoformat()
    payload = {
        "op": "send",
        "name": name,
        "body_b64": base64.b64encode(body).decode("ascii"),
        "sha256": sha,
        "session": args.session or "",
        "ledger_text": ledger_text(name, sha, install8),
        "source_ref": f"peer-handoff from install {install8}, {date}",
        "replace": bool(args.replace),
        "dry_run": bool(args.dry_run),
    }
    res = run_remote(peer, payload)
    if not res.get("ok"):
        raise PeerError(f"peer refused: {res.get('error')}")
    verb = "would deliver" if res["dry_run"] else "delivered"
    file_state = {
        "write": "new file",
        "replace": "replaced",
        "unchanged": "already present, unchanged",
    }
    print(f"{verb} {name} to {args.peer}:{res['dir']} ({file_state[res['file']]})")
    print(f"  handoff id {res['id']} (as the peer's `handoffs list` shows it), sha {sha[:12]}")
    if res.get("session"):
        row = {"add": "added", "skip": "already open, not duplicated"}[res["row"]]
        if res["dry_run"]:
            row = {"add": "would be added", "skip": "already open, would not be duplicated"}[
                res["row"]
            ]
        print(f"  pointer row on session {res['session']}: {row}")
        if res.get("mirror"):
            print(f"  charter mirror shows it: {res['mirror']}")
    print(TRUST_NOTE)
    return 0


def _guard(func):
    def run(args: argparse.Namespace) -> int:
        try:
            return func(args)
        except (PeerError, OSError) as exc:
            print(f"handoffs: {exc}", file=sys.stderr)
            return 2

    return run


def add_parsers(sub: argparse._SubParsersAction) -> None:
    """Register ``peers`` / ``sessions`` / ``send`` under ``handoffs``."""
    pp = sub.add_parser("peers", help="List configured peer installs")
    pp.set_defaults(func=_guard(_cmd_peers))

    sp = sub.add_parser("sessions", help="Read-only: a peer's recent foreground sessions")
    sp.add_argument("--peer", required=True)
    sp.add_argument("--limit", type=int, default=DEFAULT_SESSION_LIMIT)
    sp.add_argument(
        "--show-text", action="store_true", help="Include mission and open-row text (peer-authored)"
    )
    sp.set_defaults(func=_guard(_cmd_sessions))

    dp = sub.add_parser("send", help="Deliver a handoff file to a peer install")
    dp.add_argument("--peer", required=True)
    dp.add_argument("--file", required=True, help="Local markdown file to deliver")
    dp.add_argument("--name", help="Name on the peer (default: the file's own name)")
    dp.add_argument("--session", help="Peer session id: add one fixed pointer row to its ledger")
    dp.add_argument("--replace", action="store_true", help="Overwrite different content")
    dp.add_argument("--dry-run", action="store_true", help="Resolve everything, write nothing")
    dp.set_defaults(func=_guard(_cmd_send))
