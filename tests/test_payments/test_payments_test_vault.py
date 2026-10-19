"""The TEST-vault harness (scripts/payments_test_vault.py): refusals R1-R10, the TEST
label, and a fake-bot run end to end.

The end-to-end tests run the harness's real ``setup`` (a real venv, vault, audit
database and git worktree, all under tmp) and its real ``run_serve`` with a fake
Telegram side. The test MCP is a stand-in child run as ``<test venv python>
<worktree>/scripts/genesis_mcp_server.py --server health`` with the environment
``mcp.json`` gives it, so the real /proc peer check, the harness's R7 environment
check and the real socket all run. The tap goes through the harness's handler,
which is genesis-server's ``handle_callback_query``. The child prints which
fields it received and whether the number was the test card's, never a value.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from genesis.db.crud import payment_vault_events as audit
from genesis.payments import approval as ap
from genesis.payments import vault_store as vs
from genesis.payments.peer_check import ARGV_MISMATCH, PeerPolicy, PeerRefused
from tests.conftest import private_module

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "payments_test_vault.py"
H = private_module("payments_test_vault_under_test", SCRIPT)

OWNER = 111
MERCHANT = "stripe.dev"
CLAIM = "harness-claim-secret"
COMM = Path("/proc/self/comm").read_text().strip()  # this process stands in for claude

CLIENT = r"""
import json, os, socket, sys, time

if "REQUEST" not in os.environ:  # started only to be peer-checked
    time.sleep(60)
    sys.exit(0)
SOCK = os.path.join(os.environ["GENESIS_HOME"], "run", "payment_vault.sock")

def ask(payload):
    s = socket.socket(socket.AF_UNIX)
    s.connect(SOCK)
    s.sendall((json.dumps(payload) + "\n").encode())
    data = b""
    while not data.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    return json.loads(data)

out = {}
r = ask(json.loads(os.environ["REQUEST"]))
out["request"] = "ok" if r.get("ok") else r.get("error")
if r.get("ok"):
    nonce, state = r["nonce"], "pending"
    deadline = time.monotonic() + 30
    while state == "pending" and time.monotonic() < deadline:
        time.sleep(0.02)
        state = ask({"op": "release_status", "nonce": nonce})["state"]
    out["status"] = state
    c = ask({"op": "release_claim", "nonce": nonce, "claim": os.environ["CLAIM"]})
    card = c.get("card", {})
    out["claim_ok"] = c.get("ok")
    out["fields"] = sorted(card)
    out["is_test_card"] = card.get("number") == "4242" * 4
    out["label"] = card.get("label")
    del c, card
print(json.dumps(out))
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="module")
def checkouts(tmp_path_factory):
    """A fake main checkout and a linked worktree of it on a feature branch."""
    base = tmp_path_factory.mktemp("repos")
    main = base / "main"
    (main / "scripts").mkdir(parents=True)
    (main / "src" / "genesis").mkdir(parents=True)
    (main / "config").mkdir()
    (main / "scripts" / "genesis_mcp_server.py").write_text(CLIENT)
    (main / "src" / "genesis" / "__init__.py").write_text("")
    (main / "config" / ".keep").write_text("")
    _git(main, "init", "-q", "-b", "main")
    _git(main, "add", ".")
    _git(main, "commit", "-q", "-m", "init")
    wt = base / "wt"
    _git(main, "worktree", "add", "-q", "-b", "feat/b1", str(wt))
    return main, wt


def _live(fake_home: Path) -> object:
    return H.live_paths(env={"HOME": str(fake_home)})


def _setup(tmp: Path, checkouts, merchant: str = MERCHANT):
    main, wt = checkouts
    layout = H.layout_for(tmp / "testvault")
    engine = tmp / "engine" / "camoufox-bin"
    state = H.setup(
        wt,
        layout,
        merchant,
        main_root=main,
        running=Path(H.__file__),
        live=_live(tmp / "fakehome"),
        engine=engine,
    )
    return layout, state


@pytest.fixture(scope="module")
def built(tmp_path_factory, checkouts):
    tmp = tmp_path_factory.mktemp("built")
    layout, state = _setup(tmp, checkouts)
    yield layout, state
    Path(state["overlay"]).unlink(missing_ok=True)


# -- R1 ------------------------------------------------------------------------


def _fake_proc(root: Path, unix_rows: list[str]) -> Path:
    (root / "net").mkdir(parents=True)
    head = "Num       RefCount Protocol Flags    Type St Inode Path\n"
    (root / "net" / "unix").write_text(head + "".join(r + "\n" for r in unix_rows))
    return root


