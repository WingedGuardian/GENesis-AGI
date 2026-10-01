"""Config lever for gh-capable background dispatch (the CC ``steward`` profile).

Cloned from ``recon.github_steward_config`` (itself cloned from
``cc.foreground_reaper_config``): fresh-read-per-call, ``MODES`` tuple + env kill
switch, degrade-toward-less-authority on damage.

NOT THE SAME THING AS ``recon.github_steward_config``, and the names are close
enough that this has already cost a session real time. That module gates a
deterministic PYTHON poller watching the owner's GitHub account, which has been
live for weeks. THIS module gates whether a background CLAUDE CODE SESSION may be
dispatched with the ``gh`` binary in its Bash allowlist. Different subject,
different authority, different failure mode.

Modes (increasing authority):
  - ``off``  — a gh-capable dispatch is REFUSED. SHIPPED DEFAULT.
  - ``live`` — a gh-capable dispatch is permitted.

WHY ONLY TWO RUNGS, when the sibling has three. An ``observe`` rung would have to
mean "reads but never writes", and nothing can enforce that here: the gated thing
is an LLM holding a ``gh`` binary, and the allowlist bounds which BINARY runs, not
which subcommand. ``gh pr comment`` is as reachable as ``gh pr view`` under any
first-token allowlist. A rung named ``observe`` that cannot prevent a write would
be a safety claim with no mechanism behind it — precisely the error
``autonomy/audit.py:198-214`` was rewritten to forbid. ``observe`` becomes
possible once a SUBCOMMAND allowlist exists, and not before.

WHAT ``live`` MEANS, AND THE PART TO CHECK BEFORE FLIPPING IT. It permits the
DISPATCH. Whether the dispatched session can then AUTHENTICATE to GitHub is a
property of ``cc/invoker.py`` and of this install, NOT of this lever — and this
module deliberately does not assert an answer, because an earlier draft asserted
"unauthenticated" and that was FALSE on its own branch: the sealed gh config still
carried a copy of the operator's credential, so arming would have handed a
PR-reading session the owner's full GitHub identity while five separate texts
promised it had none.

**So verify, do not infer.** Before arming on any install:

    GH_CONFIG_DIR=~/.genesis/gh-sealed GH_TOKEN="" gh auth status

"not logged into any GitHub hosts" means an armed session gets the ``gh`` binary
and no identity. Anything else means arming grants whatever that account can do —
on a profile whose input is attacker-authored pull-request text. Either way the
profile's own prompt tells the session not to assume, and to treat a successful
call as acting for the owner.

WHY THE LEVER IS KEYED ON THE CAPABILITY, NOT THE PROFILE NAME. The gate asks
whether the requested profile's Bash allowlist contains ``gh``, so an install that
grants ``gh`` to its own profile through ``genesis.cc.profile_overlay`` is covered
by construction rather than by someone remembering to add a registry row —
``ProfileOverlayContext.add_profile`` writes straight into
``_PROFILE_BASH_ALLOWLIST``, which is what the gate reads. The module keeps the
``steward`` name because that is the only shipped profile with the grant; read it
as the gh-dispatch lever.

A missing/corrupt config degrades to DEFAULTS; an invalid ``mode`` degrades to
``off``. With two rungs the least-authority rung IS ``off``, so "degrade toward
less authority" and "never a silent grant" agree here — unlike the sibling, where
the middle rung is the safe one. Env kill switch
``GENESIS_CC_STEWARD_DISABLED=1`` forces ``off``.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only. In
particular this must NOT import ``genesis.cc.direct_session``, which imports it.

Generalizability: nothing install-specific ships here. The shipped default is
``off`` on every install; an operator arms their own box by writing
``~/.genesis/config/cc_steward.local.yaml``. That is the CANONICAL overlay path —
``_config_overlay`` resolves user-dir-FIRST and accepts a repo-relative
``config/cc_steward.local.yaml`` only as back-compat, so the sibling LOSES when
both exist, and the dashboard/MCP settings writers only ever land in the user dir.
Naming the sibling in operator-facing text is therefore a footgun, not a
shorthand: hand-edit it, flip the lever from the dashboard later, and the overlay
you did not touch is the one that wins.
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from genesis._config_overlay import merge_local_overlay
from genesis.env import repo_root

logger = logging.getLogger(__name__)

MODES = ("off", "live")

_CONFIG_NAME = "cc_steward.yaml"

# DERIVED, not a second literal: the overlay name must track the config name, and
# a hand-written copy is how the refusal message ends up naming a file that does
# not exist. Mirrors `_config_overlay._resolve_overlay_path`, which does the same
# `with_suffix(".local.yaml")` transform.
_OVERLAY_NAME = Path(_CONFIG_NAME).with_suffix(".local.yaml").name

_ENV_KILL_SWITCH = "GENESIS_CC_STEWARD_DISABLED"

DEFAULTS: dict[str, Any] = {
    # The master switch stays TRUE and the SHIPPED POSTURE is carried by `mode`.
    # Four siblings ship exactly this way (marketing_outreach, career_outreach,
    # voice_act, voice_recency_resume): `enabled: false` reads as "this feature
    # is broken/absent", `mode: "off"` reads as "present and deliberately inert",
    # and the second is what this is.
    "enabled": True,
    "mode": "off",
}


def config_path() -> Path:
    """Tracked config location. Separate function so tests can locate it."""
    return repo_root() / "config" / _CONFIG_NAME


def load_config() -> dict[str, Any]:
    """Merged config, READ FRESH on every call.

    Fresh-read rather than cached at import: an operator arming their install
    edits the overlay and expects the next dispatch to honour it, not the next
    server restart. The cost is one small YAML read per gh-capable spawn, which
    is not a hot path.

    Layering: ``DEFAULTS`` <- ``config/cc_steward.yaml`` <- ``cc_steward.local.yaml``.
    Every read failure is caught and logged: config damage must degrade to a
    known posture, never raise into a spawn path.
    """
    merged = copy.deepcopy(DEFAULTS)
    base_path = config_path()
    base: dict[str, Any] = {}
    try:
        if base_path.is_file():
            loaded = yaml.safe_load(base_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                base = loaded
    except Exception:
        logger.warning("cc_steward base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("cc_steward overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def effective_mode() -> str:
    """The mode a gh-capable dispatch is judged against — read live.

    Every degradation path lands on ``off``:

    - env kill switch set;
    - master ``enabled: false``;
    - ``mode`` is the BOOLEAN ``False``, which is what a hand-edited unquoted
      ``mode: off`` becomes under YAML 1.1. This is not hypothetical — a sibling
      ships exactly that unquoted form today, and without this branch it would
      fall through to the invalid-value case. Same landing here, but only by
      luck, so it is handled explicitly;
    - any value not in ``MODES``.
    """
    if os.environ.get(_ENV_KILL_SWITCH) == "1":
        return "off"
    cfg = load_config()
    # `enabled` MUST be the literal boolean True. A truthiness test here is a
    # FAIL-OPEN, and it is the operator's disarm gesture that it breaks:
    # MEASURED, `enabled: "false"` / `"off"` / `"no"` / `"0"` are all non-empty
    # STRINGS, so `not cfg["enabled"]` is False and the lever stayed LIVE while
    # the operator believed they had switched it off. Quoted forms are the likely
    # spelling too, because this lever's own config tells the reader to quote
    # values (`mode: "off"` must be quoted or YAML 1.1 makes it a boolean) — so
    # the documentation was selecting for the broken input.
    #
    # `is not True` rather than a MODES-style membership check because this is a
    # boolean knob: anything that is not exactly True — a string, a number, a
    # list, None — is damage, and damage degrades toward less authority.
    if cfg.get("enabled", True) is not True:
        logger.warning(
            "cc_steward `enabled` is %r, not a boolean — degrading to off",
            cfg.get("enabled"),
        )
        return "off"
    mode = cfg.get("mode")
    if mode is False:
        return "off"
    if mode not in MODES:
        logger.warning("cc_steward has invalid mode %r — degrading to off", mode)
        return "off"
    return mode


def gh_dispatch_permitted() -> bool:
    """Whether a dispatch may carry ``gh`` in its Bash allowlist.

    Spelled as ``== "live"`` rather than ``!= "off"`` so that adding a third rung
    later cannot silently grant it the capability: a new mode has to be named
    here to be permitted. ``effective_mode`` can only return a member of
    ``MODES`` today, which is exactly why the distinction is free now and
    expensive later.
    """
    return effective_mode() == "live"


def refusal_message(profile: str) -> str:
    """Why a gh-capable dispatch was refused, and how an operator arms it.

    Written for the SESSION reading a traceback, not for a dashboard: it names
    the file to edit and the value to set, because "disabled by config" sends the
    reader hunting for which config.
    """
    return (
        f"Refusing to dispatch profile {profile!r}: it is allowlisted to run the "
        f"`gh` CLI, and gh-capable background dispatch is currently OFF. This is "
        f"the shipped default — the capability is deliberately inert, not broken. "
        f'To arm it, set `mode: "live"` in '
        f"`~/.genesis/config/{_OVERLAY_NAME}` — the canonical overlay path, which "
        f"wins over a repo-relative sibling (quote the value; unquoted `off`/`on` "
        f"are YAML booleans). VERIFY WHAT ARMING GRANTS before you do — this "
        f"lever permits the dispatch, and whether that session can authenticate "
        f"to GitHub is a property of the invoker and of this install, not of this "
        f"lever: run `GH_CONFIG_DIR=~/.genesis/gh-sealed GH_TOKEN=\"\" gh auth "
        f"status`. \"not logged into any GitHub hosts\" means arming grants the "
        f"binary only; anything else means it grants that account's reach to a "
        f"session reading attacker-authored pull-request text. "
        f"`{_ENV_KILL_SWITCH}=1` forces off regardless of config."
    )
