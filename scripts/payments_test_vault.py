#!/usr/bin/env python3
"""TEST-vault harness: a live card-fill run against a separate vault that holds only a test card.

Run it with the MAIN venv's python (the vault side is main's code)::

    ~/genesis/.venv/bin/python scripts/payments_test_vault.py setup \
        --worktree <branch worktree> --home <new dir> --merchant <payment host>
    ~/genesis/.venv/bin/python scripts/payments_test_vault.py serve --home <dir>
    ~/genesis/.venv/bin/python scripts/payments_test_vault.py teardown --home <dir>

``setup`` builds the directory: a venv whose ``import genesis`` is the branch
worktree's, a vault holding one card (Stripe's public test Visa, labelled with
``TEST_LABEL``) unlocked by a passphrase the harness generated, an audit
database, and ``mcp.json`` for a test health MCP that runs the worktree's
server. ``serve`` runs a vault service on that directory's own socket with its
own approval registry, whose taps go through a SECOND, test-only Telegram bot
(``BOT_TOKEN_KEY`` in secrets.env) and the tap handler genesis-server uses. The
owner starts the test session (``claude --strict-mcp-config --mcp-config
<dir>/mcp.json``); the harness never starts one. ``teardown`` stops this
directory's serve and moves the directory to the Genesis trash.

The live vault, its socket, its cards and the live bot stay out of reach by
refusal: live paths are computed independently of the harness's own
environment (the passwd home, the operator's and genesis-server's environment,
every listening vault socket); the test directory never overlaps a live home;
the test socket can only resolve inside the test directory and never shares a
live socket's inode (asyncio unlinks a stale socket at its bind path); the
test audit database is a private file of its own, never a live one; the test
vault must hold exactly the one test card; no option, stdin or file can bring
another card in; the test MCP's environment is re-read from /proc on every
release op; the peer policies of the two vaults exclude each other; the
worktree is never the main checkout or on main; the card-fill overlay lands
only in the worktree; teardown signals only a process whose own command line
is this directory's serve. Each refusal message starts with its code; the
table is in docs/reference/payment-test-vault.md.

The TEST label cannot be dropped: every approval shows ``TEST_LINE`` first
(added by the registry here, which also refuses any card line but the test
card's), and the bot object the registry holds refuses to send any text
without it. A serve, and every release, is refused while a browser not started
by the test MCP runs (owner ruling Q16), and a test bot token that matches any
other bot's is refused.

Residual: the harness and every Genesis process run as one user, so a
same-user process can get past all of this. These refusals stop mistakes, not
an adversary. The test MCP inherits the rest of the owner's environment from
Claude Code (``DISPLAY`` included: its browser shows on the live display, owner
ruling Q8), so only the variables named in ``check_child_env`` are checked.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import pwd
import secrets
import signal
import stat
import subprocess
import sys
import sysconfig
import time
from dataclasses import dataclass
from pathlib import Path

from genesis.payments import vault_store as vs
from genesis.payments.approval import REFUSED, PaymentApprovalRegistry
from genesis.payments.peer_check import PeerPolicy, PeerRefused, check_release_peer, start_of

# Stripe's public test Visa. The only card this harness can put in a vault (R5).
TEST_NUMBER = "4242424242424242"
TEST_EXPIRY = "12/34"
TEST_CVC = "123"
TEST_NAME = "TEST HOLDER"
TEST_POSTAL = "12345"
TEST_LABEL = "TEST 4242"
TEST_KEY, TEST_VALUE = "TEST", "test vault, Stripe test card only"
TEST_LINE = f"{TEST_KEY}: {TEST_VALUE}"

BOT_TOKEN_KEY = "PAYMENT_TEST_VAULT_BOT_TOKEN"  # noqa: S105 - a key name; nothing else reads it
LIVE_BOT_TOKEN_KEY = "TELEGRAM_BOT_TOKEN"  # noqa: S105 - a key name, read only for its bot id
MARKER = ".genesis-test-vault"
MCP_NAME = "genesis-health-b1test"
OVERLAY = Path("config") / "payments.local.yaml"
SOCKET_NAME = "payment_vault.sock"
SERVER_SCRIPT = Path("scripts") / "genesis_mcp_server.py"
# Process names of the browsers the health MCP launches (comm, 15 chars at most).
BROWSER_COMMS = frozenset(
    {
        "camoufox-bin",
        "camoufox",
        "firefox",
        "firefox-bin",
        "chrome",
        "chromium",
        "chromium-browse",
        "headless_shell",
    }
)
APPROVAL_TIMEOUT_MAX_S = 600
APPROVAL_TIMEOUT_MIN_S = 30
# Teardown waits this long for a serve to stop after SIGINT: its vault stop is
# bounded at a few 5 s waits and the Telegram poller's shutdown at one long poll.
_SERVE_STOP_WAIT_S = 60.0
# The venv check imports one package; a hung child must not hold setup forever.
_SUBPROCESS_TIMEOUT_S = 120


class Refused(Exception):
    """A refusal with a fixed reason; the message starts with its R-code."""


@dataclass(frozen=True)
class Layout:
    root: Path

    @property
    def marker(self) -> Path:
        return self.root / MARKER

    @property
    def state(self) -> Path:
        return self.root / "state.json"

    @property
    def passphrase(self) -> Path:
        return self.root / "passphrase"

    @property
    def pidfile(self) -> Path:
        return self.root / "serve.pid"

    @property
    def mcp_json(self) -> Path:
        return self.root / "mcp.json"

    @property
    def venv(self) -> Path:
        return self.root / "venv"

    @property
    def venv_python(self) -> Path:
        return self.venv / "bin" / "python"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def genesis_home(self) -> Path:
        return self.home / ".genesis"

    @property
    def vault(self) -> Path:
        return self.genesis_home / "payment_vault"

    @property
    def socket(self) -> Path:
        return self.genesis_home / "run" / SOCKET_NAME

    @property
    def db(self) -> Path:
        return self.home / "genesis-test.db"

    @property
    def secrets(self) -> Path:
        return self.home / "secrets.env"  # never created: the test MCP reads no secrets


def layout_for(home: str | Path) -> Layout:
    path = Path(home).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    # The parent resolved, the leaf kept: the leaf may not exist yet, and a
    # symlink there is refused by setup (it must not exist) and by the marker read.
    return Layout(Path(os.path.realpath(path.parent)) / path.name)


def _inside(path: Path, base: Path) -> bool:
    return path == base or base in path.parents


# -- R1: live paths -----------------------------------------------------------


def listening_vault_sockets(proc_root: Path = Path("/proc")) -> list[Path]:
    """Paths of every LISTENING unix socket named payment_vault.sock in this netns."""
    found = []
    try:
        lines = (proc_root / "net" / "unix").read_text(errors="replace").splitlines()[1:]
    except OSError as exc:
        raise Refused(f"R1: /proc/net/unix cannot be read ({exc.strerror})") from None
    for line in lines:
        parts = line.split(None, 7)
        if len(parts) == 8 and int(parts[3], 16) & 0x10000 and parts[7].endswith("/" + SOCKET_NAME):
            found.append(Path(parts[7]))
    return found


def _environ_values(proc_root: Path, pid: int, names: tuple[str, ...]) -> dict[str, str]:
    """Only ``names`` from a process's exec-time environment (the rest is dropped)."""
    raw = (proc_root / str(pid) / "environ").read_bytes()
    out = {}
    for entry in raw.split(b"\0"):
        key, sep, value = entry.partition(b"=")
        name = key.decode("utf-8", "replace")
        if sep and name in names:
            out[name] = os.fsdecode(value)
    del raw
    return out