def test_r1_live_paths_come_from_listening_sockets_and_the_server(tmp_path):
    proc = _fake_proc(
        tmp_path / "proc",
        [
            "0000: 00000002 00000000 00010000 0001 01 101 /srv/a/.genesis/run/payment_vault.sock",
            "0000: 00000002 00000000 00000000 0001 03 102 /srv/b/.genesis/run/payment_vault.sock",
            "0000: 00000002 00000000 00010000 0001 01 103 /srv/c/other.sock",
        ],
    )
    (proc / "77").mkdir()
    (proc / "77" / "environ").write_bytes(b"A=1\0GENESIS_HOME=/srv/server-home\0SECRET=x\0")
    notes: list[str] = []
    live = H.live_paths(
        env={"HOME": "/srv/op", "GENESIS_HOME": "/srv/op-gh"},
        proc_root=proc,
        server_pid=77,
        notes=notes,
    )
    homes = {str(h) for h in live.homes}
    assert {"/srv/a/.genesis", "/srv/server-home", "/srv/op/.genesis", "/srv/op-gh"} <= homes
    assert "/srv/b/.genesis" not in homes  # not listening
    assert Path("/srv/a/.genesis/run/payment_vault.sock") in live.sockets
    excluded = H.live_paths(
        env={}, proc_root=proc, exclude_socket=Path("/srv/a/.genesis/run/payment_vault.sock")
    )
    assert "/srv/a/.genesis" not in {str(h) for h in excluded.homes}
    assert not notes


# -- R2 ------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["equal", "inside", "holds"])
def test_r2_a_test_dir_overlapping_a_live_home_is_refused(tmp_path, where):
    home = tmp_path / "op" / ".genesis"
    home.mkdir(parents=True)
    root = {"equal": home, "inside": home / "t", "holds": tmp_path / "op"}[where]
    with pytest.raises(H.Refused, match="^R2: .* overlaps a live Genesis home"):
        H.check_placement(H.layout_for(root), _live(tmp_path / "op"), new=True)


def test_r2_setup_needs_a_new_dir_and_serve_needs_the_marker(tmp_path):
    root = tmp_path / "t"
    root.mkdir()
    live = _live(tmp_path / "op")
    with pytest.raises(H.Refused, match="^R2: .* already exists"):
        H.check_placement(H.layout_for(root), live, new=True)
    with pytest.raises(H.Refused, match="^R2: .* no harness marker"):
        H.check_placement(H.layout_for(root), live, new=False)
    (root / H.MARKER).write_text("abc\n")
    (root / "state.json").write_text(json.dumps({"marker": "other"}))
    with pytest.raises(H.Refused, match="^R2: .* does not match"):
        H.check_placement(H.layout_for(root), live, new=False)
    (root / "state.json").write_text(json.dumps({"marker": "abc"}))
    H.check_placement(H.layout_for(root), live, new=False)


# -- R3 ------------------------------------------------------------------------


def _test_root(tmp_path: Path) -> H.Layout:
    layout = H.layout_for(tmp_path / "t")
    layout.root.mkdir()
    return layout


def test_r3_a_run_dir_linked_to_a_live_run_dir_is_refused(tmp_path):
    live_run = tmp_path / "op" / ".genesis" / "run"
    live_run.mkdir(parents=True)
    layout = _test_root(tmp_path)
    layout.genesis_home.mkdir(parents=True)
    os.symlink(live_run, layout.socket.parent)
    with pytest.raises(H.Refused, match="^R3: the test socket's directory resolves outside"):
        H.check_socket(layout, _live(tmp_path / "op"))


def test_r3_a_hard_link_to_a_live_socket_is_refused(tmp_path):
    live_sock = tmp_path / "op" / ".genesis" / "run" / H.SOCKET_NAME
    live_sock.parent.mkdir(parents=True)
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(live_sock))  # bound, not listening: a socket FILE with an inode
    try:
        layout = _test_root(tmp_path)
        layout.socket.parent.mkdir(parents=True)
        os.link(live_sock, layout.socket)
        with pytest.raises(H.Refused, match="^R3: the file at the test socket path is a live"):
            H.check_socket(layout, _live(tmp_path / "op"))
    finally:
        s.close()


def test_r3_a_listener_or_a_non_socket_at_the_test_path_is_refused(tmp_path):
    layout = _test_root(tmp_path)
    layout.socket.parent.mkdir(parents=True)
    layout.socket.write_text("x")
    live = _live(tmp_path / "op")
    with pytest.raises(H.Refused, match="^R3: something other than a socket"):
        H.check_socket(layout, live)
    layout.socket.unlink()
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(layout.socket))
    s.listen()
    try:
        with pytest.raises(H.Refused, match="^R3: something already listens"):
            H.check_socket(layout, live)
    finally:
        s.close()
        layout.socket.unlink()
    H.check_socket(layout, live)  # nothing there: allowed


# -- R4 ------------------------------------------------------------------------


def _copy_vault(built, tmp_path: Path) -> H.Layout:
    layout = H.layout_for(tmp_path / "copy")
    shutil.copytree(built[0].vault, layout.vault)
    return layout


def test_r4_the_setup_vault_passes(built):
    layout, state = built
    assert vs.CARD_REF.fullmatch(H.check_vault_content(layout, state["merchant"]))


def test_r4_a_second_card_is_refused(built, tmp_path):
    layout = _copy_vault(built, tmp_path)
    vs.add_card(
        vs.parse_card("4000002500003155", "12/34", "123"),
        label="TEST 3155",
        merchant=MERCHANT,
        single_use=False,
        path=layout.vault,
    )
    with pytest.raises(H.Refused, match="^R4: the test vault holds 2 cards"):
        H.check_vault_content(layout, MERCHANT)


