#!/usr/bin/env python3
"""CC↔Node pin-lockstep guard — fail a PR that bumps Claude Code past the Node
floor its ``engines.node`` requires without also bumping ``NODE_MAJOR``.

Why this exists (a real incident, 2026-07-04): a host VM sat on Node 20 while
its pinned Claude Code required Node >=22, so ``claude -p`` — the Guardian's
recovery brain — could not start. ``scripts/update.sh`` now HEALS deployed
drift, but nothing stopped the *authoring-time* mistake: the "bump NODE_MAJOR
in lockstep" instruction in ``scripts/lib/cc_version.sh`` was only a comment.
This turns that comment into a mechanical CI gate.

The single source of truth for both pins is ``scripts/lib/cc_version.sh``
(``CC_VERSION`` + ``NODE_MAJOR``). This script reads them, fetches the pinned
CC version's ``engines.node`` from the npm registry, and asserts
``NODE_MAJOR >= <required floor major>``.

Error policy (deliberate):
  * FAIL CLOSED (exit 1) on a real, definitive problem: NODE_MAJOR below the
    floor; a pin file that cannot be parsed; or an HTTP 404 for the pinned CC
    version (a version that does not exist in the registry is a typo'd pin,
    not a network blip — catching it here is free).
  * FAIL OPEN (warn + exit 0) on anything that is plausibly transient or
    merely unrecognized: connection/timeout/5xx errors (one retry first), or
    an ``engines.node`` comparator shape this parser does not understand. A
    flaky npm registry must never wall off every PR, and an unparseable-but-
    present spec should not hard-block on a parser gap — it degrades to a
    no-op gate, never a false failure.

Usage:  python scripts/check_cc_node_lockstep.py [--pin-file PATH]
        exit 0 = lockstep OK (or fail-open condition), 1 = violation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_PIN_FILE = _REPO_ROOT / "scripts" / "lib" / "cc_version.sh"

_REGISTRY_URL = "https://registry.npmjs.org/@anthropic-ai/claude-code/{version}"
_HTTP_TIMEOUT = 15  # seconds — a single registry GET; generous for a slow runner.
_RETRIES = 1  # one retry on a transient network error before failing open.

# Matches `CC_VERSION="${CC_VERSION:-2.1.201}"` / `NODE_MAJOR="${NODE_MAJOR:-22}"`.
# Captures the default value after `:-`. Tolerant of surrounding quoting/spacing.
_PIN_RE = {
    "CC_VERSION": re.compile(r'CC_VERSION="?\$\{CC_VERSION:-([0-9]+\.[0-9]+\.[0-9]+)\}"?'),
    "NODE_MAJOR": re.compile(r'NODE_MAJOR="?\$\{NODE_MAJOR:-([0-9]+)\}"?'),
}


class LockstepViolation(Exception):
    """A definitive lockstep failure — the caller should exit non-zero."""


class FailOpen(Exception):
    """A transient / unrecognized condition — warn and exit zero."""


def parse_pins(pin_file: Path) -> tuple[str, int]:
    """Extract (CC_VERSION, NODE_MAJOR) defaults from cc_version.sh.

    A pin file we cannot parse is a real error (fail closed) — the pins are
    the contract this gate enforces; if they are unreadable the gate is blind.
    """
    try:
        text = pin_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise LockstepViolation(f"cannot read pin file {pin_file}: {exc}") from exc

    cc_match = _PIN_RE["CC_VERSION"].search(text)
    node_match = _PIN_RE["NODE_MAJOR"].search(text)
    if not cc_match or not node_match:
        missing = []
        if not cc_match:
            missing.append("CC_VERSION")
        if not node_match:
            missing.append("NODE_MAJOR")
        raise LockstepViolation(
            f"could not parse {', '.join(missing)} default(s) from {pin_file} "
            "(expected `VAR=\"${VAR:-value}\"` form)"
        )
    return cc_match.group(1), int(node_match.group(1))


def fetch_engines_node(cc_version: str, *, opener=urllib.request.urlopen) -> str:
    """Return the ``engines.node`` string for a pinned CC version from npm.

    ``opener`` is injectable so tests never touch the network. Raises
    ``LockstepViolation`` on HTTP 404 (nonexistent version = typo'd pin);
    ``FailOpen`` on transient network errors (after one retry) or a missing
    engines.node field.
    """
    url = _REGISTRY_URL.format(version=cc_version)
    last_exc: Exception | None = None
    for attempt in range(_RETRIES + 1):
        try:
            with opener(url, timeout=_HTTP_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise LockstepViolation(
                    f"Claude Code {cc_version} not found in the npm registry "
                    "(HTTP 404) — is the pin a typo?"
                ) from exc
            # 5xx and other HTTP errors are treated as transient.
            last_exc = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
        if attempt < _RETRIES:
            time.sleep(1)
    else:
        raise FailOpen(
            f"npm registry unreachable for {cc_version} after "
            f"{_RETRIES + 1} attempt(s): {last_exc}"
        )

    engines = payload.get("engines") or {}
    node_spec = engines.get("node")
    if not node_spec:
        raise FailOpen(
            f"Claude Code {cc_version} declares no engines.node — "
            "cannot verify the Node floor (treating as non-blocking)"
        )
    return str(node_spec)


def required_node_major(node_spec: str) -> int:
    """Extract the minimum required Node MAJOR from an ``engines.node`` spec.

    Handles the comparator shapes npm packages actually use: ``>=22.0.0``,
    ``>=22``, ``^22.1.0``, ``~22``, ``22.x``, and ranges like ``>=20 <23``
    (the lower bound governs the floor). An unrecognized shape fails OPEN
    (a parser gap must not hard-block a real, valid spec).
    """
    # Lower-bound comparators that establish a floor.
    floor_re = re.compile(r"(?:>=|\^|~|=)?\s*v?(\d+)(?:\.\d+|\.x|\.\*)?")
    # A bare ">N" (strictly greater) floor means major N+1 at minimum only if
    # it's ">Nmajor.max" — too rare to model; treat ">" like ">=" conservatively
    # (lower floor = more lenient = fail-open-leaning, never a false failure).
    for token in node_spec.replace(",", " ").split():
        # Skip pure upper-bound comparators so a range's lower bound wins.
        if token.startswith("<"):
            continue
        m = floor_re.match(token)
        if m:
            return int(m.group(1))
    raise FailOpen(f"unrecognized engines.node spec {node_spec!r} — cannot derive floor")


def check(pin_file: Path, *, opener=urllib.request.urlopen) -> str:
    """Run the lockstep check. Returns a success message or raises.

    Raises ``LockstepViolation`` (fail closed) or ``FailOpen`` (warn + pass).
    """
    cc_version, node_major = parse_pins(pin_file)
    node_spec = fetch_engines_node(cc_version, opener=opener)
    floor = required_node_major(node_spec)
    if node_major < floor:
        raise LockstepViolation(
            f"NODE_MAJOR={node_major} is BELOW the floor required by "
            f"Claude Code {cc_version} (engines.node={node_spec!r} → needs "
            f"Node major >= {floor}). Bump NODE_MAJOR in scripts/lib/cc_version.sh "
            "in lockstep with the CC pin."
        )
    return (
        f"CC↔Node lockstep OK: Claude Code {cc_version} needs Node >= {floor} "
        f"(engines.node={node_spec!r}); NODE_MAJOR={node_major}."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CC↔Node pin-lockstep CI guard.")
    parser.add_argument(
        "--pin-file",
        type=Path,
        default=_DEFAULT_PIN_FILE,
        help="Path to cc_version.sh (default: scripts/lib/cc_version.sh).",
    )
    args = parser.parse_args(argv)

    try:
        message = check(args.pin_file)
    except LockstepViolation as exc:
        print(f"CC↔Node lockstep FAILED: {exc}", file=sys.stderr)
        return 1
    except FailOpen as exc:
        print(f"CC↔Node lockstep SKIPPED (non-blocking): {exc}", file=sys.stderr)
        return 0
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
