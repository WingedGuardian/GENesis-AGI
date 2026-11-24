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