def test_r4_another_label_or_merchant_is_refused(built, tmp_path):
    layout = _copy_vault(built, tmp_path)
    with pytest.raises(H.Refused, match="^R4: .* not the TEST 4242 card"):
        H.check_vault_content(layout, "example.com")
    import sqlite3

    with sqlite3.connect(layout.vault / vs.CARDS) as conn:
        conn.execute("UPDATE cards SET label = 'Personal'")
    with pytest.raises(H.Refused, match="^R4: .* not the TEST 4242 card"):
        H.check_vault_content(layout, MERCHANT)


# -- R5 ------------------------------------------------------------------------


def test_r5_no_argument_can_bring_a_card_in():
    """Every option the CLI takes, by subcommand: none carries card data."""
    parser = H.build_parser()
    subs = parser._subparsers._group_actions[0].choices
    parser_opts = {
        name: {o for a in sub._actions for o in a.option_strings} for name, sub in subs.items()
    }
    assert {o for a in parser._actions for o in a.option_strings} == {"-h", "--help"}
    assert parser_opts == {
        "setup": {"-h", "--help", "--worktree", "--home", "--merchant"},
        "serve": {"-h", "--help", "--home", "--approval-timeout-s"},
        "teardown": {"-h", "--help", "--home"},
    }


def test_r5_the_only_card_shaped_literal_is_the_stripe_test_card():
    tree = ast.parse(SCRIPT.read_text())
    runs = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and ap.holds_long_digit_run(node.value)
    }
    assert runs == {H.TEST_NUMBER}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "input" not in names and not {"stdin", "getpass"} & attrs  # nothing is read in


# -- R6 ------------------------------------------------------------------------


def test_r6_the_passphrase_file_must_be_private_and_not_a_link(tmp_path):
    layout = _test_root(tmp_path)
    other = tmp_path / "elsewhere"
    other.write_text("p\n")
    os.symlink(other, layout.passphrase)
    with pytest.raises(H.Refused, match="^R6: .* missing or a symlink"):
        H.read_passphrase(layout)
    layout.passphrase.unlink()
    layout.passphrase.write_text("p\n")
    os.chmod(layout.passphrase, 0o644)
    with pytest.raises(H.Refused, match="^R6: .* not a private"):
        H.read_passphrase(layout)
    os.chmod(layout.passphrase, 0o600)
    assert H.read_passphrase(layout) == "p"


def test_r6_setup_writes_a_private_generated_passphrase(built):
    layout, _ = built
    assert oct(os.stat(layout.passphrase).st_mode & 0o777) == "0o600"
    assert len(H.read_passphrase(layout)) >= 40


# -- R7 ------------------------------------------------------------------------


def _env_proc(tmp_path: Path, env: dict[str, str]) -> Path:
    proc = tmp_path / "proc"
    (proc / "5").mkdir(parents=True)
    (proc / "5" / "environ").write_bytes(
        b"".join(f"{k}={v}".encode() + b"\0" for k, v in env.items())
    )
    return proc


def _good_env(layout: H.Layout, wt: Path) -> dict[str, str]:
    env = H.mcp_env(layout, wt, Path("/x/camoufox-bin"))
    env["PATH"] = "/usr/bin"
    return env


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"HOME": "/home/someone"}, "HOME outside the test directory"),
        ({"GENESIS_HOME": ""}, "GENESIS_HOME outside the test directory"),
        ({"GENESIS_DB_PATH": "/data/genesis.db"}, "GENESIS_DB_PATH outside the test directory"),
        ({"SECRETS_PATH": "/repo/secrets.env"}, "SECRETS_PATH outside the test directory"),
        ({"GENESIS_REPO_ROOT": "/repo"}, "GENESIS_REPO_ROOT is not the worktree"),
        ({"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/bus"}, "DBUS_SESSION_BUS_ADDRESS is set"),
        ({"PYTHONPATH": "/x"}, "a PYTHON* variable is set"),
        ({"TELEGRAM_BOT_TOKEN": "1:x"}, "a bot token is set"),
        ({"OTHER_BOT_TOKEN": "1:x"}, "a bot token is set"),
    ],
)
def test_r7_the_test_mcp_environment_is_checked(tmp_path, checkouts, change, problem):
    layout = _test_root(tmp_path)
    wt = checkouts[1]
    assert (
        H.check_child_env(_env_proc(tmp_path / "ok", _good_env(layout, wt)), 5, layout, wt) is None
    )
    env = {**_good_env(layout, wt), **change}
    assert H.check_child_env(_env_proc(tmp_path, env), 5, layout, wt) == problem


