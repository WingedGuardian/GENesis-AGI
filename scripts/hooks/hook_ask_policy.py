#!/usr/bin/env python3
"""Install-local policy for the guards that ASK the user, and nothing else.

A hook that asks for approval is a deliberate, rare exception in this repo — the
standing design axiom names the push / PR-open gate and the credentials gate as
the two instances, on the grounds that they are where information leaves the
local system or where money starts being spent. The axiom does not say every
install has to answer the same prompt on the same cadence: on a box where every
push goes to the one public repo and the operator approves each one by reflex,
the prompt has stopped being a decision and become furniture. Approval fatigue is
a real failure mode, and a gate nobody reads protects nothing.

So this module exists to let ONE install turn a NAMED ask off, while the public
default is unchanged for every other clone. It is deliberately not a general
"disable the hooks" switch:

* **Asks only.** Nothing here can touch a BLOCK. A block is a refusal the guard
  reached on the merits (a force push to origin, a merge into main, a failing
  merge gate, a dispatched session with nobody to ask) and is not this module's
  business. If a call site hands a key to :func:`ask_suppressed` and then denies
  anyway, the deny stands.
* **A closed key set.** :data:`KEYS` is the whole vocabulary. An unknown key in
  the config suppresses nothing and says so; there is no wildcard, no ``all``,
  and no way for a future config file to reach an ask that no one classified.
* **Default is ASK, and every failure lands there.** A missing file, an
  unreadable one, a parse error, a duplicate key, a non-boolean value, an
  unknown key — all of them mean "ask". There is no value that produces a
  suppression by accident, which is the property that matters: the safe
  direction has to be the one you get when something goes wrong.

**The allow that replaces a suppressed ask must say so.** Callers pass the key to
:func:`suppressed_reason` and emit that text as the allow's reason, so the
decision is still visible in the transcript rather than silently absent. "Off"
here means "stop asking me", never "stop telling me".

Configuration (all keys optional; absent means ask)::

    # ~/.genesis/config/genesis.yaml
    hooks:
      asks:
        push_publish: off     # first push of a branch, and the pr-create that
                              # would push one — the routine publish approval
        secrets_env: off      # the secrets.env credentials prompt

The value is the ask's ENABLED state, so YAML's own booleans read the right way
round: ``off``/``false``/``no`` suppress, ``on``/``true``/``yes`` (and absent)
ask. Anything else is a declared policy this module could not honour, so it
prints a NOTE naming the key and falls back to asking — the same
declared-but-discarded treatment ``git_push_guard._required_ci_workflows`` gives
its own key, and for the same reason: silently substituting a different policy
for the one an operator wrote down is worse than the misconfiguration.

Test seam: ``_TEST_HOOK_ASK_POLICY`` (e.g. ``push_publish=off,secrets_env=on``)
is honoured INSTEAD of the config file when set. The config lives outside the
repo and is absent in CI, so the policy has to be injectable or it cannot be
tested deterministically — the same seam ``_TEST_CANONICAL_PUBLIC_REPO`` provides
for the sibling repo-scoping helper.
"""

from __future__ import annotations

import contextlib
import os
import re
import sys

#: Every ask this module can speak about. A key absent from this set is not a
#: policy this install declined to use — it is a key nothing classified, so it
#: can never suppress anything. Adding a member is a deliberate act with a call
#: site attached; there is no path that grows this set from configuration.
#:
#: ``push_publish`` covers ONLY the routine publish approval: the first push of a
#: branch, and a ``gh pr create`` that would push an unpushed branch. It does NOT
#: cover the force-push ask (destructive), the no-open-PR ask, or the
#: close-then-push ask — those are hygiene and safety arms that earn their
#: friction, and the guard classifies them as unsuppressible by leaving their
#: class unset rather than by listing them here.
KEYS = frozenset({"push_publish", "secrets_env"})

_CONFIG_PATH = "~/.genesis/config/genesis.yaml"
_SEAM = "_TEST_HOOK_ASK_POLICY"


def _note(message: str) -> None:
    """Print a NOTE to stderr. Never raises — diagnostics cannot change a verdict."""
    with contextlib.suppress(Exception):
        sys.stderr.write(f"NOTE: {message}\n")
        sys.stderr.flush()


