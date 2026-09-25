"""An install may silence a NAMED ask; nothing here may silence a block.

``hook_ask_policy`` exists because a prompt the operator approves by reflex has
stopped being a decision. The risk it introduces is the obvious one — a knob that
disables a safety prompt — so the tests are organised around the ways that knob
could reach further than it should, rather than around its happy path:

  * **Polarity.** Every failure to read a policy (absent file, bad YAML, missing
    pyyaml, duplicate key, non-boolean value, unknown key) must produce the ASK,
    never the allow. There is no config that silences something by accident.
  * **Reach.** The policy is consulted only after the dispatched-session deny and
    only after every hard block has had its chance to fire. A suppressed ask on a
    compound command that also contains a hard block must still block.
  * **Classification.** Only the routine publish approvals are suppressible. The
    force-push ask and the two PR-hygiene asks stay unsuppressible, and an ask
    arm nobody classified is unsuppressible by construction — that default is
    what makes a future arm safe without anyone remembering this file.

The config lives outside the repo (``~/.genesis/config/genesis.yaml``) and is
absent in CI, so every test drives the ``_TEST_HOOK_ASK_POLICY`` seam. The
file-reading path itself is covered by pointing the module's own path constant at
a tmp file, so the seam cannot hide a parse bug in the real reader.
"""

from __future__ import annotations

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
gpg = private_module("git_push_guard", _HOOKS / "git_push_guard.py")


# ─── the module itself: every failure lands on ASK ───────────────────────────


def test_an_undeclared_ask_is_enabled(monkeypatch) -> None:
    """The public default, and what every clone with no local config gets."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "")
    assert policy.ask_suppressed("push_publish") is False
    assert policy.ask_suppressed("secrets_env") is False


@pytest.mark.parametrize("spelling", ["off", "false", "no", "n", "0", "OFF", "False"])
def test_every_falsy_spelling_suppresses(monkeypatch, spelling: str) -> None:
    """YAML's own boolean spellings, because the value is the ask's ENABLED
    state — an operator writing `off` must not have to learn a private vocabulary."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", f"push_publish={spelling}")
    assert policy.ask_suppressed("push_publish") is True


@pytest.mark.parametrize("spelling", ["on", "true", "yes", "1"])
def test_every_truthy_spelling_keeps_the_ask(monkeypatch, spelling: str) -> None:
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", f"push_publish={spelling}")
    assert policy.ask_suppressed("push_publish") is False


def test_a_key_outside_the_closed_set_suppresses_nothing(monkeypatch) -> None:
    """There is no wildcard and no `all`. A config naming an ask nobody
    classified is a config that does nothing — which is what stops a future
    settings file from reaching an arm this module has never heard of."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "force_push=off,all=off,merge_gate=off")
    for key in ("force_push", "all", "merge_gate", ""):
        assert policy.ask_suppressed(key) is False
    # And the declared-but-unknown keys did not leak into the classified ones.
    assert policy.ask_suppressed("push_publish") is False


def test_a_non_boolean_value_keeps_the_ask_and_says_so(monkeypatch, capsys) -> None:
    """A declared policy this module cannot honour is announced rather than
    silently replaced — the same treatment `_required_ci_workflows` gives its
    own discarded key, for the same reason."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_publish=maybe")
    assert policy.ask_suppressed("push_publish") is False
    assert "not a boolean" in capsys.readouterr().err


def test_the_seam_ignores_junk_entries(monkeypatch) -> None:
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "nonsense,=off,push_publish=off,   ")
    assert policy.ask_suppressed("push_publish") is True


# ─── the real reader, driven against a real file ─────────────────────────────


@pytest.fixture
def config_file(monkeypatch, tmp_path):
    """Point the module's config path at a tmp file and clear the seam, so these
    tests exercise the YAML reader rather than the injection shortcut."""
    path = tmp_path / "genesis.yaml"
    monkeypatch.delenv("_TEST_HOOK_ASK_POLICY", raising=False)
    monkeypatch.setattr(policy, "_CONFIG_PATH", str(path))
    return path


def test_a_missing_config_file_is_the_public_default(config_file) -> None:
    assert not config_file.exists()
    assert policy.ask_suppressed("push_publish") is False