def genesis_server_pid() -> int | None:
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", "-p", "MainPID", "--value", "genesis-server"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return int(out) if out.isdigit() and int(out) > 0 else None


@dataclass(frozen=True)
class Live:
    homes: frozenset[Path]  # live GENESIS_HOMEs, resolved
    sockets: frozenset[Path]  # live socket paths (listening, plus each home's default)
    dbs: frozenset[Path] = frozenset()  # live databases the test DB must never be


def live_paths(
    *,
    env: dict | None = None,
    proc_root: Path = Path("/proc"),
    server_pid: int | None = None,
    exclude_socket: Path | None = None,
    notes: list[str] | None = None,
    extra_dbs: tuple[Path, ...] = (),
) -> Live:
    """R1, from the operator's environment, the passwd home, the server and the sockets."""
    env = os.environ if env is None else env
    notes = [] if notes is None else notes
    homes = {Path(pwd.getpwuid(os.getuid()).pw_dir) / ".genesis"}
    if env.get("HOME"):
        homes.add(Path(env["HOME"]) / ".genesis")
    if env.get("GENESIS_HOME"):
        homes.add(Path(env["GENESIS_HOME"]).expanduser())
    dbs = set(extra_dbs)
    if env.get("GENESIS_DB_PATH"):
        dbs.add(Path(env["GENESIS_DB_PATH"]).expanduser())
    if server_pid is None:
        notes.append("genesis-server's pid is unknown; its own GENESIS_HOME was not read")
    else:
        try:
            values = _environ_values(
                proc_root, server_pid, ("GENESIS_HOME", "HOME", "GENESIS_DB_PATH")
            )
        except OSError:
            notes.append("genesis-server's environment could not be read")
        else:
            if values.get("GENESIS_HOME"):
                homes.add(Path(values["GENESIS_HOME"]))
            if values.get("HOME"):
                homes.add(Path(values["HOME"]) / ".genesis")
            if values.get("GENESIS_DB_PATH"):
                dbs.add(Path(values["GENESIS_DB_PATH"]))
    sockets = set()
    for sock in listening_vault_sockets(proc_root):
        if exclude_socket is not None and str(sock) == str(exclude_socket):
            continue
        sockets.add(sock)
        homes.add(sock.parent.parent)
    resolved = frozenset(Path(os.path.realpath(h)) for h in homes)
    sockets |= {h / "run" / SOCKET_NAME for h in resolved}
    return Live(resolved, frozenset(sockets), frozenset(dbs))


