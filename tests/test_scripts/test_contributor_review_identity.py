"""The contributor-review workflow must post as a maintainer, and must stay inert.

Two independent properties, both load-bearing, both previously unenforced.

IDENTITY. The reviewer resolves who asked for a review from the comment AUTHOR.
A request authored by github-actions[bot] is refused on identity alone, in
about three seconds, without the diff ever being read -- MEASURED 2026-09-25 on
PRs #2237, #2238 and #2360, which are every request this workflow has ever
posted, and 3 of 3 were refused that way.

The control is the timeline, not the author. The reviewer ALSO refuses when the
account quota is exhausted, with different text, and both refusals were
observed. MEASURED: quota refusals ran until 2026-09-25T04:57:22Z and the first
delivered review followed at 05:39:52Z -- yet the bot-authored request at
17:38:36Z still drew the IDENTITY refusal, roughly 12.7 hours into healthy
quota, while maintainer-authored requests in that window were being answered
with real reviews.

Note the weaker claim is the true one. A maintainer request on #2237 drew a
QUOTA refusal, so "maintainer requests always succeed" is false. What holds is
that no maintainer request has ever drawn an IDENTITY refusal, and no bot
request has ever drawn anything else.

So the post step must carry a non-default token, and the step that decides
eligibility must NOT -- widening the eligibility step instead would put a
privileged credential next to every author-influenced query in the file.

INERTNESS. This is a pull_request_target workflow, so an outside contributor PR
runs it with repository secrets in scope. Its safety is entirely structural: it
never obtains the head code, so nothing an author writes is executed. That
property used to be belt-and-braces; once a maintainer token is present it is
the only thing standing between an outside author and that credential.

Polarity is ALLOWLIST for steps, and the checks enumerate EVERY JOB. Both
choices are load-bearing and both were learned from a checker that lacked them:
a denylist passes the next spelling nobody thought of, and a single-job walk
passes a SECOND job that checks out head code beside the same secret.

Every invariant below is exercised in BOTH directions -- the shipped file must
be clean, and a deliberately broken copy must be caught. An assertion group
that only ever sees a clean fixture passes just as well when it checks nothing.
Where an assertion could be satisfied by a string that does not actually BIND
the behaviour, it checks the binding instead: `if: always()` is non-empty and
would pass a presence test while disabling the gate it is supposed to be.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO / ".github/workflows/contributor-review.yml"

#: The only action this workflow may use, and it must be pinned to a commit SHA.
#: A tag is re-pointable by whoever controls it, which makes it a choice about
#: what code runs next to a maintainer token. Note this pattern ACCEPTS the
#: pinned form specifically -- an earlier version of this allowlist matched the
#: literal "@v9" and so would have rejected the very hardening it needed.
_PERMITTED_USES = re.compile(r"^actions/github-script@[0-9a-f]{40}\b")

_POST_STEP = "Post the review request under a maintainer identity"

#: The secret the post step must use, in BOTH channels. `github-token` is what
#: octokit authenticates with; `env.MAINTAINER_TOKEN` is what the missing-secret
#: guard tests. If they name different secrets the guard passes while the post
#: authenticates as something else, so they are checked together, not apart.
_EXPECTED_SECRET = "REVIEW_REQUEST_TOKEN"

#: Anything that reads `secrets.` inside a step, whatever the channel. The
#: credential does not only arrive via `with.github-token`: an `env:` block is
#: the idiom THIS FILE introduces, so copying it onto the eligibility step is
#: the natural mistake, and it is the one the header explicitly forbids.
_SECRET_REF = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)")


def _load() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text())


def _jobs(doc: dict) -> dict:
    return doc.get("jobs") or {}


def _all_steps(doc: dict):
    """Every step in EVERY job, so a second job cannot hide anything."""
    for job_name, job in _jobs(doc).items():
        for step in job.get("steps") or []:
            yield job_name, step


def _eligibility_step(doc: dict):
    for _job, step in _all_steps(doc):
        if step.get("id") == "eligible":
            return step
    return None


def _post_steps(doc: dict) -> list[dict]:
    return [s for _j, s in _all_steps(doc) if s.get("name") == _POST_STEP]


def _violations(doc: dict) -> list[str]:
    """Return every invariant breach in a parsed workflow document."""
    out: list[str] = []

    # Permissions: workflow level AND every job level. A job-level block
    # OVERRIDES the workflow-level one, so checking only the top is checking
    # the value that loses.
    scopes = [("workflow", doc.get("permissions") or {})]
    for name, job in _jobs(doc).items():
        scopes.append((f"job:{name}", job.get("permissions") or {}))
    for where, perms in scopes:
        if not isinstance(perms, dict):
            out.append(f"permissions-not-a-mapping:{where}")
            continue
        for scope, level in perms.items():
            if level == "write":
                out.append(f"default-token-write-scope:{where}:{scope}")

    # A job need not have steps at all. A reusable-workflow call (`uses:` at JOB
    # level) runs code from another repository, and `secrets: inherit` hands it
    # every secret this repository holds -- strictly more privilege than the
    # checkout this file already forbids, and invisible to any walk that only
    # iterates `job["steps"]`. `container:` is the same shape.
    for name, job in _jobs(doc).items():
        if not isinstance(job, dict):
            out.append(f"job-not-a-mapping:{name}")
            continue
        if "uses" in job:
            out.append(f"job-calls-a-reusable-workflow:{name}")
        if "secrets" in job:
            out.append(f"job-forwards-secrets:{name}")
        if "container" in job:
            out.append(f"job-declares-a-container:{name}")
        if not job.get("steps"):
            out.append(f"job-without-steps:{name}")

    # The post step reads `steps.eligible.outputs.pr`, which only resolves
    # WITHIN the job that declares that step. Split across jobs the expression
    # is empty, the step never runs, and nothing is posted -- silently.
    elig_jobs = {j for j, st in _all_steps(doc) if st.get("id") == "eligible"}
    post_jobs = {j for j, st in _all_steps(doc) if st.get("name") == _POST_STEP}
    if elig_jobs and post_jobs and not (elig_jobs & post_jobs):
        out.append("post-step-in-a-different-job-than-eligibility")

    saw_post = False
    for job_name, step in _all_steps(doc):
        label = f"{job_name}/{step.get('name') or '<unnamed>'}"

        if "run" in step:
            out.append(f"run-step:{label}")

        uses = step.get("uses")
        if uses is None:
            out.append(f"step-without-uses:{label}")
        elif not _PERMITTED_USES.match(str(uses)):
            out.append(f"unpermitted-action:{label}:{uses}")

        with_ = step.get("with") or {}
        if "${{" in (with_.get("script") or ""):
            # Actions substitutes these before node parses the body, so an
            # interpolated value is code rather than data.
            out.append(f"interpolation-in-script:{label}")

        token = str(with_.get("github-token", ""))
        env_secrets = {
            m.group(1)
            for v in (step.get("env") or {}).values()
            for m in _SECRET_REF.finditer(str(v))
        }
        if step.get("name") == _POST_STEP:
            saw_post = True
            # Both channels must name the SAME secret, and it must be the
            # expected one -- a guard that tests a different secret than
            # octokit posts with is a guard that proves nothing.
            if _EXPECTED_SECRET not in env_secrets:
                out.append("post-step-env-does-not-carry-the-expected-secret")
            if token and _EXPECTED_SECRET not in token:
                out.append("post-step-token-is-not-the-expected-secret")
            if not token:
                out.append("post-step-uses-default-token")
            elif "secrets." not in token:
                out.append("post-step-token-not-from-secrets")
            elif re.search(r"secrets\.GITHUB_TOKEN", token):
                # secrets.GITHUB_TOKEN contains "secrets." and is exactly the
                # bot identity the reviewer refuses, so it needs naming.
                out.append("post-step-token-is-the-default-token")
            # The guard must BIND to the eligibility output, not merely exist.
            # `if: always()` is non-empty and would satisfy a presence check
            # while doing the opposite of gating: the token is materialised
            # into a process environment on every contributor event, including
            # declined ones, and the step runs with an empty PR number.
            guard = str(step.get("if") or "")
            if "steps.eligible.outputs.pr" not in guard:
                out.append("post-step-not-guarded-by-eligibility")
        else:
            if token:
                out.append(f"non-post-step-holds-a-token:{label}")
            if env_secrets:
                # The exact threat the docstring and the workflow header name.
                out.append(f"non-post-step-holds-a-secret-in-env:{label}")

    if not saw_post:
        out.append("post-step-missing")

    if _eligibility_step(doc) is None:
        out.append("eligibility-step-missing")
    return out


# ---------------------------------------------------------------- shipped file


def test_shipped_workflow_satisfies_every_invariant():
    assert _violations(_load()) == []


def test_every_action_is_pinned_to_a_commit_sha():
    uses = [str(s.get("uses")) for _j, s in _all_steps(_load())]
    assert uses, "fixture would be vacuous with no steps"
    for u in uses:
        assert _PERMITTED_USES.match(u), f"{u} is not a SHA-pinned permitted action"


def test_post_step_is_the_only_writer_and_posts_the_literal_request():
    post = _post_steps(_load())
    assert len(post) == 1, "exactly one step may post"
    script = post[0]["with"]["script"]
    # The request string is what the reviewer matches on, and the workflow own
    # budget greps PR comments for it. If this string drifts the budget silently
    # stops recognising its own past requests.
    assert "'@codex review'" in script
    assert "createComment" in script
    others = [s for _j, s in _all_steps(_load()) if s.get("name") != _POST_STEP]
    assert others, "fixture would be vacuous with only one step"
    for step in others:
        assert "createComment" not in ((step.get("with") or {}).get("script") or "")


def test_eligibility_step_runs_on_the_default_token():
    elig = _eligibility_step(_load())
    assert elig is not None
    assert not (elig.get("with") or {}).get("github-token"), (
        "the eligibility step applies author-influenced predicates; "
        "it must not hold the maintainer credential"
    )


def test_post_step_reads_its_inputs_from_env_not_interpolation():
    post = _post_steps(_load())[0]
    env = post.get("env") or {}
    assert {"MAINTAINER_TOKEN", "TARGET_PR", "TARGET_ISSUE"} <= set(env)
    script = post["with"]["script"]
    assert "process.env.TARGET_PR" in script
    assert "process.env.MAINTAINER_TOKEN" in script


def test_absent_secret_warns_and_expired_secret_fails_loudly():
    """Neither failure may post under the identity the reviewer refuses.

    github-token falls back to the default token so the step can still
    initialise when the secret is unset -- so the MAINTAINER_TOKEN guard has to
    return before the post. A token that exists but is expired throws instead,
    which unhandled is a red job on an outside contributor first PR with nothing
    saying why.
    """
    script = _post_steps(_load())[0]["with"]["script"]
    guard = script.index("process.env.MAINTAINER_TOKEN")
    post = script.index("createComment")
    assert guard < post, "the missing-secret guard must precede the post"
    assert "core.warning" in script[guard:post]
    assert "return;" in script[guard:post]
    assert "core.setFailed" in script[post:], "an expired token must fail loudly"


# ------------------------------------------------------- negative controls
# Each mutation is the shape the corresponding assertion exists to catch.
# Without these, a checker that silently stopped looking would still pass above.


def _mutated(fn) -> dict:
    doc = copy.deepcopy(_load())
    fn(doc)
    return doc


def _first_job(doc):
    return next(iter(_jobs(doc).values()))


def _add_run_step(doc):
    _first_job(doc)["steps"].append({"name": "build", "run": "make"})


def _add_checkout(doc):
    _first_job(doc)["steps"].append({"name": "checkout", "uses": "actions/checkout@v4"})


def _unpin_the_action(doc):
    _first_job(doc)["steps"][0]["uses"] = "actions/github-script@v9"


def _second_job_with_head_checkout(doc):
    """The escape hatch a single-job walk cannot see."""
    doc["jobs"]["sneaky"] = {
        "runs-on": "ubuntu-latest",
        "steps": [
            {"name": "checkout head", "uses": "actions/checkout@v4"},
            {
                "name": "build it",
                "run": "make",
                "env": {"T": "${{ secrets.REVIEW_REQUEST_TOKEN }}"},
            },
        ],
    }


def _job_level_permission_override(doc):
    _first_job(doc)["permissions"] = {"pull-requests": "write"}


def _interpolate_into_script(doc):
    _first_job(doc)["steps"][0].setdefault("with", {})["script"] = (
        "core.info('${{ github.event.pull_request.title }}')"
    )


def _post_as_bot(doc):
    for _j, step in _all_steps(doc):
        if step.get("name") == _POST_STEP:
            step["with"].pop("github-token", None)


def _token_not_from_secrets(doc):
    for _j, step in _all_steps(doc):
        if step.get("name") == _POST_STEP:
            step["with"]["github-token"] = "${{ github.token }}"


def _token_is_the_default_token(doc):
    for _j, step in _all_steps(doc):
        if step.get("name") == _POST_STEP:
            step["with"]["github-token"] = "${{ secrets.GITHUB_TOKEN }}"


def _drop_the_eligibility_guard(doc):
    for _j, step in _all_steps(doc):
        if step.get("name") == _POST_STEP:
            step.pop("if", None)


def _neuter_the_guard_with_always(doc):
    """Non-empty, and the opposite of a gate -- the presence-test blind spot."""
    for _j, step in _all_steps(doc):
        if step.get("name") == _POST_STEP:
            step["if"] = "always()"


def _put_the_secret_in_the_eligibility_env(doc):
    """The natural mistake: copy the post step env block onto the eligibility step."""
    _first_job(doc)["steps"][0]["env"] = {
        "MAINTAINER_TOKEN": "${{ secrets.REVIEW_REQUEST_TOKEN }}"
    }


def _repoint_the_guarded_secret(doc):
    """Guard tests one secret, octokit posts with another."""
    for _j, step in _all_steps(doc):
        if step.get("name") == _POST_STEP:
            step["env"]["MAINTAINER_TOKEN"] = "${{ secrets.SOME_OTHER_SECRET }}"


def _reusable_workflow_job(doc):
    """No steps at all -- invisible to any walk over job['steps']."""
    doc["jobs"]["sneaky"] = {
        "uses": "evil-org/evil/.github/workflows/build.yml@main",
        "secrets": "inherit",
    }


def _job_level_container(doc):
    _first_job(doc)["container"] = {"image": "evil/image:latest"}


def _split_post_into_another_job(doc):
    post = [s for s in _first_job(doc)["steps"] if s.get("name") == _POST_STEP]
    _first_job(doc)["steps"] = [
        s for s in _first_job(doc)["steps"] if s.get("name") != _POST_STEP
    ]
    doc["jobs"]["poster"] = {"runs-on": "ubuntu-latest", "steps": post}


def _widen_default_token(doc):
    doc["permissions"]["pull-requests"] = "write"


def _arm_the_eligibility_step(doc):
    _first_job(doc)["steps"][0].setdefault("with", {})["github-token"] = (
        "${{ secrets.REVIEW_REQUEST_TOKEN }}"
    )


def _drop_the_post_step(doc):
    for job in _jobs(doc).values():
        job["steps"] = [s for s in job["steps"] if s.get("name") != _POST_STEP]


@pytest.mark.parametrize(
    "mutate, expected_prefix",
    [
        (_add_run_step, "run-step:"),
        (_add_checkout, "unpermitted-action:"),
        (_unpin_the_action, "unpermitted-action:"),
        (_second_job_with_head_checkout, "run-step:"),
        (_job_level_permission_override, "default-token-write-scope:job:"),
        (_interpolate_into_script, "interpolation-in-script:"),
        (_post_as_bot, "post-step-uses-default-token"),
        (_token_not_from_secrets, "post-step-token-not-from-secrets"),
        (_token_is_the_default_token, "post-step-token-is-the-default-token"),
        (_drop_the_eligibility_guard, "post-step-not-guarded-by-eligibility"),
        (_neuter_the_guard_with_always, "post-step-not-guarded-by-eligibility"),
        (_widen_default_token, "default-token-write-scope:workflow:"),
        (_arm_the_eligibility_step, "non-post-step-holds-a-token:"),
        (_drop_the_post_step, "post-step-missing"),
        (_put_the_secret_in_the_eligibility_env, "non-post-step-holds-a-secret-in-env:"),
        (_repoint_the_guarded_secret, "post-step-env-does-not-carry-the-expected-secret"),
        (_reusable_workflow_job, "job-calls-a-reusable-workflow:"),
        (_reusable_workflow_job, "job-forwards-secrets:"),
        (_reusable_workflow_job, "job-without-steps:"),
        (_job_level_container, "job-declares-a-container:"),
        (_split_post_into_another_job, "post-step-in-a-different-job-than-eligibility"),
    ],
)
def test_checker_catches_each_breach(mutate, expected_prefix):
    found = _violations(_mutated(mutate))
    assert any(v.startswith(expected_prefix) for v in found), (
        f"expected a {expected_prefix!r} violation, got {found!r}"
    )


def test_second_job_with_a_checkout_is_caught_as_an_action_too():
    """The second-job escape hatch must trip the ALLOWLIST, not only run steps."""
    found = _violations(_mutated(_second_job_with_head_checkout))
    assert any(v.startswith("unpermitted-action:sneaky/") for v in found), found
