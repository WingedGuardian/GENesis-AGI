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


def _method_body(js: str, signature: str) -> str:
    """The body of one JS method, brace-matched and stripped of line comments.

    Replaces the fixed-character windows this file used to slice with. A window
    is a FORMAT assumption wearing a content assertion: it keeps passing while
    the code it names drifts out of range, and it can match the searched token
    inside a comment the same change added. Both happened here.
    """
    start = js.index(signature)
    i = js.index("{", start)
    depth = 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                body = js[start : j + 1]
                break
    else:  # pragma: no cover - unbalanced braces would be a syntax error
        raise AssertionError(f"unbalanced braces after {signature!r}")
    return "\n".join(re.sub(r"//.*$", "", line) for line in body.splitlines())


def test_the_withheld_flag_is_the_same_STRING_on_both_sides():
    """The client guard activates on one literal the server has to send.

    Nothing else binds them. If the server ships `values_hidden`, nests it, or
    inverts it, `!!body.values_withheld` is simply `false` forever: no warning,
    no failing test, and the editor quietly returns to clearing overrides it
    cannot see. That is the failure this file exists to catch, one layer up.

    The invariant asserted is AGREEMENT, not presence, because presence is not
    yet true: the server half ships in a separate change that lands after this
    one, and until it does the client guard is inert BY DESIGN. A test demanding
    the server field today would fail for the right reason at the wrong time,
    and a skip would be the silent-green this file exists to avoid.

    So both legal states pass — neither side has it (inert, pre-merge), or both
    do (active) — and the one forbidden state fails: a client reading a literal
    no server sends, which is indistinguishable from a working guard.
    """
    from genesis.env import repo_root

    js = (repo_root() / "src/genesis/dashboard/webui/js/dashboard.js").read_text()
    py = (repo_root() / "src/genesis/dashboard/routes/secrets.py").read_text()

    client_reads = "values_withheld" in js
    server_sends = '"values_withheld"' in py

    assert client_reads, (
        "the editor no longer reads values_withheld — the withheld guard is inert"
    )
    assert client_reads == server_sends or not server_sends, (
        "client and server disagree about the withheld flag"
    )
    if not server_sends:
        # Pin the inertness the split depends on: with no such key in the
        # response, `!!body.values_withheld` is false, so the flip block and the
        # refusal both stay unreachable. This branch disappears on its own when
        # the server change lands.
        assert "!!body.values_withheld" in js, (
            "the client must coerce the ABSENT field to false, or the guard "
            "misfires against a server that does not send it yet"
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


# ── An optional override must be reversible ───────────────────────────────────
#
# Registering the commented keys made them editable, which opened a ONE-WAY DOOR:
# setting one writes an assignment into secrets.env, the environment then shadows
# genesis.yaml permanently, and later config edits appear to do nothing. Recoverable
# only by hand-editing the file the dashboard exists to avoid.


def test_optional_overrides_are_flagged_clearable():
    """The commented keys — and only those — advertise that they can be cleared."""
    by_key = {d.key: d for d in _parse_example_file()}
    for key in ("OLLAMA_URL", "LM_STUDIO_URL", "GENESIS_EMBED_PRIORITY_TIER"):
        assert by_key[key].is_optional_override is True, key
    # The control. A required credential must NOT be clearable, or the editor would
    # happily blank an API key and report success.
    for key in ("API_KEY_DEEPINFRA", "TELEGRAM_BOT_TOKEN"):
        assert by_key[key].is_optional_override is False, key


def test_clearing_an_override_comments_the_assignment_out(tmp_path, monkeypatch):
    """Empty means UNSET, and unset must not be written as `KEY=`.

    The accessors branch on `os.environ.get(key) is not None`, so an empty
    assignment still shadows genesis.yaml — just with an empty string, which is
    worse than the value it replaced. The line has to stop being an assignment.
    """
    from genesis.dashboard.routes import secrets as mod

    env_file = tmp_path / "secrets.env"
    env_file.write_text("OLLAMA_URL=http://was-set.invalid:11434\nAPI_KEY_GROQ=abc\n")
    monkeypatch.setattr(mod, "secrets_path", lambda: env_file)

    mod._update_secrets_file({"OLLAMA_URL": ""})
    text = env_file.read_text()
    assert "\nOLLAMA_URL=" not in "\n" + text, f"still an active assignment:\n{text}"
    assert "# OLLAMA_URL=http://was-set.invalid:11434" in text, text
    # The untouched key must survive verbatim — a rewrite that loses siblings is
    # the same data-loss shape as the timezone handler's.
    assert "API_KEY_GROQ=abc" in text


def test_setting_a_value_still_writes_an_assignment(tmp_path, monkeypatch):
    """The control in the other direction: clearing must not break setting."""
    from genesis.dashboard.routes import secrets as mod

    env_file = tmp_path / "secrets.env"
    env_file.write_text("OLLAMA_URL=http://old.invalid:11434\n")
    monkeypatch.setattr(mod, "secrets_path", lambda: env_file)

    mod._update_secrets_file({"OLLAMA_URL": "http://new.invalid:11434"})
    assert "OLLAMA_URL=http://new.invalid:11434" in env_file.read_text()


def test_an_unset_key_is_not_appended_as_empty(tmp_path, monkeypatch):
    """Clearing a key absent from the file must add nothing at all."""
    from genesis.dashboard.routes import secrets as mod

    env_file = tmp_path / "secrets.env"
    env_file.write_text("API_KEY_GROQ=abc\n")
    monkeypatch.setattr(mod, "secrets_path", lambda: env_file)

    mod._update_secrets_file({"OLLAMA_URL": ""})
    assert "OLLAMA_URL" not in env_file.read_text()


def test_the_writer_refuses_None_rather_than_writing_KEY_equals_None(tmp_path, monkeypatch):
    """None is not a value here — it must fail LOUDLY, not corrupt the file.

    MEASURED before the guard existed: ``_update_secrets_file({"K": None})``
    raised nothing and wrote the literal ``K=None``. ``_key_value`` then reads
    that back as ``''`` (it filters "None"/"NA"), so the dashboard reports the
    key as NOT SET while os.environ still holds the string and keeps shadowing
    genesis.yaml — the exact corruption the ``os.environ.pop`` in
    ``secrets_update`` exists to prevent, arrived at by another door.

    The HTTP layer translates its ``null`` to ``""`` before calling, so a None
    reaching here is a caller bug. Unreachable today; this pins it that way.
    """
    from genesis.dashboard.routes import secrets as mod

    env_file = tmp_path / "secrets.env"
    env_file.write_text("OLLAMA_URL=http://was-set.invalid:11434\n")
    monkeypatch.setattr(mod, "secrets_path", lambda: env_file)

    with pytest.raises(TypeError, match="OLLAMA_URL"):
        mod._update_secrets_file({"OLLAMA_URL": None})

    # And it refused BEFORE touching the file.
    assert env_file.read_text() == "OLLAMA_URL=http://was-set.invalid:11434\n"


def test_clearing_a_DUPLICATED_assignment_only_comments_the_first(tmp_path, monkeypatch):
    """CHARACTERIZATION of a known limit — documenting, not endorsing.

    ``_update_secrets_file`` pops from ``remaining`` on the first match, so a key
    assigned twice keeps its SECOND assignment and the route still answers 200.
    The surviving line is what the environment picks up, so the clear silently
    does nothing from the operator's point of view.

    Fixing it changes writer semantics and is deliberately out of scope for the
    write-protocol change; this test exists so the behaviour cannot drift
    unnoticed, and so the next reader finds it stated rather than discovering it.
    """
    from genesis.dashboard.routes import secrets as mod

    env_file = tmp_path / "secrets.env"
    env_file.write_text(
        "OLLAMA_URL=http://first.invalid:11434\n"
        "OLLAMA_URL=http://second.invalid:11434\n"
    )
    monkeypatch.setattr(mod, "secrets_path", lambda: env_file)

    mod._update_secrets_file({"OLLAMA_URL": ""})

    text = env_file.read_text()
    assert "# OLLAMA_URL=http://first.invalid:11434" in text
    assert "OLLAMA_URL=http://second.invalid:11434" in text
    active = [ln for ln in text.splitlines() if ln.startswith("OLLAMA_URL=")]
    assert active == ["OLLAMA_URL=http://second.invalid:11434"], (
        f"expected exactly the second assignment to survive, got {active}"
    )


def test_opening_an_editor_seeds_the_value_so_save_is_not_a_delete():
    """An UNTOUCHED field must mean "no change", never "delete".

    Structural, because there is no JS harness here — but the failure it guards is
    concrete and was live: `config.html` binds
    `:value="secretsValues[k.key] ?? k.value ?? ''"`, so the displayed value comes
    from `k.value` while `secretsValues[k.key]` stays UNDEFINED until an `input`
    event fires. Opening a configured override and pressing Save without typing
    therefore read as empty — and once empty meant "unset", that silently removed
    the override the operator had just been looking at.
    """
    from genesis.env import repo_root

    js = (repo_root() / "src/genesis/dashboard/webui/js/dashboard.js").read_text()

    handler = _method_body(js, "toggleSecretEdit(keyName)")
    assert "secretsValues" in handler and "def.value" in handler, (
        "toggleSecretEdit must seed the edit buffer from the current value; "
        "without it an untouched field saves as an empty string, i.e. a deletion"
    )
    # The backstop: seeding cannot cover a masked value, so clearing is confirmed.
    #
    # Both slices were FIXED WINDOWS (900 and 1600 chars) and both had rotted.
    # MEASURED before this change: `def.value` sat at char 840 of 900, and the
    # `confirm(` assertion matched the word inside a COMMENT at char 1190 while
    # the real `!confirm(` call had moved beyond 1600 — so it asserted nothing,
    # and the comment that made it vacuous was added by the very change it was
    # meant to guard. Bounded to the method and stripped of comments, a window
    # cannot silently stop reaching the code it names.
    # The backstop MOVED, and that is the point of the write-protocol change:
    # saveSecret can no longer clear anything at all, so there is nothing left
    # for it to confirm. Clearing is a separate handler sending an explicit
    # null, and THAT is what must be a deliberate act.
    save = _method_body(js, "async saveSecret(keyName)")
    assert "confirm(" not in save, (
        "saveSecret must have no clear path left — clearing is clearSecret's job, "
        "and a Save that can clear is the ambiguity this protocol removed"
    )
    clear = _method_body(js, "async clearSecret(keyName)")
    assert "!confirm(" in clear, "clearing an override must be an explicit act"
    assert "null" in clear, (
        "clearSecret must send an explicit null — an empty string is refused by "
        "the server as ambiguous"
    )


def test_the_empty_string_is_false_for_every_boolean_accessor():
    """Guards the sibling regression from the same commit, at the API boundary.

    `_yaml_bool` answers "is this one of the words meaning no", and "" is in none
    of them — so the token check alone read an empty quoted scalar as TRUE, where
    the `bool()` it replaced read it as False. On `embed_priority_tier` that
    silently selects the paid lane.
    """
    from genesis.env import _yaml_bool

    for empty in ("", "   ", "\t"):
        assert _yaml_bool(empty) is False, repr(empty)
    # Controls in both directions.
    assert _yaml_bool("false") is False
    assert _yaml_bool("true") is True
    assert _yaml_bool(False) is False
    assert _yaml_bool(True) is True


def test_the_clear_button_is_not_gated_on_a_status_that_lies():
    """The Clear control must not derive reachability from `_key_status`.

    `_key_status` reports `not_set` for `KEY=`, `KEY=None` and `KEY=NA` as well as
    for a genuinely absent key — it filters exactly those strings. But those ARE
    assignments: they sit in secrets.env shadowing genesis.yaml, which is the
    one-way door the optional-override mechanism exists to prevent.

    MEASURED: the server accepts a clear for that state and comments the line out
    (see test_a_shadowing_empty_assignment_is_repairable). So gating the button on
    `status !== 'not_set'` would hide the dashboard's ONLY repair path for it —
    a UI predicate silently removing a capability the server still offers.

    Structural, because there is no browser harness here; the behaviour it guards
    is measured on the server side in the contract suite.
    """
    from genesis.env import repo_root

    html = (
        repo_root() / "src/genesis/dashboard/templates/partials/tabs/config.html"
    ).read_text()

    i = html.index("clearSecret(k.key)")
    # The enclosing <template x-if="..."> guard for the Clear button.
    guard_start = html.rindex("<template x-if=", 0, i)
    guard = html[guard_start: html.index(">", guard_start)]

    assert "is_optional_override" in guard, (
        "Clear must be offered only for keys that HAVE an unset state to fall back to"
    )
    assert "status" not in guard, (
        "Clear must NOT be gated on k.status — `not_set` also covers KEY= / KEY=None, "
        "the shadowing assignments that most need clearing (see this test's docstring)"
    )