# -- R2, R3, R4, R6, R9, R10 ----------------------------------------------------


def check_placement(layout: Layout, live: Live, *, new: bool) -> None:
    """R2: the test dir never overlaps a live home; new at setup, marked afterwards."""
    root = Path(os.path.realpath(layout.root))
    for home in live.homes:
        if _inside(root, home) or _inside(home, root):
            raise Refused(f"R2: {layout.root} overlaps a live Genesis home ({home})")
    if new:
        if os.path.lexists(layout.root):
            raise Refused(f"R2: {layout.root} already exists; setup makes a new directory")
        return
    if root != layout.root:
        raise Refused(f"R2: {layout.root} is a symlink; use its real path")
    read_marker(layout)


def read_marker(layout: Layout) -> str:
    try:
        info = os.lstat(layout.marker)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OSError
        marker = layout.marker.read_text().strip()
        state = json.loads(layout.state.read_text())
    except (OSError, ValueError):
        raise Refused(
            f"R2: {layout.root} has no harness marker; not a test-vault directory"
        ) from None
    if not marker or state.get("marker") != marker:
        raise Refused(f"R2: {layout.root}'s marker does not match its state file")
    return marker


def check_socket(layout: Layout, live: Live) -> None:
    """R3, before the bind: the test socket path can only be the test vault's own."""
    root = Path(os.path.realpath(layout.root))
    run = Path(os.path.realpath(layout.socket.parent))
    if not _inside(run, root):
        raise Refused("R3: the test socket's directory resolves outside the test directory")
    for sock in live.sockets:
        if run == Path(os.path.realpath(sock.parent)):
            raise Refused("R3: the test socket's directory is a live vault's run directory")
    if any(str(s) == str(layout.socket) for s in listening_vault_sockets()):
        raise Refused("R3: something already listens on the test socket (another serve?)")
    try:
        here = os.lstat(layout.socket)
    except FileNotFoundError:
        return
    for sock in live.sockets:
        try:
            other = os.lstat(sock)
        except OSError:
            continue
        if (here.st_dev, here.st_ino) == (other.st_dev, other.st_ino):
            raise Refused("R3: the file at the test socket path is a live vault socket")
    if not stat.S_ISSOCK(here.st_mode):
        raise Refused("R3: something other than a socket is at the test socket path")


def check_vault_content(layout: Layout, merchant: str) -> str:
    """R4: exactly one card, the test card, active and scoped as setup made it; its ref."""
    try:
        cards = vs.list_cards(layout.vault)
    except (vs.VaultError, OSError):
        raise Refused("R4: the test vault cannot be read") from None
    if len(cards) != 1:
        raise Refused(
            f"R4: the test vault holds {len(cards)} cards; it must hold only the test card"
        )
    card = cards[0]
    expected = {
        "status": "active",
        "label": TEST_LABEL,
        "last4": TEST_NUMBER[-4:],
        "brand": "visa",
        "merchant": merchant,
        "single_use": 0,
    }
    if any(card.get(k) != v for k, v in expected.items()):
        raise Refused("R4: the test vault's card is not the TEST 4242 card setup added")
    return card["card_ref"]


