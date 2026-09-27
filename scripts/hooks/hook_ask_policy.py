#!/usr/bin/env python3
"""Install-local policy for the guards that ASK the user, and nothing else.

A hook that asks for approval is a deliberate, rare exception in this repo. Two
of them are routine enough to wear an operator down: the push guard's first-push
prompt (where code leaves the local system) and the credentials guard's
secrets.env prompt. Nothing says every install has to answer the same prompt on
the same cadence: on a box where every push goes to the one public repo and the
operator approves each one by reflex,
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
  unreadable one, a parse error, a duplicate key, a non-boolean value (a
  bodiless key included), an unknown key — all of them mean "ask", and every
  one of them that the operator actually WROTE is announced with a NOTE, carried
  in the hook's own stdout payload (see :func:`drain_notes`) on the next ask the
  policy is consulted for. There
  is no value that produces a suppression by accident, which is the property
  that matters: the safe direction has to be the one you get when something
  goes wrong.

**Suppression means "this hook stops objecting", never "approve on my behalf".**
A suppressed ask is replaced by NO permission decision at all: the hook exits 0
with only ``additionalContext`` naming the setting (the context-only shape
``git_discard_guard`` uses). Other hooks, and Claude Code's own permission
settings, then decide the command exactly as they would if this hook had never
raised the prompt. An ``allow`` would be the wrong translation: it overrides the
native permission prompt for the WHOLE Bash command, so ``git push && curl …``
would be approved by a setting that was only ever about the push. The note is
what keeps the decision visible in the transcript. "Off" here means "stop asking
me", never "stop telling me".

Consequence worth stating for installs that run Claude Code in its default
permission mode: silencing this hook's prompt does not by itself stop Claude Code
asking, because a command not on the install's ``permissions.allow`` list still
gets the native prompt. Such an install adds the native allow rule (for example
``Bash(git push:*)``) as well — that is Claude Code's own lever, and this module
deliberately does not reach around it.

Configuration (all keys optional; absent means ask)::

    # ~/.genesis/config/genesis.yaml
    hooks:
      asks:
        push_publish: off     # a bare first push of the current branch to
                              # the declared public repo
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
import sys

#: Every ask this module can speak about. A key absent from this set is not a
#: policy this install declined to use — it is a key nothing classified, so it
#: can never suppress anything. Adding a member is a deliberate act with a call
#: site attached; there is no path that grows this set from configuration.
#:
#: ``push_publish`` covers ONLY the routine publish approval: a command that is
#: one bare first push of the current branch, whose push URLs all name the
#: install's declared public repo. It does NOT cover a push anywhere else, a
#: compound command, a ``gh pr create`` that would push
#: (gh picks its own destination, possibly a fork), the force-push ask
#: (destructive), the no-open-PR ask, or the close-then-push ask — the guard
#: classifies those as unsuppressible by leaving their class unset rather than
#: by listing them here.
KEYS = frozenset({"push_publish", "secrets_env"})

_CONFIG_PATH = "~/.genesis/config/genesis.yaml"
_SEAM = "_TEST_HOOK_ASK_POLICY"


#: NOTEs raised while reading the policy, held for the caller to deliver. stderr
#: alone is not enough: Claude Code discards an exit-0 hook's stderr, and every
#: path that reads this policy exits 0 (an ask, or a suppression note), so a
#: NOTE printed only there would be the silent discard this module promises not
#: to commit. Callers append :func:`drain_notes` to the text they emit.
_PENDING_NOTES: list[str] = []


def _note(message: str) -> None:
    """Record a NOTE and print it to stderr. Never raises — diagnostics cannot
    change a verdict."""
    with contextlib.suppress(Exception):
        if message not in _PENDING_NOTES:
            _PENDING_NOTES.append(message)
        sys.stderr.write(f"NOTE: {message}\n")
        sys.stderr.flush()


def drain_notes() -> str:
    """The NOTEs raised since the last drain, as one block of text ("" if none).

    The caller puts this into the ask reason or the suppression note it emits,
    because that stdout payload is the only channel Claude Code delivers on an
    exit-0 hook.
    """
    notes = [f"NOTE: {m}" for m in _PENDING_NOTES]
    _PENDING_NOTES.clear()
    return "\n".join(notes)


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


def _has_merge_key(yaml, mapping) -> bool:
    return any(
        isinstance(k, yaml.ScalarNode) and k.tag == "tag:yaml.org,2002:merge"
        for k, _ in mapping.value
    )


def _duplicates_on_the_policy_path(yaml, text: str) -> list[str]:
    """Keys repeated along ``hooks`` -> ``asks`` -> a member of :data:`KEYS`.

    ``yaml.safe_load`` silently keeps the LAST value of a repeated key, so a
    badly-merged config that visibly says ``push_publish: on`` could turn the
    prompt off. Refusing to guess is the point; WHERE to look is what the first
    version got wrong. It scanned lines with a regex, which knows neither
    quoting nor nesting, and failed in both directions:

    * ``"push_publish": on`` then ``push_publish: off`` — the quoted spelling did
      not match, one key was counted, the LAST value won, and the prompt was
      switched OFF against an explicit ``on``. Fail-open.
    * an ``asks:`` or ``push_publish:`` in an UNRELATED section counted as a
      duplicate, so a valid ``off`` was discarded. Fail-closed, but wrong.

    Walking the node graph fixes both: a key's identity is its PARSED scalar
    value (so quoting cannot hide it), and only the mappings on this one path
    are visited (so an unrelated section cannot be mistaken for it). A syntax
    error raises, and the caller treats that as no policy.

    A MERGE KEY (``<<``) anywhere on the path is refused outright. safe_load pulls
    keys in from merges and resolves conflicts by rules a reader cannot see
    (explicit beats merged in any order; the FIRST entry of a merge list wins), so
    a merge can carry a hidden duplicate or override the value written last — each
    MEASURED to switch the prompt off. Checked by TAG, not text: a quoted ``"<<"``
    is an ordinary key to safe_load too.
    """
    node = yaml.compose(text, Loader=yaml.SafeLoader)
    dupes: list[str] = []
    for label in ("hooks", "asks"):
        if not isinstance(node, yaml.MappingNode):
            return dupes
        if _has_merge_key(yaml, node):
            return ["<< (merge key)"]
        values = [v for k, v in node.value if isinstance(k, yaml.ScalarNode) and k.value == label]
        if len(values) > 1:
            return [label]
        if not values:
            return dupes
        node = values[0]
    if isinstance(node, yaml.MappingNode):
        if _has_merge_key(yaml, node):
            return ["<< (merge key)"]
        counts: dict[str, int] = {}
        for k, _ in node.value:
            if isinstance(k, yaml.ScalarNode) and k.value in KEYS:
                counts[k.value] = counts.get(k.value, 0) + 1
        dupes = sorted(k for k, n in counts.items() if n > 1)
    return dupes


def _declared() -> dict[str, object]:
    """The raw ``hooks.asks`` mapping this install declares, or ``{}``.

    Every failure returns ``{}`` (= every ask enabled), which is the safe
    direction. A key repeated on the policy path is refused rather than resolved
    — see :func:`_duplicates_on_the_policy_path` for why that is a node-graph
    walk and not a line scan.
    """
    raw = os.environ.get(_SEAM)
    if raw is not None:
        return _from_seam(raw)
    try:
        import yaml  # lazy: keep the hook import-light; the genesis venv has pyyaml

        with open(os.path.expanduser(_CONFIG_PATH)) as fh:
            text = fh.read()
        dupes = _duplicates_on_the_policy_path(yaml, text)
        if dupes:
            _note(
                f"hooks.asks in {_CONFIG_PATH} declares {', '.join(dupes)} more than "
                f"once — which value wins is not decidable, so every ask stays "
                f"ENABLED. Merge the duplicates to restore your declared policy."
            )
            return {}
        cfg = yaml.safe_load(text) or {}
        asks = (cfg.get("hooks") or {}).get("asks")
        if asks is not None and not isinstance(asks, dict):
            # `asks: off` or `asks: [push_publish]` is a declaration the
            # operator wrote; dropping it without a word is the silent discard
            # this module promises not to do. A bodiless `asks:` (None)
            # declares nothing and stays quiet.
            _note(
                f"hooks.asks in {_CONFIG_PATH} is {asks!r}, not a mapping of "
                f"<prompt>: on/off — every ask stays ENABLED."
            )
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
    declared = _declared()
    # A declared key outside KEYS is a switch the operator believes they flipped
    # — most often a misspelling. It suppresses nothing (closed set), and the
    # module's contract is that it SAYS so: a prompt that keeps appearing with no
    # word about why is the silent-discard this module exists to avoid.
    unknown = sorted(str(k) for k in declared if k not in KEYS)
    if unknown:
        _note(
            f"hooks.asks in {_CONFIG_PATH} names {', '.join(unknown)}, which is not "
            f"a prompt this install can turn off (known: {', '.join(sorted(KEYS))}). "
            f"Ignored — those prompts stay ENABLED."
        )
    # Presence, not truthiness: a bodiless `push_publish:` loads as None, and
    # that is a declaration the operator wrote, not an absent key. Only a key
    # that is genuinely not there takes the silent public-default branch.
    if key not in declared:
        return False  # not declared → the public default → ask
    value = declared[key]
    if isinstance(value, bool):
        return not value  # `off`/`false` → suppressed; `on`/`true` → ask
    shown = "empty (a key with no value)" if value is None else repr(value)
    _note(
        f"hooks.asks.{key} in {_CONFIG_PATH} is {shown}, which is not a boolean — "
        f"use `off` to silence this ask or `on` to keep it. Keeping it ENABLED."
    )
    return False


def suppressed_reason(key: str, detail: str = "") -> str:
    """The context note emitted in place of a suppressed ask.

    Carried as ``additionalContext`` with NO permission decision. The point of
    the sentence is that a reader of the transcript can tell "this guard had
    nothing to say" from "this guard had something to say and this install asked
    it not to interrupt" — and that the command was NOT approved by this hook,
    only left to whatever else decides it.
    """
    tail = f" Suppressed prompt: {detail}" if detail else ""
    return (
        f"not prompting: this install sets hooks.asks.{key}: off in "
        f"{_CONFIG_PATH}, so this hook raises no '{key}' approval prompt here. It "
        f"has not approved the command either — other hooks and Claude Code's own "
        f"permission settings still decide it. Blocks are unaffected.{tail}"
    )
