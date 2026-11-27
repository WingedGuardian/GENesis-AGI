"""One router decides a session's type; the session-type skills point to it instead
of typing a session by its task or by being loaded (spec-is-the-contract, PR 2)."""

from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2] / ".claude" / "skills"
ROUTER = SKILLS / "genesis-development" / "SKILL.md"


def test_router_defaults_to_spec_and_names_one_type_per_session():
    text = " ".join(ROUTER.read_text(encoding="utf-8").split())
    assert "none fits" not in text
    assert "load both, in order" not in text
    assert "leaves it a **spec session**, the default" in text
    assert "names a task, not a type" in text
    assert "keeps it, and writes it as the first word of its mission line" in text
    assert "a type word counts only when it names the session" in text
    assert "without `GENESIS_SESSION_SUPERVISED=1`" in text
    assert "`GENESIS_BUILDER_KEY`" in text
    assert "A closing or validating session, foreground or dispatched" in text
    assert '("close out the open PRs") names a closing session' in text
    assert '("validate the merged PRs") names a validating session' in text
    assert "has no session type: it stays universal" in text
    assert "A board builder plans first and asks the owner to approve its plan" in text


def test_the_owners_words_outrank_a_handoff_which_outranks_a_spec():
    text = " ".join(ROUTER.read_text(encoding="utf-8").split())
    order = (
        "takes its type from, in this order: the owner's words naming a type",
        "a handoff file's `For: <type> session` line;",
        "a handed-over spec whose frontmatter status counts as approved",
    )
    at = [text.find(part) for part in order]
    assert -1 not in at, order[at.index(-1)]
    assert at == sorted(at)


def test_a_saved_issue_is_not_checked_for_spec_sections():
    router = " ".join(ROUTER.read_text(encoding="utf-8").split())
    assert "`~/.genesis/output/specs/issue-<N>.md`" in router
    analyze = SKILLS / "spec-session" / "references" / "spec-analyze.md"
    text = " ".join(analyze.read_text(encoding="utf-8").split())
    assert (
        "A saved issue (`issue-<N>.md`) is not checked by this pass: its contract is the nine premise items."
        in text
    )


def test_skip_list_counts_with_pr_shape_and_claude_md_points_to_it():
    repo = SKILLS.parents[1]
    skill = " ".join((SKILLS / "spec-session" / "SKILL.md").read_text(encoding="utf-8").split())
    assert (
        "a change of 10 or fewer counted lines, as `python3 scripts/pr_shape.py --base <base>` counts them"
        in skill
    )
    assert (
        "checks it again after its commit and before any push, and a change over 10 lines stops there,"
        " with the commit unpushed, and goes to the owner." in skill
    )
    assert "except for the 10-line entry (checked after its commit, above)" in skill
    claude = " ".join((repo / "CLAUDE.md").read_text(encoding="utf-8").split())
    assert "write no code unless the change is on the spec skill's skip list" in claude


def test_board_builder_prompt_plans_before_building():
    prompt = (SKILLS.parents[1] / "config" / "BUILDER_PROMPT.md").read_text(encoding="utf-8")
    assert "not for a plan" in prompt
    assert "Before your first edit, show the owner the plan" in prompt


def test_sibling_skills_defer_to_the_router():
    stale = {
        "build-session/SKILL.md": "Loading this skill makes you a **build session**",
        "closing-session/SKILL.md": "you are a build session — load `build-session` instead",
        "validating-merges/SKILL.md": 'If the job is "make the change", you are a build session',
    }
    for rel, sentence in stale.items():
        text = (SKILLS / rel).read_text(encoding="utf-8")
        assert sentence not in text, rel
        assert "Which session are you?" in text, rel
