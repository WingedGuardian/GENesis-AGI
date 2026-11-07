"""A card release end to end: the real socket, the real /proc peer check, a real child process.

The child is a stand-in health server: a tiny client saved as
``<tmp repo>/scripts/genesis_mcp_server.py`` and run as ``<python> <script>
--server health``, so its argv, exe and environment are checked exactly as a
real one's are. Only the approval registry is a fake: the registry has its
own tests, and a live tap is exercised end to end with the fill tool. The
child prints only which fields it received, never their values.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import importlib
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from genesis.db.crud import payment_vault_events as audit
from genesis.payments import approval as ap
from genesis.payments import vault_store as vs
from genesis.payments.peer_check import LAUNCHED, PeerPolicy, check_release_peer
from genesis.payments.vault_service import VaultService

MIG = importlib.import_module("genesis.db.migrations.20261011060631_payment_vault_events")
VISA = "4242" * 4  # Stripe's public test Visa
SECRET = "socket-claim-secret"

CLIENT = r"""
import json, os, socket, sys, time

def ask(payload):
    s = socket.socket(socket.AF_UNIX)
    s.connect(os.environ["VAULT_SOCK"])
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
    deadline = time.monotonic() + 20
    while state == "pending" and time.monotonic() < deadline:
        time.sleep(0.02)
        state = ask({"op": "release_status", "nonce": nonce})["state"]
    out["status"] = state
    claim = {"op": "release_claim", "nonce": nonce, "claim": os.environ["CLAIM"]}
    c = ask(claim)
    out["claim_ok"] = c.get("ok")
    out["fields"] = sorted(c.get("card", {}))
    del c
    out["after"] = ask({"op": "release_status", "nonce": nonce})["state"]
    out["second"] = ask(claim).get("error")
print(json.dumps(out))
"""


class FakeRegistry:
    def __init__(self) -> None:
        self.futures: list[asyncio.Future] = []
        self.fields: list[dict] = []

    def attach_audit(self, _audit) -> None:
        pass

    async def request(self, fields, *, audit_detail=None, timeout_s=None) -> str:
        self.fields.append(dict(fields))
        fut = asyncio.get_running_loop().create_future()
        self.futures.append(fut)
        return await fut

    async def notify(self, text: str) -> bool:
        return True


@pytest.fixture(scope="module")
def _keyed(tmp_path_factory):
    path = tmp_path_factory.mktemp("keyed") / "payment_vault"
    vs.init_vault(path)
    return path


@pytest.fixture
async def rig(_keyed, tmp_path):
    vault = tmp_path / "payment_vault"
    shutil.copytree(_keyed, vault)
    exp = f"12/{datetime.now(UTC).year + 3}"
    ref = vs.add_card(
        vs.parse_card(VISA, exp, "987"),
        label="Test card",
        merchant="example.com",
        single_use=False,
        name="Ada Placeholder",
        postal_code="ZZ9 9ZZ",
        path=vault,
    )
    script = tmp_path / "repo" / "scripts" / "genesis_mcp_server.py"
    script.parent.mkdir(parents=True)
    script.write_text(CLIENT)
    comm = Path("/proc/self/comm").read_text().strip()
    policy = PeerPolicy(python=sys.executable, script=str(script), ancestor_comm=comm)
    reg = FakeRegistry()
    async with aiosqlite.connect(str(tmp_path / "g.db")) as db:
        await MIG.up(db)
        svc = VaultService(
            db,
            vault_path=vault,
            socket_path=tmp_path / "run" / "v.sock",
            approvals=reg,
            # This test process stands in for claude, and its parent for
            # genesis-server: the child is then a Genesis-launched session's.
            release_peer=functools.partial(
                check_release_peer, policy=policy, server_pid=os.getppid()
            ),
        )
        await svc.start()  # loads identity.key
        assert svc.unlocked
        request = {
            "op": "release_request",
            "card_ref": ref,
            "host": "shop.example.com",
            "page_title": "Checkout",
            "amount": "12.00 USD",
            "claim_hash": hashlib.sha256(SECRET.encode()).hexdigest(),
            "field_origins": {"number": "https://js.stripe.com"},
            "button_label": "Pay 12.00 USD",
            "also_filled": ["email"],
        }
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        env.update(VAULT_SOCK=str(svc._socket_path), REQUEST=json.dumps(request), CLAIM=SECRET)
        yield svc, reg, db, script, env, request
        await svc.stop()


async def _run_child(script: Path, env: dict, reg: FakeRegistry, grant: bool) -> dict:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        "--server",
        "health",
        env=env,
        stdout=asyncio.subprocess.PIPE,
    )
    if grant:
        async with asyncio.timeout(20):
            while not reg.futures:
                await asyncio.sleep(0.01)
        reg.futures[0].set_result(ap.GRANTED)
    out, _ = await asyncio.wait_for(proc.communicate(), 30)
    assert proc.returncode == 0
    return json.loads(out)


async def _rows(db) -> list[dict]:
    return list(reversed(await audit.list_recent(db, 100)))


async def test_the_health_server_peer_claims_once(rig):
    svc, reg, db, script, env, _request = rig
    out = await _run_child(script, env, reg, grant=True)
    assert out == {
        "request": "ok",
        "status": "granted",
        "claim_ok": True,
        "fields": ["label", "last4", "number"],  # only what field_origins named
        "after": "expired",
        "second": "no such release",
    }
    rows = [r for r in await _rows(db) if r["event"].startswith("release_")]
    assert [r["event"] for r in rows][:3] == [
        "release_requested",
        "release_granted",
        "release_claimed",
    ]
    peer = rows[0]["detail"]["peer"]
    assert peer["kind"] == LAUNCHED and peer["claude_pid"] == os.getpid()
    assert "Genesis-launched session" in reg.fields[0]["Requester"]
    assert (reg.fields[0]["Button"], reg.fields[0]["Also filled"]) == ("Pay 12.00 USD", "email")
    assert rows[0]["detail"]["field_lengths"]["button_label"] == len("Pay 12.00 USD")
    assert rows[0]["detail"]["also_filled"] == ["email"]
    assert VISA not in json.dumps(rows) and VISA not in json.dumps(out)


async def test_pytest_itself_is_refused_for_its_argv(rig):
    svc, _reg, db, _script, _env, request = rig
    reader, writer = await asyncio.open_unix_connection(str(svc._socket_path))
    writer.write((json.dumps(request) + "\n").encode())
    await writer.drain()
    reply = json.loads(await reader.readline())
    writer.close()
    assert reply == {"ok": False, "error": "peer not allowed"}
    row = (await _rows(db))[-1]
    assert row["event"] == "release_refused"
    assert row["detail"]["peer"]["check"] == "argv mismatch"


async def test_the_same_child_with_a_python_variable_is_refused(rig):
    _svc, reg, db, script, env, _request = rig
    out = await _run_child(script, {**env, "PYTHONDONTWRITEBYTECODE": "1"}, reg, grant=False)
    assert out == {"request": "peer not allowed"}
    row = (await _rows(db))[-1]
    assert row["detail"]["peer"]["check"] == "python env"
    assert row["detail"]["peer"]["python_env"] == ["PYTHONDONTWRITEBYTECODE"]
    assert reg.futures == []