def test_r7_mcp_json_overrides_every_checked_name(built):
    layout, state = built
    server = json.loads(layout.mcp_json.read_text())["mcpServers"][H.MCP_NAME]
    env = server["env"]
    for name in ("HOME", "GENESIS_HOME", "GENESIS_DB_PATH", "XDG_CACHE_HOME", "SECRETS_PATH"):
        assert Path(env[name]).is_relative_to(layout.root)
    assert env["GENESIS_REPO_ROOT"] == state["worktree"]
    assert env["DBUS_SESSION_BUS_ADDRESS"] == env["TELEGRAM_BOT_TOKEN"] == ""
    assert env[H.BOT_TOKEN_KEY] == ""
    assert server["command"] == str(layout.venv_python)
    assert server["args"] == [str(Path(state["worktree"]) / H.SERVER_SCRIPT), "--server", "health"]
    assert oct(os.stat(layout.mcp_json).st_mode & 0o777) == "0o600"


# -- R8 ------------------------------------------------------------------------


async def _sleeper(argv0: str, script: Path, env: dict) -> asyncio.subprocess.Process:
    script.parent.mkdir(parents=True, exist_ok=True)
    if not script.exists():
        script.write_text("import time\ntime.sleep(60)\n")
    return await asyncio.create_subprocess_exec(argv0, str(script), "--server", "health", env=env)


async def test_r8_peer_refusal_runs_both_ways(built, tmp_path):
    from genesis.payments.peer_check import accept_ticks, check_release_peer

    layout, state = built
    wt = Path(state["worktree"])
    env = {**_good_env(layout, wt), "PATH": os.environ.get("PATH", "")}
    test_check = H.make_release_peer(layout, wt, server_pid=os.getppid(), ancestor_comm=COMM)
    live_script = tmp_path / "main" / "scripts" / "genesis_mcp_server.py"
    live_policy = PeerPolicy(python=sys.executable, script=str(live_script), ancestor_comm=COMM)
    test_mcp = await _sleeper(str(layout.venv_python), wt / H.SERVER_SCRIPT, env)
    live_mcp = await _sleeper(sys.executable, live_script, env)
    try:
        await asyncio.sleep(0.3)
        assert test_check(test_mcp.pid, accept_ticks=accept_ticks()).pid == test_mcp.pid
        with pytest.raises(PeerRefused) as refused:
            test_check(live_mcp.pid, accept_ticks=accept_ticks())
        assert refused.value.reason == ARGV_MISMATCH  # the test vault refuses a live MCP
        with pytest.raises(PeerRefused) as refused:
            check_release_peer(
                test_mcp.pid, policy=live_policy, server_pid=0, accept_ticks=accept_ticks()
            )
        assert refused.value.reason == ARGV_MISMATCH  # the live vault refuses the test MCP
    finally:
        for p in (test_mcp, live_mcp):
            p.kill()
            await p.wait()


def test_r8_a_policy_naming_the_live_server_is_refused(built, monkeypatch):
    layout, state = built
    wt = Path(state["worktree"])
    same = PeerPolicy(python=str(layout.venv_python), script="/elsewhere/s.py")
    monkeypatch.setattr(H.PeerPolicy, "default", classmethod(lambda cls: same))
    with pytest.raises(H.Refused, match="^R8:"):
        H.make_release_peer(layout, wt, server_pid=1)
    same_script = PeerPolicy(python="/other/python", script=str(wt / H.SERVER_SCRIPT))
    monkeypatch.setattr(H.PeerPolicy, "default", classmethod(lambda cls: same_script))
    with pytest.raises(H.Refused, match="^R8:"):
        H.make_release_peer(layout, wt, server_pid=1)


# -- R9 ------------------------------------------------------------------------


def test_r9_the_worktree_rules(tmp_path, checkouts):
    main, wt = checkouts
    layout = H.layout_for(tmp_path / "t")
    ok = H.check_worktree(wt, layout, main_root=main, running=Path(H.__file__))
    assert ok == Path(os.path.realpath(wt))
    with pytest.raises(H.Refused, match="^R9: the worktree is the main checkout"):
        H.check_worktree(main, layout, main_root=main, running=Path(H.__file__))
    with pytest.raises(H.Refused, match="^R9: this harness runs the worktree's genesis"):
        H.check_worktree(wt, layout, main_root=main, running=wt / "src" / "genesis" / "__init__.py")
    with pytest.raises(H.Refused, match="^R9: the test directory and the worktree"):
        H.check_worktree(wt, H.layout_for(wt / "inside"), main_root=main, running=Path(H.__file__))
    with pytest.raises(H.Refused, match="^R9: .* not a Genesis checkout"):
        H.check_worktree(tmp_path, layout, main_root=main, running=Path(H.__file__))


def test_r9_a_worktree_on_main_is_refused(tmp_path, checkouts):
    main, _ = checkouts
    other = tmp_path / "onmain"
    _git(main, "worktree", "add", "-q", "--detach", str(other))
    try:
        with pytest.raises(
            H.Refused, match="^R9: the worktree must be on a branch other than main"
        ):
            H.check_worktree(
                other,
                H.layout_for(tmp_path / "t"),
                main_root=tmp_path / "x",
                running=Path(H.__file__),
            )
    finally:
        _git(main, "worktree", "remove", "--force", str(other))


# -- R10 -------------------------------------------------------------------------