def read_passphrase(layout: Layout) -> str:
    """R6: the generated passphrase, from a private regular file in the test dir."""
    try:
        fd = os.open(layout.passphrase, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise Refused("R6: the test passphrase file is missing or a symlink") from None
    with os.fdopen(fd, "r") as f:
        info = os.fstat(f.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise Refused("R6: the test passphrase file is not a private (0600) file you own")
        return f.read().strip()


def check_test_db(layout: Layout, live: Live) -> None:
    """R2 for the audit database: a regular, singly linked file in the test dir, no live DB."""
    root = Path(os.path.realpath(layout.root))
    for path in (layout.db, Path(f"{layout.db}-wal"), Path(f"{layout.db}-shm")):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            if path == layout.db:
                raise Refused("R2: the test audit database is missing; run setup again") from None
            continue
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or not _inside(Path(os.path.realpath(path)), root)
        ):
            raise Refused(f"R2: {path.name} is not a private regular file in the test directory")
        for db in live.dbs:
            with contextlib.suppress(OSError):
                if os.path.samefile(path, db):
                    raise Refused(f"R2: {path.name} is a live database")


def _git(worktree: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
        check=False,
    )
    return res.stdout.strip() if res.returncode == 0 else ""


def check_worktree(worktree: Path, layout: Layout, *, main_root: Path, running: Path) -> Path:
    """R9: a branch checkout that is not main, not the main checkout, not this harness's genesis."""
    wt = Path(os.path.realpath(worktree))
    if not (wt / SERVER_SCRIPT).is_file() or not (wt / "src" / "genesis" / "__init__.py").is_file():
        raise Refused(f"R9: {worktree} is not a Genesis checkout")
    if Path(os.path.realpath(_git(wt, "rev-parse", "--show-toplevel") or "/")) != wt:
        raise Refused(f"R9: {worktree} is not the top of a git checkout")
    if wt == Path(os.path.realpath(main_root)):
        raise Refused("R9: the worktree is the main checkout")
    branch = _git(wt, "symbolic-ref", "--short", "-q", "HEAD")
    if not branch or branch == "main":
        raise Refused("R9: the worktree must be on a branch other than main")
    if _inside(Path(os.path.realpath(running)), wt):
        raise Refused("R9: this harness runs the worktree's genesis; run it with the main venv")
    root = Path(os.path.realpath(layout.root))
    if _inside(root, wt) or _inside(wt, root):
        raise Refused("R9: the test directory and the worktree must not contain each other")
    return wt


def overlay_text(marker: str) -> str:
    return (
        f"# Written by scripts/payments_test_vault.py setup (marker {marker}); "
        "removed by its teardown.\ncard_fill:\n  enabled: true\n"
    )


def overlay_path(worktree: Path, main_root: Path) -> Path:
    """R10: the card-fill overlay, in the worktree's config/ only."""
    path = worktree / OVERLAY
    lands = Path(os.path.realpath(path.parent))
    if lands != Path(os.path.realpath(worktree)) / "config":
        raise Refused("R10: the card-fill overlay would land outside the worktree's config/")
    if _inside(lands, Path(os.path.realpath(main_root))):
        raise Refused("R10: the card-fill overlay would land in the main checkout")
    return path


# -- setup --------------------------------------------------------------------


def mcp_env(layout: Layout, worktree: Path, engine: Path) -> dict[str, str]:
    """What the test MCP's environment overrides (Claude Code passes the rest through)."""
    return {
        "HOME": str(layout.home),
        "GENESIS_HOME": str(layout.genesis_home),
        "GENESIS_DB_PATH": str(layout.db),
        "GENESIS_REPO_ROOT": str(worktree),
        "SECRETS_PATH": str(layout.secrets),
        "XDG_CACHE_HOME": str(layout.cache),
        "CAMOUFOX_EXECUTABLE_PATH": str(engine),
        "DBUS_SESSION_BUS_ADDRESS": "",
        LIVE_BOT_TOKEN_KEY: "",
        BOT_TOKEN_KEY: "",
    }


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def make_venv(layout: Layout, worktree: Path) -> None:
    """A venv importing the worktree's genesis first, then the main venv's packages."""
    base = os.path.realpath(sys.executable)
    subprocess.run(
        [base, "-m", "venv", "--without-pip", str(layout.venv)],
        check=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
    )
    purelib = subprocess.run(
        [
            str(layout.venv_python),
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        capture_output=True,
        text=True,
        env={},
        check=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
    ).stdout.strip()
    main_site = sysconfig.get_paths()["purelib"]
    Path(purelib, "genesis_test_vault.pth").write_text(
        f"{worktree / 'src'}\nimport site; site.addsitedir({main_site!r})\n"
    )
    res = subprocess.run(
        [str(layout.venv_python), "-c", "import genesis; print(genesis.__file__)"],
        capture_output=True,
        text=True,
        env={},
        cwd=layout.root,
        timeout=_SUBPROCESS_TIMEOUT_S,
        check=False,
    )
    got = Path(os.path.realpath(res.stdout.strip() or "/"))
    if res.returncode != 0 or not _inside(got, Path(os.path.realpath(worktree / "src"))):
        raise Refused("setup: the test venv does not import the worktree's genesis")


def resolve_engine() -> Path:
    from genesis.browser.engine import resolve_camoufox_engine

    status = resolve_camoufox_engine()
    if not status.ready or status.path is None:
        raise Refused(f"setup: no ready Camoufox engine to point the test MCP at ({status.detail})")
    return Path(status.path)


async def _init_db(path: Path) -> None:
    from genesis.db.connection import init_db

    db = await init_db(path)
    await db.close()


def setup(
    worktree: Path,
    layout: Layout,
    merchant: str,
    *,
    main_root: Path,
    running: Path,
    live: Live,
    engine: Path,
    venv=make_venv,
) -> dict:
    check_placement(layout, live, new=True)
    wt = check_worktree(worktree, layout, main_root=main_root, running=running)
    try:
        merchant = vs.normalise_merchant(merchant)
    except vs.VaultError as exc:
        raise Refused(f"setup: {exc}") from None
    overlay = overlay_path(wt, main_root)
    if os.path.lexists(overlay):
        raise Refused(f"R10: {overlay} already exists; it is not the harness's to replace")
    layout.root.mkdir(mode=0o700)
    marker = secrets.token_hex(16)
    _write_private(layout.marker, marker + "\n")
    _write_private(layout.state, json.dumps({"marker": marker, "phase": "setup"}))
    venv(layout, wt)
    for d in (layout.home, layout.genesis_home, layout.cache):
        d.mkdir(mode=0o700)
    passphrase = secrets.token_urlsafe(32)
    _write_private(layout.passphrase, passphrase + "\n")
    vs.init_vault(passphrase, layout.vault)
    del passphrase
    vs.add_card(  # R5: the constants above are the only card the harness holds
        vs.parse_card(TEST_NUMBER, TEST_EXPIRY, TEST_CVC),
        label=TEST_LABEL,
        merchant=merchant,
        single_use=False,
        name=TEST_NAME,
        postal_code=TEST_POSTAL,
        path=layout.vault,
    )
    asyncio.run(_init_db(layout.db))
    server = {
        "command": str(layout.venv_python),
        "args": [str(wt / SERVER_SCRIPT), "--server", "health"],
        "env": mcp_env(layout, wt, engine),
    }
    _write_private(layout.mcp_json, json.dumps({"mcpServers": {MCP_NAME: server}}, indent=2) + "\n")
    state = {
        "marker": marker,
        "phase": "setup",
        "worktree": str(wt),
        "merchant": merchant,
        "overlay": str(overlay),
    }
    # Recorded before it is written, so teardown finds an overlay a failed setup left.
    _write_private_replace(layout.state, json.dumps(state, indent=2) + "\n")
    _write_private(overlay, overlay_text(marker))
    check_vault_content(layout, merchant)
    state["phase"] = "ready"
    _write_private_replace(layout.state, json.dumps(state, indent=2) + "\n")
    return state


# -- serve: the peer check, the TEST registry, the test bot ------------------------


def other_browsers(
    proc_root: Path = Path("/proc"), *, allowed_root: int | None = None
) -> list[int]:
    """Browser processes not descended from ``allowed_root`` (the test MCP), by pid."""
    found = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().strip()
        except OSError:
            continue
        if comm not in BROWSER_COMMS:
            continue
        at, mine = int(entry.name), False
        for _ in range(32):
            if at == allowed_root:
                mine = True
                break
            try:
                raw = (proc_root / str(at) / "stat").read_text()
                at = int(raw[raw.rindex(")") + 1 :].split()[1])
            except (OSError, ValueError, IndexError):
                break
            if at <= 1:
                break
        if not mine:
            found.append(int(entry.name))
    return found


def check_child_env(proc_root: Path, pid: int, layout: Layout, worktree: Path) -> str | None:
    """R7 on the running test MCP: the first problem found, as a fixed name, or None."""
    try:
        raw = (proc_root / str(pid) / "environ").read_bytes()
    except OSError:
        return "environ unreadable"
    env = {}
    for entry in raw.split(b"\0"):
        key, sep, value = entry.partition(b"=")
        if sep:
            env[key.decode("utf-8", "replace")] = value
    del raw
    root = Path(os.path.realpath(layout.root))
    for name in ("HOME", "GENESIS_HOME", "GENESIS_DB_PATH", "XDG_CACHE_HOME", "SECRETS_PATH"):
        value = os.fsdecode(env.get(name, b""))
        if not value or not _inside(Path(os.path.realpath(value)), root):
            return f"{name} outside the test directory"
    repo = os.fsdecode(env.get("GENESIS_REPO_ROOT", b""))
    if not repo or Path(os.path.realpath(repo)) != Path(os.path.realpath(worktree)):
        return "GENESIS_REPO_ROOT is not the worktree"
    if env.get("DBUS_SESSION_BUS_ADDRESS"):
        return "DBUS_SESSION_BUS_ADDRESS is set"
    if any(n.startswith("PYTHON") for n in env):
        return "a PYTHON* variable is set"
    if any("BOT_TOKEN" in n and v for n, v in env.items()):
        return "a bot token is set"
    return None


def make_release_peer(
    layout: Layout,
    worktree: Path,
    *,
    server_pid: int,
    ancestor_comm: str = "claude",
    proc_root: Path = Path("/proc"),
):
    """The test vault's release peer check: R8's policy, then R7 and Q16."""
    policy = PeerPolicy(
        python=str(layout.venv_python),
        script=str(worktree / SERVER_SCRIPT),
        ancestor_comm=ancestor_comm,
        proc_root=proc_root,
    )
    live = PeerPolicy.default()  # what the live vault admits: the main venv and main script
    if policy.python == live.python or os.path.realpath(policy.script) == os.path.realpath(
        live.script
    ):
        raise Refused("R8: the test peer policy would admit the live MCPs")

    def check(pid: int, *, accept_ticks: int):
        peer = check_release_peer(
            pid, policy=policy, server_pid=server_pid, accept_ticks=accept_ticks
        )
        problem = check_child_env(proc_root, pid, layout, worktree)
        if problem:
            raise PeerRefused("test env", {"check": "test env", "problem": problem})
        if other_browsers(proc_root, allowed_root=pid):
            raise PeerRefused("another browser", {"check": "another browser is open"})
        return peer

    return check


def carries_test_line(text: str) -> bool:
    return TEST_LINE in str(text).split("\n")[:2]


class TestOnlyBot:
    """The bot the test registry holds: it sends nothing that lacks the TEST line."""

    def __init__(self, bot) -> None:
        self._bot = bot

    @property
    def request(self):  # send_with_client_heal re-initialises a closed client through it
        return self._bot.request

    async def send_message(self, *, chat_id, text, **kwargs):
        if not carries_test_line(text):
            raise RuntimeError("refused: a test-bot message without the TEST line")
        return await self._bot.send_message(chat_id=chat_id, text=text, **kwargs)


class TestApprovalRegistry(PaymentApprovalRegistry):
    """The real registry, with the TEST field first, the test card only, and a capped wait."""

    def __init__(self, *, timeout_cap_s: float) -> None:
        super().__init__(timeout_s=timeout_cap_s)
        self._cap = timeout_cap_s

    def attach_bot(self, bot, owner_id: int, *, stopping=None) -> None:
        super().attach_bot(TestOnlyBot(bot), owner_id, stopping=stopping)

    async def request(self, fields, *, audit_detail=None, timeout_s=None) -> str:
        if TEST_KEY in fields or fields.get("Card") != f"{TEST_LABEL} ending {TEST_NUMBER[-4:]}":
            return REFUSED
        shown = {TEST_KEY: TEST_VALUE, **fields}
        wait = self._cap if timeout_s is None else min(timeout_s, self._cap)
        return await super().request(shown, audit_detail=audit_detail, timeout_s=wait)

    async def notify(self, text: str) -> bool:
        return await super().notify(f"{TEST_LINE}\n{text}")


def read_bot_secrets(path: Path, env: dict | None = None) -> tuple[str, int]:
    """The test bot's token and the owner id; refused when it is any other bot's token.

    Every other ``*BOT_TOKEN*`` value, in secrets.env or the environment, is
    compared by bot id (only the id is kept), and the live bot's token must be
    found: a check that cannot see it cannot tell the two bots apart.
    """
    env = os.environ if env is None else env
    from genesis.channels.bridge_config import build_bridge_config, parse_secrets_env_text

    try:
        values = parse_secrets_env_text(path.read_text())
    except OSError as exc:
        raise Refused(f"serve: secrets.env cannot be read ({exc.strerror})") from None
    token = values.get(BOT_TOKEN_KEY, "")
    bot_id, sep, rest = token.partition(":")
    if not (bot_id.isdigit() and sep and rest):
        raise Refused(f"serve: {BOT_TOKEN_KEY} is not set to a bot token in secrets.env")
    others = [
        (k, v)
        for src in (values, env)
        for k, v in src.items()
        if "BOT_TOKEN" in k and k != BOT_TOKEN_KEY
    ]
    if not any(k == LIVE_BOT_TOKEN_KEY and v for k, v in others):
        raise Refused(
            f"serve: {LIVE_BOT_TOKEN_KEY} was not found, so the test bot cannot be told apart"
        )
    other_ids = {v.partition(":")[0] for _k, v in others}
    del others
    for key in [k for k in values if "BOT_TOKEN" in k and k != BOT_TOKEN_KEY]:
        values.pop(key)
    if bot_id in other_ids:
        raise Refused(f"serve: {BOT_TOKEN_KEY} is another bot's token; create a separate test bot")
    config = build_bridge_config(
        {
            "TELEGRAM_BOT_TOKEN": token,
            "TELEGRAM_ALLOWED_USERS": values.get("TELEGRAM_ALLOWED_USERS", ""),
        }
    )
    owner = (config or {}).get("allowed_users_ordered") or ()
    if not owner:
        raise Refused("serve: TELEGRAM_ALLOWED_USERS has no owner id first")
    return token, owner[0]


def make_tap_handler(registry):
    """The callback handler: genesis-server's own routing, for this registry only."""
    from genesis.channels.telegram._handler_context import HandlerContext
    from genesis.channels.telegram._handler_messages import handle_callback_query

    ctx = HandlerContext(
        loop=None, allowed_users=set(), whisper_model="", payment_approvals=registry
    )

    async def on_callback(update, context) -> None:
        await handle_callback_query(ctx, update, context)

    return on_callback


class TelegramSide:
    """A python-telegram-bot Application for the TEST bot: one callback handler, polled."""

    def __init__(self, token: str) -> None:
        self._token = token
        self.app = None
        self.stopping = False

    async def start(self, handler) -> object:
        from telegram.ext import Application, CallbackQueryHandler

        # httpx logs each Bot API URL, token included, at INFO.
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)

        self.app = Application.builder().token(self._token).build()
        self.app.add_handler(CallbackQueryHandler(handler, pattern=r"^payapprove:"))
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(
            allowed_updates=["callback_query"], drop_pending_updates=True
        )
        return self.app.bot

    async def stop(self) -> None:
        self.stopping = True
        if self.app is None:
            return
        if self.app.updater.running:
            await self.app.updater.stop()
        if self.app.running:
            await self.app.stop()
        await self.app.shutdown()


async def run_serve(
    layout: Layout,
    state: dict,
    *,
    live: Live,
    telegram,
    owner: int,
    timeout_cap_s: float,
    stop: asyncio.Event,
    release_peer=None,
    on_ready=None,
) -> None:
    """The test vault service and its bot, until ``stop`` is set."""
    from genesis.db.connection import get_db
    from genesis.payments.vault_service import VaultService

    worktree = Path(state["worktree"])
    registry = TestApprovalRegistry(timeout_cap_s=timeout_cap_s)
    release_peer = release_peer or make_release_peer(layout, worktree, server_pid=os.getpid())
    check_test_db(layout, live)
    db = await get_db(layout.db)
    service = VaultService(
        db,
        vault_path=layout.vault,
        socket_path=layout.socket,
        approvals=registry,
        release_peer=release_peer,
    )
    started = False
    try:
        check_socket(layout, live)  # again, right before the bind
        await service.start()
        started = True
        registry.attach_audit(service._audit)
        unlocked = await service.unlock(read_passphrase(layout), None)
        if not unlocked.get("ok"):
            raise Refused(f"serve: the test vault did not unlock ({unlocked.get('error')})")
        bot = await telegram.start(make_tap_handler(registry))
        registry.attach_bot(bot, owner, stopping=lambda: telegram.stopping)
        if on_ready is not None:
            await on_ready(service, registry)
        await stop.wait()
    finally:
        await telegram.stop()
        if started:
            await service.stop()
        await db.close()


# -- teardown -------------------------------------------------------------------


def _is_this_serve(pid: int, layout: Layout, proc_root: Path) -> bool:
    """Whether ``pid``'s own command line is ``payments_test_vault.py serve --home <this root>``.

    serve.pid sits in a directory the code under test can write, so the pid it
    names is checked against the process itself before anything is signalled.
    """
    try:
        argv = [
            os.fsdecode(a) for a in (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0")
        ]
        cwd = os.readlink(proc_root / str(pid) / "cwd")
    except OSError:
        return False
    argv = [a for a in argv if a]
    script = next((i for i, a in enumerate(argv) if Path(a).name == Path(__file__).name), None)
    if script is None or argv[script + 1 : script + 2] != ["serve"]:
        return False
    rest = argv[script + 2 :]
    homes = [rest[i + 1] for i, a in enumerate(rest[:-1]) if a == "--home"]
    homes += [a.split("=", 1)[1] for a in rest if a.startswith("--home=")]
    if len(homes) != 1:
        return False
    home = Path(homes[0]).expanduser()
    return layout_for(home if home.is_absolute() else Path(cwd) / home).root == layout.root


def stop_running_serve(
    layout: Layout, *, server_pid: int | None = None, proc_root: Path = Path("/proc")
) -> None:
    try:
        pid_text, start_text = layout.pidfile.read_text().split()
        pid, start = int(pid_text), int(start_text)
    except (OSError, ValueError):
        return
    if start_of(pid, proc_root) != start:
        return
    if pid <= 1 or pid in (os.getpid(), server_pid) or not _is_this_serve(pid, layout, proc_root):
        raise Refused(
            "teardown: serve.pid names a process that is not this directory's serve; "
            "nothing was signalled or removed"
        )
    os.kill(pid, signal.SIGINT)
    deadline = time.monotonic() + _SERVE_STOP_WAIT_S
    while time.monotonic() < deadline:
        if start_of(pid) != start:
            return
        time.sleep(0.2)
    raise Refused(f"teardown: the serve (pid {pid}) did not stop; nothing was removed")


_ROOT_ENTRIES = frozenset(
    {MARKER, "state.json", "passphrase", "serve.pid", "mcp.json", "venv", "cache", "home"}
)


def teardown(
    layout: Layout, *, live: Live, main_root: Path, server_pid: int | None = None
) -> list[str]:
    from genesis.trash import TrashRefused, trash

    check_placement(layout, live, new=False)
    state = json.loads(layout.state.read_text())
    extra = sorted(e.name for e in layout.root.iterdir() if e.name not in _ROOT_ENTRIES)
    if extra:
        raise Refused(f"R2: {layout.root} holds entries setup never made ({', '.join(extra)})")
    stop_running_serve(layout, server_pid=server_pid)
    done = []
    if state.get("overlay"):
        overlay = Path(state["overlay"])
        if overlay != overlay_path(Path(state["worktree"]), main_root):  # R10 again
            raise Refused("R10: the state file names an overlay that is not the worktree's")
        try:
            ours = overlay.read_text() == overlay_text(state["marker"])
        except OSError:
            ours = False
        if ours:
            overlay.unlink()
            done.append(f"removed {overlay}")
        elif overlay.exists():
            done.append(f"left {overlay} in place: it changed since setup")
    try:
        trash(
            layout.root, reason="payment test-vault harness teardown", caller="payments_test_vault"
        )
    except TrashRefused as exc:
        raise Refused(f"teardown: the trash refused {layout.root} ({exc})") from None
    done.append(f"moved {layout.root} to the Genesis trash")
    return done


# -- command line -------------------------------------------------------------------


def _main_root() -> Path:
    import genesis

    return Path(genesis.__file__).resolve().parents[2]


def _running() -> Path:
    import genesis

    return Path(genesis.__file__).resolve()


def _cli_live(layout: Layout, *, exclude: bool) -> tuple[Live, int | None]:
    """R1 for the command line, with the live database, the degraded sources printed."""
    from genesis.env import genesis_db_path

    server_pid = genesis_server_pid()
    notes: list[str] = []
    live = live_paths(
        server_pid=server_pid,
        exclude_socket=layout.socket if exclude else None,
        notes=notes,
        extra_dbs=(genesis_db_path(),),
    )
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    return live, server_pid


def _serve(layout: Layout, timeout_cap_s: float) -> None:
    from genesis.env import secrets_path

    live, _server = _cli_live(layout, exclude=True)
    check_placement(layout, live, new=False)
    state = json.loads(layout.state.read_text())
    if state.get("phase") != "ready":
        raise Refused("serve: setup did not finish; run teardown and setup again")
    worktree = check_worktree(
        Path(state["worktree"]), layout, main_root=_main_root(), running=_running()
    )
    overlay_path(worktree, _main_root())
    check_vault_content(layout, state["merchant"])
    check_socket(layout, live)
    check_test_db(layout, live)
    read_passphrase(layout)
    if other_browsers():
        raise Refused(
            "Q16: a browser is open in another session; close it (or wait for its "
            "idle reclaim) before a test run"
        )
    token, owner = read_bot_secrets(secrets_path())
    # Belt and braces: any default path the vault code resolves is the test home's.
    os.environ["GENESIS_HOME"] = str(layout.genesis_home)
    os.environ["GENESIS_DB_PATH"] = str(layout.db)
    _write_private_replace(layout.pidfile, f"{os.getpid()} {start_of(os.getpid())}\n")

    async def ready(_service, _registry) -> None:
        print(
            f"Test vault serving on {layout.socket} (approvals by the TEST bot, "
            f"wait capped at {int(timeout_cap_s)} s). Start the test session yourself:"
        )
        print(f"  cd {worktree} && claude --strict-mcp-config --mcp-config {layout.mcp_json}")
        print("Ctrl-C stops the test vault.")

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await run_serve(
            layout,
            state,
            live=live,
            telegram=TelegramSide(token),
            owner=owner,
            timeout_cap_s=timeout_cap_s,
            stop=stop,
            on_ready=ready,
        )

    try:
        asyncio.run(run())
    finally:
        layout.pidfile.unlink(missing_ok=True)


def _write_private_replace(path: Path, text: str) -> None:
    path.unlink(missing_ok=True)
    _write_private(path, text)


def build_parser() -> argparse.ArgumentParser:
    """The whole command line. No option takes card data (R5)."""
    parser = argparse.ArgumentParser(
        prog="payments_test_vault.py", description=__doc__.split("\n")[0]
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("setup", help="build a new test-vault directory")
    p.add_argument("--worktree", required=True, help="the branch worktree the test MCP runs")
    p.add_argument("--home", required=True, help="a NEW directory for the test vault")
    p.add_argument(
        "--merchant",
        required=True,
        help="the registrable domain the test checkout's PAYMENT page shows",
    )
    p = sub.add_parser("serve", help="run the test vault and the test bot (foreground)")
    p.add_argument("--home", required=True)
    p.add_argument(
        "--approval-timeout-s",
        type=int,
        default=APPROVAL_TIMEOUT_MAX_S,
        help=f"cap the approval wait ({APPROVAL_TIMEOUT_MIN_S}-{APPROVAL_TIMEOUT_MAX_S})",
    )
    p = sub.add_parser("teardown", help="stop a serve, remove the overlay, trash the directory")
    p.add_argument("--home", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    layout = layout_for(args.home)
    try:
        if args.cmd == "setup":
            live, _server = _cli_live(layout, exclude=False)
            state = setup(
                Path(args.worktree),
                layout,
                args.merchant,
                main_root=_main_root(),
                running=_running(),
                live=live,
                engine=resolve_engine(),
            )
            print(
                f"Test vault ready in {layout.root} (card {TEST_LABEL}, merchant "
                f"{state['merchant']}). Next: serve --home {layout.root}"
            )
        elif args.cmd == "serve":
            if not APPROVAL_TIMEOUT_MIN_S <= args.approval_timeout_s <= APPROVAL_TIMEOUT_MAX_S:
                raise Refused("serve: --approval-timeout-s is out of range")
            _serve(layout, float(args.approval_timeout_s))
        else:
            live, server_pid = _cli_live(layout, exclude=True)
            for line in teardown(layout, live=live, main_root=_main_root(), server_pid=server_pid):
                print(line)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except (vs.VaultError, subprocess.SubprocessError, OSError) as exc:  # never a card value
        print(f"refused: {args.cmd}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
