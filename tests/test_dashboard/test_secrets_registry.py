"""`secrets.env.example` is a PARSED REGISTRY, not prose — guard its contract.

`dashboard/routes/secrets.py::_parse_example_file` builds the dashboard's
Provider Keys panel from this file. Four comment shapes are load-bearing, and
two of them are rendered to users:

* ``# Used by: …`` becomes the key's description
* ``# Signup: …``  becomes ``signup_url``, which
  ``templates/partials/tabs/config.html`` renders as ``https://`` + the value

So a ``# Signup:`` line containing free text ships a DEAD CLICKABLE LINK to
every install. There was no test for any of this, which is why three
consecutive review rounds on PR #1606 read the file as prose and only the third
noticed — after measuring the parser's output rather than reading the file.
These tests exist so the next editor is told by CI instead.
"""

from __future__ import annotations

import re

import pytest

from genesis.dashboard.routes.secrets import _parse_example_file

# Pre-existing malformed values, enumerated rather than silently tolerated.
# Each renders a dead link in the Provider Keys panel today (e.g.
# "https://console.cloud.google.com -> APIs & Services -> Credentials").
# They predate the guard and are tracked separately; the point of listing them
# is that the set may only ever SHRINK. A new key cannot join it — adding one
# here should feel like the deliberate act it is.
_KNOWN_MALFORMED_SIGNUP = frozenset(
    {
        "GOOGLE_API_KEY",
        "API_KEY_ZENMUX",
        "API_KEY_MINIMAX",
        "API_KEY_NVIDIA_NIM",
        "API_KEY_GITHUB",
        "API_KEY_AZURE",
        "API_KEY_BEDROCK",
        "API_KEY_TAVILY",
        "API_KEY_EXA",
        "API_KEY_CLOUDFLARE",
        "API_KEY_ELEVENLABS",
        "API_KEY_CARTESIA",
        "API_KEY_FISH_AUDIO",
        "API_KEY_DEEPINFRA",
        "API_KEY_PAGEINDEX",
        "TESTSPRITE_API_KEY",
    }
)

# A bare host: labels separated by dots, optionally followed by a /path.
# Deliberately strict — no spaces, no arrows, no trailing prose.
_BARE_HOST_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+(/\S*)?$"
)


def test_signup_urls_are_bare_hosts():
    """Every ``# Signup:`` value must be href-able, because it becomes an href.

    The template does ``:href="'https://' + k.signup_url"`` with no validation,
    so anything with a space in it is a broken link shipped to every install.
    """
    offenders = {
        d.key: d.signup_url
        for d in _parse_example_file()
        if d.signup_url
        and d.key not in _KNOWN_MALFORMED_SIGNUP
        and not _BARE_HOST_RE.match(d.signup_url)
    }
    assert not offenders, (
        "`# Signup:` becomes an href (https:// + value) in the dashboard's "
        f"Provider Keys panel, so these ship dead links: {offenders}. Use a bare "
        "host (e.g. `# Signup: console.cloud.google.com`) and put any "
        "navigation steps on the `# Used by:` line instead."
    )


def test_known_malformed_set_only_shrinks():
    """The pre-existing-debt allow-list must not contain keys that are now fine.

    Without this, a fixed key would linger in the list forever and the guard
    would quietly stop covering it.
    """
    parsed = {d.key: d.signup_url for d in _parse_example_file()}
    stale = {
        key
        for key in _KNOWN_MALFORMED_SIGNUP
        if key not in parsed or not parsed[key] or _BARE_HOST_RE.match(parsed[key])
    }
    assert not stale, (
        f"These keys are no longer malformed (or no longer exist): {sorted(stale)}. "
        "Remove them from _KNOWN_MALFORMED_SIGNUP — the list may only shrink."
    )


@pytest.mark.parametrize("key", ["ZHIPU_API_KEY", "ZAI_CODING_API_KEY", "BIGMODEL_CODING_API_KEY"])
def test_glm_keys_carry_both_parsed_fields(key):
    """The GLM slots specifically — the ones PR #1606 broke and then fixed.

    Both fields RESET after every ``KEY=`` line (secrets.py), so a single shared
    prose block above three keys leaves two of them empty. That is exactly what
    happened, and it was invisible to reading.
    """
    by_key = {d.key: d for d in _parse_example_file()}
    assert key in by_key, f"{key} is not declared in secrets.env.example"
    entry = by_key[key]
    assert entry.description, f"{key} lost its `# Used by:` line"
    assert entry.signup_url, f"{key} lost its `# Signup:` line"
    assert _BARE_HOST_RE.match(entry.signup_url), (
        f"{key} signup_url is not a bare host: {entry.signup_url!r}"
    )


# ── Commented keys stay in the registry ───────────────────────────────────────
#
# The template deliberately ships some assignments COMMENTED so that their
# genesis.yaml equivalents keep working — an uncommented assignment is copied into
# secrets.env on a fresh install and every accessor reads the environment first,
# which makes the documented yaml lever dead on arrival. But the registry parser
# anchored on `^KEY=`, so commenting a key ALSO deleted it from the dashboard's
# editable set, and the PUT route rejects anything absent from _KNOWN_KEYS. One
# fix bought another bug: the field vanishes and an update 4xx-es.

_COMMENTED_BUT_SETTABLE = [
    "OLLAMA_URL",
    "LM_STUDIO_URL",
    "LM_STUDIO_HEALTH_URL",
    "GENESIS_EMBED_PRIORITY_TIER",
    # These four were already commented — and already invisible — before the
    # local-inference URLs joined them. The gap predates that change.
    "TTS_ELEVENLABS_STABILITY",
    "GENESIS_DASHBOARD_API_AUTH",
]


@pytest.mark.parametrize("key", _COMMENTED_BUT_SETTABLE)
def test_commented_keys_remain_registered(key):
    """A commented default is still a settable key, and must stay editable."""
    keys = {d.key for d in _parse_example_file()}
    assert key in keys, (
        f"{key} is commented in secrets.env.example and dropped out of the dashboard "
        "registry — the field disappears and PUT rejects it as unknown."
    )


def test_uncommented_keys_are_unaffected(  ):
    """The control: ordinary assignments must still register exactly as before."""
    keys = {d.key for d in _parse_example_file()}
    for key in ("API_KEY_DEEPINFRA", "GENESIS_ENABLE_OLLAMA", "OLLAMA_URL"):
        assert key in keys, key


def test_prose_is_not_mistaken_for_a_key():
    """The other direction. `# NOTE: ...` and similar must not become keys.

    The commented-key pattern is deliberately narrow — an uppercase identifier
    immediately followed by `=` — because a looser one would turn ordinary
    commentary into phantom registry entries the PUT route would then accept.
    """
    keys = {d.key for d in _parse_example_file()}
    bogus = {k for k in keys if not k.replace("_", "").isalnum()}
    assert not bogus, f"non-key text registered as keys: {sorted(bogus)}"
    # Every registered key must actually appear as an assignment in the template.
    from genesis.env import repo_root

    text = (repo_root() / "secrets.env.example").read_text()
    for k in keys:
        assert f"{k}=" in text, f"{k} registered but never assigned in the template"
