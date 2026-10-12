"""``python -m genesis vault``: create the card vault, add, list or retire cards,
and unlock or lock the vault service in genesis-server.

The commands that read a secret (init, add, retire) run only in the owner's own
terminal: both stdin and stdout must be a TTY, and they refuse inside a coding
agent's session (Claude Code or Codex markers set). That is an accident guard, so a session
never ends up holding a card number or the passphrase; it is not a boundary
against a process that controls its own environment. Secrets are read with
``getpass`` and never echoed: a terminal that cannot hide input is refused,
not answered in the clear. ``unlock`` is guarded the same way and reads the
passphrase before it connects. ``list``, ``status`` and ``lock`` show nothing
secret and run anywhere: locking is always the safe direction.
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
import socket
import sys
import warnings

from genesis.env import vault_socket_path
from genesis.payments import vault_store as vs

# Unlock's scrypt runs about 2-5 s in the server; an answer missing after 30 s
# means the server is wedged, and the owner's terminal must not hang on it.
_SERVICE_TIMEOUT_S = 30.0
_MAX_REPLY = 64 * 1024  # a status reply is a few hundred bytes


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


# Set in the shells that coding agents run commands in: Claude Code
# (CLAUDECODE) and Codex (READ: openai/codex codex-rs/protocol/src/
# shell_environment.rs and core spawn). An agent's terminal can still be a TTY,
# so these are checked before the TTY test.
_AGENT_SESSION_MARKERS = (
    "CLAUDECODE",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "CODEX_SANDBOX_NETWORK_DISABLED",
)


def _ask(prompt: str) -> str:
    """One hidden answer. Never echoed: refused instead, as is ended input."""
    with warnings.catch_warnings():
        # getpass falls back to ECHOED input, with only a warning, when it
        # cannot turn echo off; raised as an error, that stops it before it reads.
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise vs.VaultError(
                "this terminal cannot hide what you type; nothing was read"
            ) from None
        except EOFError:
            raise vs.VaultError("input ended before an answer; nothing was saved") from None
        except KeyboardInterrupt:
            raise vs.VaultError("cancelled; nothing was saved") from None


def _owner_terminal(command: str, *, needs_crypto: bool = True) -> bool:
    if any(os.environ.get(name) for name in _AGENT_SESSION_MARKERS):
        print(
            f"vault {command}: refused inside a coding agent's session; "
            "run it in your own terminal.",
            file=sys.stderr,
        )
        return False
    if not _interactive():
        print(f"vault {command}: needs an interactive terminal.", file=sys.stderr)
        return False
    missing = [m for m in ("pyrage", "zxcvbn") if importlib.util.find_spec(m) is None]
    if missing and needs_crypto:
        print(
            f"vault {command}: missing {', '.join(missing)}; install the payments extra "
            "(pip install -e '.[payments]').",
            file=sys.stderr,
        )
        return False
    return True


def _service(request: dict) -> dict | None:
    """One request to the vault service; None when it is not running."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(_SERVICE_TIMEOUT_S)
            conn.connect(str(vault_socket_path()))
            conn.sendall((json.dumps(request) + "\n").encode())
            reply = b""
            while not reply.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                reply += chunk
                if len(reply) > _MAX_REPLY:
                    raise vs.VaultError("the vault service sent an oversized reply")
    except (FileNotFoundError, ConnectionRefusedError):
        return None
    except OSError as exc:
        raise vs.VaultError(f"the vault service did not answer ({type(exc).__name__})") from None
    try:
        return json.loads(reply)
    except ValueError:
        raise vs.VaultError("the vault service sent an unreadable reply") from None


_NOT_RUNNING = "the vault service is not running (it runs inside genesis-server)"


def _cmd_unlock(args: argparse.Namespace) -> int:
    if not _owner_terminal("unlock", needs_crypto=False):
        return 2
    try:
        state = _service({"op": "status"})
        if state is None:
            raise vs.VaultError(_NOT_RUNNING)
        if state.get("unlocked"):
            print("the vault is already unlocked")
            return 0
        if not state.get("initialised"):
            raise vs.VaultError("the vault is not initialised; run `python -m genesis vault init`")
        if not state.get("payments_extra"):
            raise vs.VaultError("genesis-server lacks the payments extra")
        if not state.get("audit"):  # the service refuses an unlock it cannot record
            raise vs.VaultError(
                "the vault service's audit log is unavailable, so it would refuse; "
                "check genesis-server's logs"
            )
        reply = _service({"op": "unlock", "passphrase": _ask("Vault passphrase: ")})
        if reply is None:
            raise vs.VaultError(_NOT_RUNNING)
        if not reply.get("ok"):
            raise vs.VaultError(reply.get("error", "refused"))
    except vs.VaultError as exc:
        print(f"vault unlock: {exc}", file=sys.stderr)
        return 2
    if reply.get("relock_in_s") is None:  # another unlock got there first
        print("the vault is already unlocked")
        return 0
    minutes = round(reply["relock_in_s"] / 60)
    print(
        f"vault unlocked; it relocks after {minutes} minutes idle, on `vault lock`, or on restart"
    )
    return 0