def test_r10_the_overlay_is_only_in_the_worktree(tmp_path, checkouts, built):
    main, wt = checkouts
    _layout, state = built
    assert Path(state["overlay"]) == Path(os.path.realpath(wt)) / H.OVERLAY
    assert Path(state["overlay"]).read_text() == H.overlay_text(state["marker"])
    fake = tmp_path / "fakewt"
    fake.mkdir()
    os.symlink(main / "config", fake / "config")
    with pytest.raises(H.Refused, match="^R10: .* outside the worktree's config/"):
        H.overlay_path(fake, main)
    elsewhere = tmp_path / "fakewt2"
    elsewhere.mkdir()
    (tmp_path / "other-config").mkdir()
    os.symlink(tmp_path / "other-config", elsewhere / "config")
    with pytest.raises(H.Refused, match="^R10: .* outside the worktree's config/"):
        H.overlay_path(elsewhere, main)  # not the main checkout, still not the worktree's
    nested = main / "nestedwt"
    (nested / "config").mkdir(parents=True)
    try:
        with pytest.raises(H.Refused, match="^R10: .* in the main checkout"):
            H.overlay_path(nested, main)
    finally:
        shutil.rmtree(nested)


async def test_the_telegram_side_polls_callbacks_only(monkeypatch):
    from telegram.ext import Application, CallbackQueryHandler, Updater

    for owner, name in (
        (Application, "initialize"),
        (Application, "start"),
        (Application, "stop"),
        (Application, "shutdown"),
        (Updater, "start_polling"),
        (Updater, "stop"),
    ):
        monkeypatch.setattr(owner, name, AsyncMock())
    side = H.TelegramSide("901:test-token")

    async def handler(update, context):
        return None

    bot = await side.start(handler)
    assert bot is side.app.bot and bot.token == "901:test-token"
    handlers = [h for group in side.app.handlers.values() for h in group]
    assert len(handlers) == 1 and isinstance(handlers[0], CallbackQueryHandler)
    assert handlers[0].callback is handler and handlers[0].pattern.pattern == "^payapprove:"
    Updater.start_polling.assert_awaited_once_with(
        allowed_updates=["callback_query"], drop_pending_updates=True
    )
    await side.stop()
    assert side.stopping


def test_r10_setup_never_replaces_an_existing_overlay(tmp_path, checkouts, built):
    with pytest.raises(H.Refused, match="^R10: .* already exists"):
        _setup(tmp_path, checkouts)  # the module's setup left its overlay in place
    assert not (tmp_path / "testvault").exists()


# -- the TEST label ---------------------------------------------------------------


class FakeMsg:
    def __init__(self, chat_id: int, message_id: int, text: str) -> None:
        self.chat = SimpleNamespace(id=chat_id)
        self.message_id = message_id
        self.text = text
        self.is_accessible = True
        self.edit_date = None


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[FakeMsg, object]] = []
        self.request = SimpleNamespace(initialize=AsyncMock())

    async def send_message(self, *, chat_id, text, reply_markup=None, **_kw):
        msg = FakeMsg(chat_id, len(self.sent) + 1, text)
        self.sent.append((msg, reply_markup))
        return msg


async def _audit_sink(_event, _detail):
    return True


CARD_LINE = f"{H.TEST_LABEL} ending 4242"


async def test_the_registry_puts_the_test_line_first():
    reg = H.TestApprovalRegistry(timeout_cap_s=0.05)
    reg.attach_audit(_audit_sink)
    bot = FakeBot()
    reg.attach_bot(bot, OWNER)
    assert await reg.request({"Site": "checkout.stripe.dev", "Card": CARD_LINE}) == ap.EXPIRED
    lines = bot.sent[0][0].text.split("\n")
    assert lines[:3] == ["Payment approval", H.TEST_LINE, "Site: checkout.stripe.dev"]
    assert await reg.notify("A notice.")
    assert bot.sent[1][0].text == f"{H.TEST_LINE}\nA notice."


@pytest.mark.parametrize(
    "fields",
    [{"Card": "Personal ending 4242"}, {"Card": CARD_LINE, "TEST": "no"}, {"Site": "x.dev"}],
)
async def test_the_registry_refuses_any_other_card_or_a_test_key(fields):
    reg = H.TestApprovalRegistry(timeout_cap_s=0.05)
    reg.attach_audit(_audit_sink)
    bot = FakeBot()
    reg.attach_bot(bot, OWNER)
    assert await reg.request(fields) == ap.REFUSED
    assert bot.sent == []


async def test_the_bot_refuses_a_message_without_the_test_line():
    bot = FakeBot()
    guarded = H.TestOnlyBot(bot)
    with pytest.raises(RuntimeError, match="without the TEST line"):
        await guarded.send_message(chat_id=OWNER, text="Payment approval\nSite: x")
    # Even the base registry, if it were handed this bot, cannot send unlabelled.
    reg = ap.PaymentApprovalRegistry(timeout_s=0.05)
    reg.attach_audit(_audit_sink)
    reg.attach_bot(guarded, OWNER)
    assert await reg.request({"Card": CARD_LINE}) == ap.REFUSED
    assert bot.sent == []
    assert not hasattr(guarded, "edit_message_text")