def test_a_real_config_file_suppresses(config_file) -> None:
    config_file.write_text("github:\n  user: someone\nhooks:\n  asks:\n    push_publish: off\n")
    assert policy.ask_suppressed("push_publish") is True
    assert policy.ask_suppressed("secrets_env") is False  # untouched key unaffected


def test_unparseable_yaml_keeps_the_ask(config_file, capsys) -> None:
    config_file.write_text("hooks:\n  asks:\n   - this is not a mapping\n  : :\n")
    assert policy.ask_suppressed("push_publish") is False
    assert "could not be read" in capsys.readouterr().err


def test_a_duplicate_hooks_section_refuses_to_guess(config_file, capsys) -> None:
    """yaml.safe_load silently keeps the LAST value for a repeated key, so a
    badly-merged config could flip a policy with nothing to show for it.

    Only `hooks:` is doubled here; `asks:` appears once. The duplicate check's
    two clauses are exercised SEPARATELY on purpose — a config that doubles both
    passes on whichever clause still works, so one combined test stays green with
    either half deleted. (Measured: it did.)"""
    config_file.write_text("hooks:\n  asks:\n    push_publish: off\nhooks:\n  other: 1\n")
    assert policy.ask_suppressed("push_publish") is False
    assert "duplicate" in capsys.readouterr().err


def test_a_duplicate_asks_section_refuses_to_guess(config_file, capsys) -> None:
    """The other clause: one `hooks:`, two `asks:` nested under it."""
    config_file.write_text(
        "hooks:\n  asks:\n    push_publish: off\n  asks:\n    push_publish: on\n"
    )
    assert policy.ask_suppressed("push_publish") is False
    assert "duplicate" in capsys.readouterr().err


def test_a_non_mapping_asks_section_keeps_the_ask(config_file) -> None:
    config_file.write_text("hooks:\n  asks: off\n")
    assert policy.ask_suppressed("push_publish") is False


def test_a_bodiless_asks_key_keeps_the_ask(config_file) -> None:
    """YAML loads a bodiless key as None, not {} — the shape that has already
    produced one defect class in this repo's routing overlay."""
    config_file.write_text("hooks:\n  asks:\n")
    assert policy.ask_suppressed("push_publish") is False


def test_the_allow_reason_names_the_setting(monkeypatch) -> None:
    """'Off' means stop asking, never stop telling: a reader of the transcript
    has to be able to tell a suppressed prompt from a guard with nothing to say."""
    reason = policy.suppressed_reason("secrets_env", "cat secrets.env")
    assert "hooks.asks.secrets_env" in reason
    assert "cat secrets.env" in reason
    assert "Blocks are unaffected" in reason


# ─── needs_user.decide: the dispatched deny is out of reach ──────────────────


def _decision(doc: dict) -> str:
    return doc["hookSpecificOutput"]["permissionDecision"]


def test_decide_without_a_key_always_asks(monkeypatch) -> None:
    """Existing call sites are unchanged: no key, no policy, no suppression."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_publish=off,secrets_env=off")
    monkeypatch.setattr(needs_user, "is_dispatched", lambda: False)
    assert _decision(needs_user.decide("something", "because")) == "ask"


def test_decide_honours_a_suppressed_key(monkeypatch) -> None:
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "secrets_env=off")
    monkeypatch.setattr(needs_user, "is_dispatched", lambda: False)
    doc = needs_user.decide("access secrets.env", "because", ask_key="secrets_env")
    assert _decision(doc) == "allow"
    assert "hooks.asks.secrets_env" in doc["hookSpecificOutput"]["permissionDecisionReason"]


def test_an_older_needs_user_without_ask_key_still_prompts(monkeypatch, tmp_path) -> None:
    """REVERSE version skew, and the fail direction is the point.

    The secrets guard has no run_guard wrapper and no try/except around main(),
    and its own docstring records that a crash exits non-zero — which Claude Code
    treats as NON-blocking. So an uncaught `TypeError: decide() got an unexpected
    keyword argument 'ask_key'`, reachable purely by deploying this file next to
    an older needs_user.py, would let the credentials access through with no
    prompt, no block and no record: the one outcome the guard exists to prevent.

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

    import contextlib as _ctx
    import io as _io

    buf = _io.StringIO()
    with _ctx.redirect_stdout(buf):
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
    doc = needs_user.decide("access secrets.env", "because", ask_key="secrets_env")
    assert _decision(doc) == "deny"


