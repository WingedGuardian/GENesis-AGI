"""The subagent git/gh predicates, called on an argv rather than a parsed segment.

They moved from ``git_push_guard.py`` into ``scripts/hooks/command_policy.py`` so
a second caller can build on them. The guard's own tests
(``test_push_publish_ask_switch.py``) still cover the policy through the guard;
these pin the argv entry point and that the guard routes through it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import private_module

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
gpg = private_module("git_push_guard", _HOOKS / "git_push_guard.py")
cp = private_module("command_policy", _HOOKS / "command_policy.py")


@pytest.mark.parametrize(
    ("argv", "refusal"),
    [
        (["git", "log", "-1"], None),
        (["git", "commit", "-m", "x"], None),  # a subagent commits its own work
        (["git", "push", "origin", "HEAD"], "git push"),
        (["git", "grep", "-O", "x"], "git grep -O (runs a program)"),
        (["git", "grep", "--open-files", "x"], "git grep --open-files (runs a program)"),
        (["git", "rebase", "--exec", "make"], "git rebase --exec"),
        (["git", "submodule", "foreach", "ls"], "git submodule foreach"),
        (["gh", "pr", "view", "1"], None),
        (["gh", "pr", "comment", "1", "-b", "x"], "gh pr comment"),
        (["gh", "api", "repos/o/r/pulls"], None),
        (["gh", "api", "-X", "POST", "repos/o/r/issues"], "gh api (not a plain read)"),
        (["gh", "api", "repos/o/r/issues", "-f", "title=x"], "gh api (not a plain read)"),
        (["gh", "api", "graphql", "-f", "query=mutation { x }"], "gh api (not a plain read)"),
        (["gh", "api", "graphql", "-f", "query=query { viewer { login } }"], None),
        (
            ["gh", "--yes", "o/r", "repo", "delete"],
            "gh o/r (a flag before the command hides which command runs)",
        ),
        (["ls", "-la"], None),  # not git or gh: not this rule's subject
    ],
)
def test_subagent_refusal_on_argv(argv, refusal):
    assert cp.subagent_refusal(argv[0], argv) == refusal


def test_an_unresolved_verb_is_refused():
    argv = ["git", "$SUB"]
    assert (
        cp.subagent_refusal("git", argv, verb_unresolved=True)
        == "git (its subcommand is built at run time)"
    )


def test_the_guard_routes_segments_through_the_argv_policy(monkeypatch):
    seen = []

    def spy(exe, argv, *, verb_unresolved=False):
        seen.append((exe, argv, verb_unresolved))
        return "spied"

    monkeypatch.setattr(gpg, "subagent_refusal", spy)
    seg = SimpleNamespace(exe="gh", argv=("gh", "pr", "view"), verb_unresolved=True)
    assert gpg._subagent_refusal(seg) == "spied"
    assert seen == [("gh", ["gh", "pr", "view"], True)]