async def test_the_approval_wait_is_capped():
    reg = H.TestApprovalRegistry(timeout_cap_s=0.05)
    reg.attach_audit(_audit_sink)
    reg.attach_bot(FakeBot(), OWNER)
    async with asyncio.timeout(5):  # the release asks for 600 s; the cap wins
        assert await reg.request({"Card": CARD_LINE}, timeout_s=600) == ap.EXPIRED


# -- the test bot's secrets ---------------------------------------------------------


LIVE = "TELEGRAM_BOT_TOKEN=900:live\n"
OWNERS = "TELEGRAM_ALLOWED_USERS=111,222\n"


@pytest.mark.parametrize(
    ("text", "env", "match"),
    [
        (LIVE + OWNERS, {}, "is not set to a bot token"),
        (f"{LIVE}{H.BOT_TOKEN_KEY}=900:other\n{OWNERS}", {}, "is another bot's token"),
        (
            f"{LIVE}GUARDIAN_BOT_TOKEN=901:g\n{H.BOT_TOKEN_KEY}=901:t\n{OWNERS}",
            {},
            "is another bot's token",
        ),
        (
            f"{LIVE}{H.BOT_TOKEN_KEY}=902:t\n{OWNERS}",
            {"OTHER_BOT_TOKEN": "902:e"},
            "is another bot's token",
        ),
        (f"{H.BOT_TOKEN_KEY}=901:t\n{OWNERS}", {}, "TELEGRAM_BOT_TOKEN was not found"),
        (
            f"{H.BOT_TOKEN_KEY}=901:t\n{OWNERS}",
            {"TELEGRAM_BOT_TOKEN": "901:x"},
            "is another bot's token",
        ),
        (f"{LIVE}{H.BOT_TOKEN_KEY}=901:t\nTELEGRAM_ALLOWED_USERS=x,111\n", {}, "no owner id first"),
    ],
)
def test_bot_secrets_refusals(tmp_path, text, env, match):
    path = tmp_path / "secrets.env"
    path.write_text(text)
    with pytest.raises(H.Refused, match=match):
        H.read_bot_secrets(path, env)


def test_bot_secrets_read_the_test_token_and_the_owner(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text(f"{LIVE}{H.BOT_TOKEN_KEY}=901:t\n{OWNERS}")
    assert H.read_bot_secrets(path, {}) == ("901:t", 111)
    assert H.read_bot_secrets(tmp_path / "secrets.env", {"TELEGRAM_BOT_TOKEN": "900:x"})[1] == 111


# -- the test audit database (review W2) --------------------------------------------


def test_the_test_db_must_be_its_own_file(built, tmp_path):
    layout, _state = built
    live_db = tmp_path / "live.db"
    live_db.write_bytes(b"live")
    live = H.Live(frozenset(), frozenset(), frozenset({live_db}))
    H.check_test_db(layout, live)
    copy = H.layout_for(tmp_path / "copy")
    copy.home.mkdir(parents=True)
    os.symlink(live_db, copy.db)
    with pytest.raises(H.Refused, match="^R2: genesis-test.db is not a private regular file"):
        H.check_test_db(copy, live)
    copy.db.unlink()
    os.link(live_db, copy.db)
    with pytest.raises(H.Refused, match="^R2: genesis-test.db is not a private regular file"):
        H.check_test_db(copy, live)
    copy.db.unlink()
    copy.db.write_bytes(b"x")
    os.symlink(live_db, Path(f"{copy.db}-wal"))
    with pytest.raises(H.Refused, match="^R2: genesis-test.db-wal is not a private"):
        H.check_test_db(copy, live)
    Path(f"{copy.db}-wal").unlink()
    H.check_test_db(copy, live)
    single = H.Live(frozenset(), frozenset(), frozenset({copy.db}))
    with pytest.raises(H.Refused, match="^R2: genesis-test.db is a live database"):
        H.check_test_db(copy, single)
    (copy.db).unlink()
    with pytest.raises(H.Refused, match="^R2: the test audit database is missing"):
        H.check_test_db(copy, live)


def test_r1_records_the_live_databases(tmp_path):
    proc = _fake_proc(tmp_path / "proc", [])
    (proc / "77").mkdir()
    (proc / "77" / "environ").write_bytes(b"GENESIS_DB_PATH=/srv/data/live.db\0")
    live = H.live_paths(
        env={"GENESIS_DB_PATH": "/op/g.db"},
        proc_root=proc,
        server_pid=77,
        extra_dbs=(Path("/repo/data/genesis.db"),),
    )
    assert live.dbs == {Path("/srv/data/live.db"), Path("/op/g.db"), Path("/repo/data/genesis.db")}


# -- Q16 -------------------------------------------------------------------------


def _proc_tree(tmp_path: Path, procs: dict[int, tuple[str, int]]) -> Path:
    proc = tmp_path / "proc"
    for pid, (comm, ppid) in procs.items():
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "comm").write_text(comm + "\n")
        (proc / str(pid) / "stat").write_text(f"{pid} ({comm}) S {ppid} 0 0\n")
    return proc