# ─── git_push_guard: which arms the knob can and cannot reach ────────────────


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "--quiet", "-b", "main"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "T"],
        ["checkout", "--quiet", "-b", "feat/x"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=30)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], capture_output=True, timeout=30)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "commit", "-qm", "seed"],
        capture_output=True,
        timeout=30,
    )
    return repo


def _run(monkeypatch, tmp_path, capsys, command: str):
    repo = _repo(tmp_path)
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
    monkeypatch.setattr(gpg.sys, "stdin", io.StringIO(json.dumps(payload)))
    rc = gpg.main()
    out = capsys.readouterr()
    return rc, out.out, out.err


@pytest.fixture
def first_push(monkeypatch):
    """A first push of a branch that is not yet on the remote — the arm the knob
    is FOR. Held to the same seams the sibling adjacency tests use."""
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: False)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: set())
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)
    # Short-circuited on this path (push_allow_reason is None), but patched
    # anyway: a real call here would reach the network from a unit test.
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 1)


def test_the_first_push_ask_is_the_baseline(monkeypatch, tmp_path, capsys, first_push) -> None:
    """The control arm. Without it, a green suppression test proves nothing —
    an allow could be the guard having no opinion rather than the policy working."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "")
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD")
    assert rc == 0
    assert _decision(json.loads(out)) == "ask", (out, err)


def test_the_first_push_ask_is_suppressible(monkeypatch, tmp_path, capsys, first_push) -> None:
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_publish=off")
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD")
    assert rc == 0
    doc = json.loads(out)
    assert _decision(doc) == "allow", (out, err)
    assert "hooks.asks.push_publish" in doc["hookSpecificOutput"]["permissionDecisionReason"]


def test_the_no_open_pr_ask_survives_the_knob(monkeypatch, tmp_path, capsys) -> None:
    """Classified unsuppressible on purpose: a public branch with no PR runs
    neither CI nor the leak scan, which is the state the repo's publish rule
    forbids. Turning off the routine publish prompt must not turn this off."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_publish=off")
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: set())
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 0)
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push")
    assert rc == 0
    doc = json.loads(out)
    assert _decision(doc) == "ask", (out, err)
    assert "NO OPEN PR" in doc["hookSpecificOutput"]["permissionDecisionReason"]


def test_the_force_push_ask_survives_the_knob(monkeypatch, tmp_path, capsys) -> None:
    """A force push to a remote provably disjoint from origin is the ONE force
    arm that asks rather than blocks outright — and it is destructive, so it is
    classified unsuppressible. Without this test a mutation that classified it
    `push_publish` would survive, because no other test reaches this arm."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_publish=off")
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)
    monkeypatch.setattr(gpg, "_resolve_push_remote", lambda *a, **k: "mirror")
    monkeypatch.setattr(gpg, "_push_dest_urls", lambda *a, **k: {"ssh://elsewhere/x.git"})
    monkeypatch.setattr(
        gpg,
        "_remote_push_urls",
        lambda name, **k: {"ssh://github/owner-repo.git"} if name == "origin" else set(),
    )

    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push --force mirror feat/x")
    assert rc == 0, (rc, out, err)
    doc = json.loads(out)
    assert _decision(doc) == "ask", (out, err)
    assert "FORCE" in doc["hookSpecificOutput"]["permissionDecisionReason"]


def test_the_close_then_push_ask_survives_the_knob(monkeypatch, tmp_path, capsys) -> None:
    """Closing a PR in the same command invalidates the state the silent re-push
    allow was read from. That is a correctness arm, not routine publish friction,
    so the knob does not reach it — and nothing else in this file exercises it."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_publish=off")
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: set())
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 1)
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run(monkeypatch, tmp_path, capsys, "gh pr close 7 && git push")
    assert rc == 0, (rc, out, err)
    doc = json.loads(out)
    assert _decision(doc) == "ask", (out, err)
    assert "CLOSES a pull request" in doc["hookSpecificOutput"]["permissionDecisionReason"]