def _cmd_lock(args: argparse.Namespace) -> int:
    try:
        reply = _service({"op": "lock"})
    except vs.VaultError as exc:
        print(f"vault lock: {exc}", file=sys.stderr)
        return 2
    if reply is None:
        print("the vault service is not running, so nothing is unlocked")
        return 0
    if not reply.get("ok"):
        print(f"vault lock: {reply.get('error', 'refused')}", file=sys.stderr)
        return 2
    print(f"vault {reply.get('state', 'locked')}")
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    if not _owner_terminal("init"):
        return 2
    try:
        vs.check_can_init()
        first = _ask("New vault passphrase: ")
        if first != _ask("Repeat it: "):
            raise vs.VaultError("the passphrases differ; nothing was created")
        vs.init_vault(first)
    except vs.VaultError as exc:
        print(f"vault init: {exc}", file=sys.stderr)
        return 2
    print(f"vault created at {vs.vault_dir()}")
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    if not _owner_terminal("add"):
        return 2
    try:
        vs.check_ready()
        merchant = vs.check_entry(args.label, args.merchant, args.single_use)
        card = vs.parse_card(
            _ask("Card number (hidden): "),
            _ask("Expiry MM/YY (hidden): "),
            _ask("Security code (hidden): "),
        )
        name = _ask("Name on card (hidden, Enter to skip): ").strip()
        postal = _ask("Billing postal code (hidden, Enter to skip): ").strip()
        ref = vs.add_card(
            card,
            label=args.label,
            merchant=merchant,
            single_use=args.single_use,
            name=name,
            postal_code=postal,
        )
        # Echo what the vault stored, read back, never anything derived from
        # the typed number: it also confirms the row landed.
        stored = next((c for c in vs.list_cards() if c["card_ref"] == ref), None)
        if stored is None:
            raise vs.VaultError(f"{ref} was written but cannot be read back; run `vault list`")
    except vs.VaultError as exc:
        print(f"vault add: {exc}", file=sys.stderr)
        return 2
    print(f"added {ref} ({stored['brand']} ending {stored['last4']})")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    try:
        cards = vs.list_cards()
    except vs.VaultError as exc:
        print(f"vault list: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(cards, indent=2))
        return 0
    if not cards:
        print("no cards")
        return 0
    for c in cards:
        scope = c["merchant"] or "any merchant"
        use = ", single-use" if c["single_use"] else ""
        print(
            f"{c['card_ref']}  {c['status']:<7}  {c['brand']} ...{c['last4']}  "
            f"{c['label']}  [{scope}{use}]"
        )
    return 0


def _cmd_retire(args: argparse.Namespace) -> int:
    if not _owner_terminal("retire"):
        return 2
    try:
        vs.check_ready()
        vs.check_active_card(args.card_ref)
        vs.retire_card(args.card_ref, _ask("Vault passphrase: "))
    except vs.VaultError as exc:
        print(f"vault retire: {exc}", file=sys.stderr)
        return 2
    print(
        f"retired {args.card_ref}; its encrypted details were removed from the vault "
        "(best effort on disk: also close the virtual card with its issuer)"
    )
    return 0


def _service_line(service: dict | None) -> str:
    if service is None:
        return "service: not running"
    if not service.get("ok"):
        return f"service: {service.get('error', 'no answer')}"
    if service.get("unlocked"):
        line = f"service: UNLOCKED since {service.get('unlocked_at')}"
        if service.get("relock_in_s") is not None:
            line += f", relocks in {round(service['relock_in_s'] / 60)} min if idle"
    else:
        line = "service: locked"
    if not service.get("audit"):
        line += " (audit log unavailable: unlock is refused)"
    last = service.get("last_event")
    if last:
        line += f"; last event: {last['event']} at {last['created_at']}"
    return line


def _cmd_status(args: argparse.Namespace) -> int:
    status = vs.vault_status()
    try:
        service = _service({"op": "status"})
    except vs.VaultError as exc:
        service = {"ok": False, "error": str(exc)}
    healthy = not status["error"] and all(i["private"] for i in status["files"].values())
    if args.json:
        print(json.dumps({**status, "service": service}, indent=2))
        return 0 if healthy else 1
    print(
        f"vault: {status['path']} ({'initialised' if status['initialised'] else 'not initialised'})"
    )
    if status["error"]:
        print(f"cards: {status['error']}")
    else:
        print(f"cards: {status['cards']['active']} active, {status['cards']['retired']} retired")
    for name, info in status["files"].items():
        print(f"  {name}: {info['mode']}{'' if info['private'] else '  NOT PRIVATE'}")
    print(_service_line(service))
    return 0 if healthy else 1


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("vault", help="Encrypted payment card vault (virtual cards only)")
    sub = p.add_subparsers(dest="vault_command", required=True)

    sub.add_parser("init", help="Create the vault and set its passphrase").set_defaults(
        func=_cmd_init
    )

    ap = sub.add_parser("add", help="Add a virtual card (prompts for its details)")
    ap.add_argument("--label", required=True, help="A name for the card, e.g. 'utilities'")
    ap.add_argument("--merchant", help="The one merchant host this card is for")
    ap.add_argument("--single-use", action="store_true", help="The card is single-use")
    ap.set_defaults(func=_cmd_add)

    lp = sub.add_parser("list", help="List cards (label, brand, last four, scope, status)")
    lp.add_argument("--json", action="store_true")
    lp.set_defaults(func=_cmd_list)

    rp = sub.add_parser("retire", help="Retire a card and erase its details (needs the passphrase)")
    rp.add_argument("card_ref")
    rp.set_defaults(func=_cmd_retire)

    sub.add_parser(
        "unlock", help="Unlock the vault in genesis-server (asks for the passphrase)"
    ).set_defaults(func=_cmd_unlock)
    sub.add_parser("lock", help="Lock the vault in genesis-server now").set_defaults(func=_cmd_lock)

    sp = sub.add_parser("status", help="Whether the vault exists, card counts, file modes")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_status)