def test_q16_a_browser_outside_the_test_mcp_is_found(tmp_path):
    proc = _proc_tree(
        tmp_path,
        {
            10: ("python", 1),
            11: ("camoufox-bin", 10),
            12: ("Web Content", 11),
            20: ("python", 1),
            21: ("chrome", 20),
        },
    )
    assert H.other_browsers(proc, allowed_root=10) == [21]
    assert sorted(H.other_browsers(proc)) == [11, 21]
    assert H.other_browsers(proc, allowed_root=20) == [11]


def test_q16_a_release_is_refused_while_another_browser_runs(built, monkeypatch):
    layout, state = built
    check = H.make_release_peer(layout, Path(state["worktree"]), server_pid=1, ancestor_comm=COMM)
    peer = SimpleNamespace(pid=os.getpid())
    monkeypatch.setattr(H, "check_release_peer", lambda *a, **k: peer)
    monkeypatch.setattr(H, "check_child_env", lambda *a: None)
    monkeypatch.setattr(H, "other_browsers", lambda *a, **k: [4242])
    with pytest.raises(PeerRefused, match="another browser"):
        check(os.getpid(), accept_ticks=0)


# -- end to end: request, tap, claim, against the TEST vault ---------------------------


class FakeTelegram:
    def __init__(self) -> None:
        self.stopping = False
        self.bot = FakeBot()
        self.handler = None

    async def start(self, handler):
        self.handler = handler
        return self.bot

    async def stop(self) -> None:
        self.stopping = True


def _request() -> dict:
    return {
        "op": "release_request",
        "card_ref": "",
        "host": "checkout.stripe.dev",
        "page_title": "Checkout",
        "amount": "12.00 USD",
        "claim_hash": hashlib.sha256(CLAIM.encode()).hexdigest(),
        "field_origins": {
            k: "https://js.stripe.com" for k in ("number", "expiry", "cvc", "name", "postal_code")
        },
    }


async def _serve_and_run(layout, state, child_env: dict, *, tap: bool):
    wt = Path(state["worktree"])
    telegram = FakeTelegram()
    stop = asyncio.Event()
    ready = asyncio.Event()

    async def on_ready(_service, _registry):
        ready.set()

    peer = H.make_release_peer(layout, wt, server_pid=os.getppid(), ancestor_comm=COMM)
    live = _live(layout.root.parent / "fakehome")
    serve = asyncio.create_task(
        H.run_serve(
            layout,
            state,
            live=live,
            telegram=telegram,
            owner=OWNER,
            timeout_cap_s=30,
            stop=stop,
            release_peer=peer,
            on_ready=on_ready,
        )
    )
    try:
        async with asyncio.timeout(30):
            await ready.wait()
        request = {**_request(), "card_ref": H.check_vault_content(layout, state["merchant"])}
        env = {**child_env, "REQUEST": json.dumps(request), "CLAIM": CLAIM}
        child = await asyncio.create_subprocess_exec(
            str(layout.venv_python),
            str(wt / H.SERVER_SCRIPT),
            "--server",
            "health",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if tap:
            async with asyncio.timeout(30):
                while not telegram.bot.sent:
                    await asyncio.sleep(0.02)
            msg, markup = telegram.bot.sent[0]
            query = SimpleNamespace(
                data=markup.inline_keyboard[0][0].callback_data,
                message=msg,
                answer=AsyncMock(),
                edit_message_reply_markup=AsyncMock(),
            )
            await telegram.handler(
                SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=OWNER)),
                None,
            )
            query.answer.assert_awaited_once_with("Approved", show_alert=False)
        out, err = await asyncio.wait_for(child.communicate(), 60)
        assert child.returncode == 0, err.decode()
    finally:
        stop.set()
        await asyncio.wait_for(serve, 30)
    return json.loads(out), out + err, telegram


async def _rows(layout) -> list[dict]:
    from genesis.db.connection import get_db

    db = await get_db(layout.db)
    try:
        return list(reversed(await audit.list_recent(db, 500)))
    finally:
        await db.close()


def _child_env(layout, state) -> dict:
    return {
        **H.mcp_env(layout, Path(state["worktree"]), Path("/x/camoufox-bin")),
        "PATH": os.environ.get("PATH", ""),
    }


async def test_end_to_end_request_tap_claim_is_value_free(built):
    layout, state = built
    before = len(await _rows(layout))
    out, child_bytes, telegram = await _serve_and_run(
        layout, state, _child_env(layout, state), tap=True
    )
    assert out == {
        "request": "ok",
        "status": "granted",
        "claim_ok": True,
        "fields": [
            "cvc",
            "exp_month",
            "exp_year",
            "label",
            "last4",
            "name",
            "number",
            "postal_code",
        ],
        "is_test_card": True,
        "label": H.TEST_LABEL,
    }
    rows = (await _rows(layout))[before:]
    events = [r["event"] for r in rows]
    for e in (
        "service_start",
        "unlock",
        "release_requested",
        "approval_requested",
        "approval_granted",
        "release_granted",
        "release_claimed",
        "service_stop",
    ):
        assert e in events
    text = telegram.bot.sent[0][0].text
    lines = text.split("\n")
    assert lines[1] == H.TEST_LINE and f"Card: {CARD_LINE}" in lines
    # The value-free sweep: audit rows, every message, the child's output, every file.
    passphrase = H.read_passphrase(layout)
    shown = json.dumps(rows) + "".join(m.text for m, _ in telegram.bot.sent)
    assert H.TEST_NUMBER not in shown and passphrase not in shown
    assert not ap.holds_long_digit_run(shown)
    assert H.TEST_NUMBER.encode() not in child_bytes and passphrase.encode() not in child_bytes
    for path in layout.root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            assert H.TEST_NUMBER.encode() not in path.read_bytes(), path