def _from_seam(raw: str) -> dict[str, object]:
    """Parse the ``key=value,key=value`` test seam into the same shape as the file.

    Values are mapped through YAML's own boolean spellings so the seam and the
    config file cannot disagree about what ``off`` means. An unrecognised value
    is passed through as the raw string, which the caller then rejects exactly as
    it would reject a non-boolean in the file — the seam must be able to exercise
    the invalid-value branch, not route around it.
    """
    truthy = {"on", "true", "yes", "y", "1"}
    falsy = {"off", "false", "no", "n", "0"}
    parsed: dict[str, object] = {}
    for item in raw.split(","):
        if "=" not in item:
            continue
        key, _, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        low = value.lower()
        parsed[key] = True if low in truthy else False if low in falsy else value
    return parsed


def _declared() -> dict[str, object]:
    """The raw ``hooks.asks`` mapping this install declares, or ``{}``.

    Every failure returns ``{}`` (= every ask enabled), which is the safe
    direction. The duplicate-key line scan mirrors the sibling readers in
    ``git_push_guard``: ``yaml.safe_load`` silently keeps the LAST value for a
    repeated key, so a badly-merged config could flip a policy with no sign of
    it. Realistic spellings are line-scanned and refused rather than parsed.
    """
    raw = os.environ.get(_SEAM)
    if raw is not None:
        return _from_seam(raw)
    try:
        import yaml  # lazy: keep the hook import-light; the genesis venv has pyyaml

        with open(os.path.expanduser(_CONFIG_PATH)) as fh:
            text = fh.read()
        if (
            len(re.findall(r"(?m)^hooks\s*:", text)) > 1
            or len(re.findall(r"(?m)^\s*asks\s*:", text)) > 1
        ):
            _note(
                f"hooks.asks in {_CONFIG_PATH} has a duplicate 'hooks'/'asks' key — "
                f"which value wins is not decidable, so every ask stays ENABLED. "
                f"Merge the duplicate sections to restore your declared policy."
            )
            return {}
        cfg = yaml.safe_load(text) or {}
        asks = (cfg.get("hooks") or {}).get("asks")
        return asks if isinstance(asks, dict) else {}
    except FileNotFoundError:
        return {}  # the normal install: no local config, no local policy
    except Exception:  # noqa: BLE001 — an unreadable policy is no policy; ask.
        _note(
            f"hooks.asks in {_CONFIG_PATH} could not be read (missing pyyaml, parse "
            f"error, or unreadable file) — every ask stays ENABLED."
        )
        return {}


def ask_suppressed(key: str) -> bool:
    """True when this install has turned the ask named ``key`` OFF.

    ``key`` must be a member of :data:`KEYS`; anything else returns False and
    prints nothing, because an unclassified ask is not a policy question. A
    caller that gets False must ask (or deny) exactly as it would have before
    this module existed.
    """
    if key not in KEYS:
        return False
    value = _declared().get(key)
    if value is None:
        return False  # not declared → the public default → ask
    if isinstance(value, bool):
        return not value  # `off`/`false` → suppressed; `on`/`true` → ask
    _note(
        f"hooks.asks.{key} in {_CONFIG_PATH} is {value!r}, which is not a boolean — "
        f"use `off` to silence this ask or `on` to keep it. Keeping it ENABLED."
    )
    return False


def suppressed_reason(key: str, detail: str = "") -> str:
    """The text an allow carries when it stands in for a suppressed ask.

    The point of the sentence is that a reader of the transcript can tell the
    difference between "this guard had nothing to say" and "this guard had
    something to say and this install asked it not to interrupt". Both end in an
    allow; only one of them is a policy the operator chose, and it should be the
    one that reads like it.
    """
    tail = f" {detail}" if detail else ""
    return (
        f"allowed without prompting: this install sets hooks.asks.{key}: off in "
        f"{_CONFIG_PATH}, so the '{key}' approval prompt is turned off here. Blocks "
        f"are unaffected — only the prompt is.{tail}"
    )