def test_an_inline_hard_block_still_blocks_with_the_ask_off(
    monkeypatch, tmp_path, capsys, first_push
) -> None:
    """A suppressed push must not chaperone a blocked action through. This arm
    covers the blocks that return 2 INLINE, long before the tail — reaching them
    would take a suppression that short-circuits the whole of main()."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_publish=off")
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push && git commit --no-verify -m x")
    assert rc == 2, (rc, out, err)


def test_a_deferred_hard_block_outranks_a_suppressed_ask(
    monkeypatch, tmp_path, capsys, first_push
) -> None:
    """The arm that actually pins the ORDERING, and the reason the one above is
    not enough on its own: three denies (`blind_spot_deny`,
    `round_compound_deny`, `round_autonomous_deny`) are deliberately deferred to
    the same tail the ask is consumed at, so only they can catch a suppression
    that jumped the queue.

    A carrier segment (`eval git push`) is a payload this guard cannot recover,
    which sets `blind_spot_deny`. Paired with a first push whose ask is
    suppressed, the command must still exit 2. MEASURED: mutating the tail to
    consult the policy one position earlier left the --no-verify test above
    green and turned only this one red."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_publish=off")
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD && eval git push")
    assert rc == 2, (rc, out, err)
    assert "BLOCKED" in err, (out, err)


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push origin main",
        "git push -u origin main",
        # The two that isolate the CURRENT-branch clause. Each command above is
        # already refused by a different clause (no positionals, or a refspec
        # that names main), so without these a mutation deleting the
        # `cur in ("main","master")` check survives the entire file. MEASURED: it did.
        "git push -u origin HEAD",
        "git push origin HEAD",
    ],
)
def test_a_push_to_the_default_branch_is_never_suppressible(
    monkeypatch, tmp_path, capsys, command: str
) -> None:
    """A direct push to main/master must keep prompting however the knob is set.

    This is the arm that makes `ask_class` default to None rather than to a
    class. The `else` branch that catches this reads like "the ordinary push
    ask", but it is the catch-all for everything the guard could NOT establish as
    a first push of your own feature branch — being on main among them.

    MEASURED before the fix: with `push_publish: off`, all three commands below
    returned `allow` with no prompt. There is no hard block in front of them —
    unlike a merge into main, a PUSH to main was only ever an ask — so the knob
    was silencing the single thing this guard is named for."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "--quiet", "-b", "main"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "T"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=30)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], capture_output=True, timeout=30)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "commit", "-qm", "seed"],
        capture_output=True,
        timeout=30,
    )
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_publish=off")
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
    monkeypatch.setattr(gpg.sys, "stdin", io.StringIO(json.dumps(payload)))
    rc = gpg.main()
    out = capsys.readouterr()
    assert rc in (0, 2), (rc, out.out, out.err)
    if out.out.strip():
        assert _decision(json.loads(out.out)) != "allow", (out.out, out.err)


def _parsed_push_seg(command: str):
    """The push segment as main() sees it, with `argv` POPULATED.

    `split_segments` returns raw strings and leaves `argv` as None, and the
    predicate under test correctly refuses a segment it cannot read — so a
    harness built on it returns False for every input and every negative row
    passes for the wrong reason. Measured: it did, on the first draft of this
    file, and the three positive rows were the only thing that revealed it.
    `analyze_checked` is the parse main() actually runs."""
    segs, _blind = gpg.analyze_checked(command)
    push = [s for s in segs if s.exe == "git" and gpg.git_subcommand(s.argv) == "push"]
    assert push, f"no push segment parsed out of {command!r}"
    assert getattr(push[0], "argv", None), f"argv not populated for {command!r}"
    return push[0]


@pytest.mark.parametrize(
    ("command", "suppressible", "why"),
    [
        ("git push -u origin HEAD", True, "the form this repo's workflow prescribes"),
        ("git push origin feat/x", True, "an explicit publish of a feature branch"),
        ("git push --quiet -u origin HEAD", True, "a ref-set-neutral flag"),
        ("git push origin main", False, "names the DEFAULT BRANCH as the destination"),
        ("git push origin master", False, "same, other spelling"),
        ("git push origin HEAD:refs/heads/main", False, "a src:dst refspec reaching main"),
        ("git push origin HEAD:feat/y", False, "any colon refspec — destination is not cur"),
        ("git push --all origin", False, "--all broadens the ref set"),
        ("git push --tags origin", False, "--tags broadens the ref set"),
        ("git push --delete origin feat/x", False, "--delete is not a publish"),
        ("git push origin feat/x feat/y", False, "several refspecs"),
        ("git push", False, "bare — reaching this arm means repo config is NOT simple"),
        ("git push origin", False, "remote-only — same, the ref set comes from config"),
        ("git push --repo origin", False, "--repo redirects the destination"),
    ],
)
def test_the_routine_publish_predicate_admits_only_what_it_claims(
    command: str, suppressible: bool, why: str
) -> None:
    """The predicate that decides whether an ask in the catch-all arm may be
    silenced. Driven directly, because the arm it guards is reached by several
    shapes at once and a behavioural test of one of them would say nothing about
    the others.

    `git push origin main` is the row that matters most: it passes every clause
    about the CURRENT branch (you are on a feature branch) and is still a push to
    the default branch. A check on `cur` alone would admit it, and so would a
    check on the refspec alone when run from main."""
    seg = _parsed_push_seg(command)
    got = gpg._push_is_routine_feature_publish(seg, "feat/x", cwd_known=True)
    assert got is suppressible, f"{command!r} — {why} (got {got})"


@pytest.mark.parametrize(
    ("cur", "cwd_known"),
    [("main", True), ("master", True), (None, True), ("", True), ("feat/x", False)],
)
def test_the_routine_publish_predicate_refuses_an_unestablished_context(
    cur, cwd_known: bool
) -> None:
    """Being ON the default branch, having no branch at all (detached HEAD), or
    not knowing which checkout this is are each enough on their own. The command
    is the same routine-looking push in every case — the context is what
    disqualifies it."""
    seg = _parsed_push_seg("git push -u origin HEAD")
    assert gpg._push_is_routine_feature_publish(seg, cur, cwd_known=cwd_known) is False


def test_an_absent_policy_module_leaves_the_ask_standing(tmp_path) -> None:
    """The import fallback, exercised by actually removing the module.

    Its sibling below monkeypatches `ask_suppressed` on an already-imported
    guard, which is a weaker claim than it looks: it never runs the
    `except Exception:` stub, so a mutation deleting that stub outright would
    survive. Both guards are copied to a tmp tree WITHOUT hook_ask_policy.py and
    driven as subprocesses with the knob switched ON, because that is the only
    way to reach an import that fails — this test module's own top-level
    `private_module(...)` would take collection down with it otherwise.

    The direction is the whole point: this module can only turn an ask INTO an
    allow, so a half-deployed hook tree must cost an extra prompt, never one
    fewer."""
    tree = tmp_path / "hooks"
    shutil.copytree(_HOOKS, tree)
    (tree / "hook_ask_policy.py").unlink()
    shutil.rmtree(tree / "__pycache__", ignore_errors=True)
    assert not (tree / "hook_ask_policy.py").exists()

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "--quiet", "-b", "feat/skew"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "T"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=30)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], capture_output=True, timeout=30)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "commit", "-qm", "seed"],
        capture_output=True,
        timeout=30,
    )

    env = {**os.environ, "_TEST_HOOK_ASK_POLICY": "push_publish=off,secrets_env=off"}
    for script, command, cwd in (
        ("git_push_guard.py", "git push -u origin HEAD", str(repo)),
        # `~/genesis/secrets.env` rather than an absolute path: the guard matches
        # by RESOLVED path, tilde expansion included, and a literal home
        # directory would both hardcode one developer's layout and put a
        # username into a public repo. Same spelling the sibling suite uses.
        ("secrets_env_access_guard.py", "cat ~/genesis/secrets.env", str(repo)),
    ):
        proc = subprocess.run(
            [sys.executable, str(tree / script)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}),
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        assert proc.stdout.strip(), (script, proc.returncode, proc.stderr[-400:])
        assert _decision(json.loads(proc.stdout)) == "ask", (script, proc.stdout)


def test_a_broken_policy_module_leaves_the_ask_standing(
    monkeypatch, tmp_path, capsys, first_push
) -> None:
    """Version skew: this module can only turn an ask into an allow, so an
    absent or broken copy must fall back to asking. Simulated by the same stub
    the guard installs when the import fails."""
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_publish=off")
    monkeypatch.setattr(gpg, "ask_suppressed", lambda key: False)
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD")
    assert rc == 0
    assert _decision(json.loads(out)) == "ask", (out, err)