async def test_end_to_end_a_test_mcp_with_a_live_home_is_refused(built):
    layout, state = built
    before = len(await _rows(layout))
    env = {**_child_env(layout, state), "HOME": "/home/someone-else"}
    out, _bytes, telegram = await _serve_and_run(layout, state, env, tap=False)
    assert out == {"request": "peer not allowed"}
    assert telegram.bot.sent == []
    refused = [r for r in (await _rows(layout))[before:] if r["event"] == "release_refused"]
    assert refused[0]["detail"]["peer"]["problem"] == "HOME outside the test directory"


# -- teardown ----------------------------------------------------------------------


def test_teardown_removes_the_overlay_and_trashes_the_dir(tmp_path, checkouts, built):
    main, _wt = checkouts
    overlay = Path(built[1]["overlay"])
    kept = overlay.read_text()
    overlay.unlink()  # the module's run owns it; this test makes its own
    try:
        layout, _state = _setup(tmp_path, checkouts)
        done = H.teardown(layout, live=_live(tmp_path / "fakehome"), main_root=main)
        assert not layout.root.exists() and not overlay.exists()
        assert any("trash" in line for line in done)
        # conftest points the Genesis trash at tmp_path / "genesis-trash".
        assert any((tmp_path / "genesis-trash").rglob(H.MARKER))
    finally:
        overlay.write_text(kept)


def test_teardown_leaves_a_changed_overlay_and_refuses_an_unmarked_dir(tmp_path, checkouts, built):
    main, _wt = checkouts
    overlay = Path(built[1]["overlay"])
    kept = overlay.read_text()
    overlay.unlink()
    try:
        layout, _state = _setup(tmp_path, checkouts)
        overlay.write_text(kept + "# edited\n")
        done = H.teardown(layout, live=_live(tmp_path / "fakehome"), main_root=main)
        assert overlay.exists() and any("changed since setup" in line for line in done)
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(H.Refused, match="^R2: .* no harness marker"):
            H.teardown(H.layout_for(plain), live=_live(tmp_path / "fakehome"), main_root=main)
        assert plain.exists()
    finally:
        overlay.write_text(kept)


# -- teardown signals only this directory's serve (review W1) ---------------------------


def _pidfile(layout, pid: int) -> None:
    from genesis.payments.peer_check import start_of

    layout.pidfile.write_text(f"{pid} {start_of(pid)}\n")


def _fake_serve(tmp_path: Path, *args: str) -> subprocess.Popen:
    script = tmp_path / "bin" / H.Path(H.__file__).name
    script.parent.mkdir(exist_ok=True)
    script.write_text("import time\ntime.sleep(60)\n")
    return subprocess.Popen([sys.executable, str(script), *args])


def test_teardown_never_signals_a_process_that_is_not_this_serve(tmp_path):
    layout = _test_root(tmp_path)
    _pidfile(layout, os.getpid())
    with pytest.raises(H.Refused, match="not this directory's serve"):
        H.stop_running_serve(layout)
    other = _fake_serve(tmp_path, "serve", "--home", str(tmp_path / "another"))
    try:
        _pidfile(layout, other.pid)
        with pytest.raises(H.Refused, match="not this directory's serve"):
            H.stop_running_serve(layout)
        with pytest.raises(H.Refused, match="not this directory's serve"):
            H.stop_running_serve(layout, server_pid=other.pid)
        assert other.poll() is None  # never signalled
    finally:
        other.kill()
        other.wait()


def test_teardown_stops_this_directorys_serve(tmp_path):
    layout = _test_root(tmp_path)
    mine = _fake_serve(tmp_path, "serve", "--home", str(layout.root))
    try:
        _pidfile(layout, mine.pid)
        H.stop_running_serve(layout)
        assert mine.wait(timeout=10) != 0  # stopped by SIGINT
    finally:
        if mine.poll() is None:
            mine.kill()
            mine.wait()


def test_teardown_refuses_a_dir_holding_more_than_setup_made(built):
    layout, state = built
    stray = layout.root / "stray"
    stray.write_text("x")
    try:
        with pytest.raises(H.Refused, match="^R2: .* entries setup never made \\(stray\\)"):
            H.teardown(
                layout,
                live=_live(layout.root.parent / "fakehome"),
                main_root=Path(state["worktree"]).parent / "main",
            )
        assert layout.root.exists() and Path(state["overlay"]).exists()
    finally:
        stray.unlink()
