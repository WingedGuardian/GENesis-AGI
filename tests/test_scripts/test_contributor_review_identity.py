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

Polarity is ALLOWLIST throughout, at three levels, and every one of them was
learned from a checker that lacked it:

  ACTIONS    only a SHA-pinned `actions/github-script` may run, so an action
             nobody anticipated fails rather than passes.
  LOCATIONS  every `secrets` read in the document is a violation except in the
             two KEYS the post step may hold one in, so a channel nobody
             anticipated fails. A single-job walk would miss a SECOND job that
             checks out head code beside the same secret, so every job is
             enumerated.
  VALUES     the post step's token, its MAINTAINER_TOKEN env and its `if`
             guard are compared to exact literals, so an EXPRESSION nobody
             anticipated fails.

The third level is the newest and the most expensive to have learned. Each
earlier form reasoned about the secret NAME inside an expression, and an
adversarial pass defeated every one of them with an expression carrying the
right name and resolving to something else -- including one that reinstates
this file's original defect in a single line. A name is not what a slot
resolves to; only the literal is.

Every invariant below is exercised in BOTH directions -- the shipped file must
be clean, and a deliberately broken copy must be caught. An assertion group
that only ever sees a clean fixture passes just as well when it checks nothing.
And where an assertion could be satisfied by a string that does not actually
BIND the behaviour, it compares the whole value: `if: always()` fails a
presence test, but `if: always() || steps.eligible.outputs.pr != ''` passes
one while gating nothing, and only equality rejects both.

What this file does NOT establish is in `_secret_locations` -- a lexical scan
cannot see runtime dataflow, which is why the structural rules about step ids
and step order are there instead.
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

#: The three expressions the post step is allowed to carry, compared as EXACT
#: LITERALS rather than parsed.
#:
#: Extracting a secret NAME from an expression and reasoning about it is the
#: wrong primitive for a credential gate, because the name is only one of the
#: things that decides what the slot resolves to. Three separate defeats of the
#: name-based form were MEASURED on this file, and a literal comparison closes
#: all three at once:
#:
#:   ${{ secrets['ATTACKER_PAT'] || secrets.REVIEW_REQUEST_TOKEN }}
#:       index syntax is documented first-class context access, so a
#:       dot-only pattern does not see the operand that actually wins.
#:   env.MAINTAINER_TOKEN: ${{ secrets.REVIEW_REQUEST_TOKEN || github.token }}
#:       the only name present is the expected one, and yet with the secret
#:       unset this resolves to the DEFAULT TOKEN, so the missing-secret guard
#:       sees a value, returns false, and the request posts as
#:       github-actions[bot] -- silently recreating the defect this whole file
#:       exists to prevent. A fallback is harmless in the token slot, where the
#:       guard returns first, and guard-defeating in the env slot.
#:   if: always() || steps.eligible.outputs.pr != ''
#:       contains the gating token and gates nothing.
#:
#: An allowlist over VALUES has no expression surface left to attack. The cost
#: is that a deliberate change to any of these three strings fails CI until the
#: constant is updated, which on a credential expression is the review
#: checkpoint we want rather than a maintenance burden.
_PERMITTED_TOKEN = "${{ secrets.REVIEW_REQUEST_TOKEN || github.token }}"
_PERMITTED_MAINTAINER_ENV = "${{ secrets.REVIEW_REQUEST_TOKEN }}"
_PERMITTED_GUARD = "steps.eligible.outputs.pr != ''"

#: The only step that may carry an `id`. A later step can read an earlier one's
#: output, which is runtime dataflow and therefore invisible to any lexical
#: scan: the post step could emit its own token with `core.setOutput` and a
#: downstream step could consume it with no `secrets` reference anywhere in the
#: file. Forbidding the downstream step removes the channel instead of trying
#: to detect it.
_PERMITTED_STEP_IDS = {"eligible"}

#: Any read of the `secrets` context, in EITHER documented spelling. GitHub
#: documents both `secrets.NAME` and `secrets['NAME']`; a pattern matching only
#: the first is a denylist one spelling wide, which is how the maintainer
#: credential reached the eligibility step with the suite fully green. A
#: bracket read whose name cannot be resolved statically is reported as
#: `<unresolved>` -- named, not skipped, because failing closed on something
#: unreadable is the only safe direction for a credential.
_SECRET_REF = re.compile(r"secrets\s*(?:\.\s*([A-Za-z_][A-Za-z0-9_]*)|\[)")


def _load() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text())


def _jobs(doc: dict) -> dict:
    return doc.get("jobs") or {}


def _all_steps(doc: dict):
    """Every step in EVERY job, so a second job cannot hide anything."""
    for job_name, job in _jobs(doc).items():
        for step in job.get("steps") or []:
            yield job_name, step


def _secret_locations(node, path: str = "") -> list[str]:
    """`<dotted path>:<secret name>` for every `secrets.X` reference in a tree.

    Channel-agnostic on purpose. Enumerating the places a credential can arrive
    is a DENYLIST, and successive review rounds each supplied the next spelling
    the previous one had missed: `with.github-token`, then a step `env:`, then
    a workflow- or job-level `env:` that every step INHERITS, then any other
    action input. So nothing is enumerated: the whole document is walked and
    the two KEYS the credential may occupy are removed before the walk rather
    than recognised during it.

    STATE THE GUARANTEE EXACTLY, because the previous version of this docstring
    claimed a channel nobody had thought of would "fail by construction" and an
    audit falsified that in three lines of YAML. What this walk gives is: every
    LOCATION in the document, for every `secrets` read it can DETECT LEXICALLY.
    Two limits follow directly, and neither is fixable by widening a pattern:

    - A value that arrives by runtime DATAFLOW carries no `secrets` text at
      all. The post step could publish its own token with `core.setOutput` and
      a later step could read `${{ steps.<id>.outputs.x }}`. That is why
      `_PERMITTED_STEP_IDS` and the last-step rule exist -- the channel is
      removed rather than detected.
    - Only VALUES are walked, never mapping keys. No Actions shape is known
      where a credential in key position is exploitable, but the walk does not
      cover it, and "the whole document" would be the wrong thing to claim.
    """
    if isinstance(node, dict):
        return [
            loc
            for key, value in node.items()
            for loc in _secret_locations(value, f"{path}.{key}" if path else str(key))
        ]
    if isinstance(node, list):
        return [
            loc
            for index, value in enumerate(node)
            for loc in _secret_locations(value, f"{path}[{index}]")
        ]
    return [f"{path}:{m.group(1) or '<unresolved>'}" for m in _SECRET_REF.finditer(str(node))]


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

    # The post step is identified BY NAME, so it has to be unique: a second
    # step wearing the same name would have its credential slots blessed by the
    # walk below and then satisfy every post-step check.
    posts = _post_steps(doc)
    if len(posts) != 1:
        out.append(f"post-step-is-not-unique:{len(posts)}")

    # Nothing may follow the post step, and no other step may carry an `id`.
    # See `_PERMITTED_STEP_IDS`: a step output is runtime dataflow and a
    # lexical walk cannot see it, so the downstream step is forbidden rather
    # than inspected.
    for name, job in _jobs(doc).items():
        steps = job.get("steps") if isinstance(job, dict) else None
        if steps is not None and not isinstance(steps, list):
            out.append(f"steps-not-a-list:{name}")
            continue
        for index, step in enumerate(steps or []):
            if not isinstance(step, dict):
                out.append(f"step-not-a-mapping:{name}[{index}]")
                continue
            step_id = step.get("id")
            if step_id is not None and step_id not in _PERMITTED_STEP_IDS:
                out.append(f"unexpected-step-id:{name}:{step_id}")
            if step.get("name") == _POST_STEP and index != len(steps) - 1:
                out.append(f"post-step-is-not-the-last-step:{name}")

    # Credential LOCATION. Blank the two KEYS the post step may hold the
    # credential in, then any `secrets` read left ANYWHERE in the file is a
    # violation -- including one in a workflow- or job-level scope that names
    # no step but is inherited by all of them. Only those two keys are blanked,
    # never the whole `env` or `with` block, so a SECOND credential smuggled in
    # beside them is still reported.
    #
    # Blanking REPLACES the mapping rather than mutating it. `yaml.safe_load`
    # materialises a YAML anchor and its aliases as ONE object and `deepcopy`
    # preserves that sharing, so popping a key would blank it for every step
    # aliasing the same mapping and hide a real violation elsewhere.
    probe = copy.deepcopy(doc)
    probe_posts = _post_steps(probe)
    if len(probe_posts) == 1:
        post = probe_posts[0]
        for block, key in (("with", "github-token"), ("env", "MAINTAINER_TOKEN")):
            held = post.get(block)
            if isinstance(held, dict):
                post[block] = {k: v for k, v in held.items() if k != key}
    for loc in _secret_locations(probe):
        out.append(f"secret-outside-the-post-step:{loc}")

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

        with_raw = step.get("with")
        if with_raw is not None and not isinstance(with_raw, dict):
            out.append(f"with-not-a-mapping:{label}")
            with_ = {}
        else:
            with_ = with_raw or {}
        if "${{" in (with_.get("script") or ""):
            # Actions substitutes these before node parses the body, so an
            # interpolated value is code rather than data.
            out.append(f"interpolation-in-script:{label}")

        token = str(with_.get("github-token", ""))
        if step.get("name") == _POST_STEP:
            saw_post = True
            # Each of the three slots is compared to its permitted LITERAL.
            # See the constants: every previous form of these checks reasoned
            # about the secret NAME inside the expression, and each one was
            # defeated by an expression carrying the right name and resolving
            # to something else.
            if not token:
                out.append("post-step-uses-default-token")
            elif token.strip() != _PERMITTED_TOKEN:
                out.append(f"post-step-token-is-not-the-permitted-expression:{token}")
            env = step.get("env")
            env = env if isinstance(env, dict) else {}
            if str(env.get("MAINTAINER_TOKEN", "")).strip() != _PERMITTED_MAINTAINER_ENV:
                out.append("post-step-maintainer-env-is-not-the-permitted-expression")
            if str(step.get("if") or "").strip() != _PERMITTED_GUARD:
                out.append("post-step-guard-is-not-the-permitted-expression")
        elif token:
            # A secret on a non-post step is caught by location above, whatever
            # channel carries it. This catches the remaining case: a credential
            # that is not a secret reference at all, such as `${{ github.token
            # }}`, handed to a step that applies author-influenced predicates.
            out.append(f"non-post-step-holds-a-token:{label}")

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
    _first_job(doc)["steps"][0]["env"] = {"MAINTAINER_TOKEN": "${{ secrets.REVIEW_REQUEST_TOKEN }}"}


def _workflow_level_env_secret(doc):
    """Inherited by every step in every job, and named by no step at all."""
    doc["env"] = {"MAINTAINER_TOKEN": "${{ secrets.REVIEW_REQUEST_TOKEN }}"}


def _job_level_env_secret(doc):
    """Same inheritance, one scope down."""
    _first_job(doc)["env"] = {"MAINTAINER_TOKEN": "${{ secrets.REVIEW_REQUEST_TOKEN }}"}


def _secret_in_another_action_input(doc):
    """Not `github-token`, so a check that reads only that input is blind."""
    _first_job(doc)["steps"][0].setdefault("with", {})["result-encoding"] = (
        "${{ secrets.REVIEW_REQUEST_TOKEN }}"
    )


def _token_prefers_another_secret(doc):
    """Actions resolves `a || b` to `a`, so this posts as OTHER.

    The expected name is still present as a substring, which is exactly what
    makes a containment check pass while authentication happens as something
    else entirely.
    """
    for _j, step in _all_steps(doc):
        if step.get("name") == _POST_STEP:
            step["with"]["github-token"] = "${{ secrets.OTHER || secrets.REVIEW_REQUEST_TOKEN }}"


def _bracket_secret_on_the_eligibility_step(doc):
    """Index syntax -- documented, first-class, and invisible to a dot pattern."""
    _first_job(doc)["steps"][0]["env"] = {
        "MAINTAINER_TOKEN": "${{ secrets['REVIEW_REQUEST_TOKEN'] }}"
    }


def _bracket_secret_in_workflow_env(doc):
    doc["env"] = {"MAINTAINER_TOKEN": "${{ secrets['REVIEW_REQUEST_TOKEN'] }}"}


def _bracket_secret_by_computed_name(doc):
    """A name no static reader can resolve. Must fail CLOSED, not be skipped."""
    _first_job(doc)["steps"][0]["env"] = {
        "MAINTAINER_TOKEN": "${{ secrets[format('REVIEW_{0}', 'REQUEST_TOKEN')] }}"
    }


def _token_prefers_a_bracket_secret(doc):
    """The winning operand is bracket-spelled, so a name check never sees it."""
    for _j, step in _all_steps(doc):
        if step.get("name") == _POST_STEP:
            step["with"]["github-token"] = (
                "${{ secrets['ATTACKER_PAT'] || secrets.REVIEW_REQUEST_TOKEN }}"
            )


def _maintainer_env_falls_back_to_the_default_token(doc):
    """The only name present is the right one, and it still posts as the bot.

    With the secret unset this resolves to the default token rather than the
    empty string, so the script missing-secret guard sees a value and returns
    false -- and the request goes out under the identity the reviewer refuses.
    """
    for _j, step in _all_steps(doc):
        if step.get("name") == _POST_STEP:
            step["env"]["MAINTAINER_TOKEN"] = "${{ secrets.REVIEW_REQUEST_TOKEN || github.token }}"


def _extra_secret_beside_the_permitted_one(doc):
    """Blanking a whole `env` block would bless this; blanking one key does not."""
    for _j, step in _all_steps(doc):
        if step.get("name") == _POST_STEP:
            step["env"]["SECOND"] = "${{ secrets.ANOTHER_PAT }}"


def _shared_with_mapping_via_anchor(doc):
    """One mapping aliased onto two steps, as `yaml.safe_load` materialises it.

    Mutating the post step credential out of a SHARED mapping would blank it
    for the other step too, and the walk would report nothing.
    """
    shared = None
    for _j, step in _all_steps(doc):
        if step.get("name") == _POST_STEP:
            shared = step["with"]
    _first_job(doc)["steps"][0]["with"] = shared


def _launder_the_token_through_a_step_output(doc):
    """No `secrets` reference anywhere on the receiving step."""
    job = _first_job(doc)
    for step in job["steps"]:
        if step.get("name") == _POST_STEP:
            step["id"] = "poster"
    job["steps"].append(
        {
            "name": "use it",
            "uses": job["steps"][0]["uses"],
            "env": {
                "T": "${{ steps.poster.outputs.t }}",
                "BODY": "${{ github.event.pull_request.body }}",
            },
            "with": {"script": "core.info('x')"},
        }
    )


def _duplicate_post_step_name(doc):
    """A second step wearing the post step name gets its slots blessed."""
    job = _first_job(doc)
    twin = copy.deepcopy(job["steps"][-1])
    twin["with"] = dict(twin["with"])
    twin["with"]["script"] = "core.info(process.env.BODY)"
    twin["env"] = dict(twin["env"])
    twin["env"]["BODY"] = "${{ github.event.pull_request.body }}"
    job["steps"].insert(0, twin)


def _neuter_the_guard_with_always_or(doc):
    """Contains the gating token, short-circuits past it, gates nothing."""
    for _j, step in _all_steps(doc):
        if step.get("name") == _POST_STEP:
            step["if"] = "always() || steps.eligible.outputs.pr != ''"


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
    _first_job(doc)["steps"] = [s for s in _first_job(doc)["steps"] if s.get("name") != _POST_STEP]
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
        (_token_not_from_secrets, "post-step-token-is-not-the-permitted-expression"),
        (_token_is_the_default_token, "post-step-token-is-not-the-permitted-expression"),
        (_drop_the_eligibility_guard, "post-step-guard-is-not-the-permitted-expression"),
        (
            _neuter_the_guard_with_always,
            "post-step-guard-is-not-the-permitted-expression",
        ),
        (
            _neuter_the_guard_with_always_or,
            "post-step-guard-is-not-the-permitted-expression",
        ),
        (_widen_default_token, "default-token-write-scope:workflow:"),
        (_arm_the_eligibility_step, "non-post-step-holds-a-token:"),
        (_drop_the_post_step, "post-step-missing"),
        (_put_the_secret_in_the_eligibility_env, "secret-outside-the-post-step:"),
        (_workflow_level_env_secret, "secret-outside-the-post-step:env."),
        (_job_level_env_secret, "secret-outside-the-post-step:jobs."),
        (_secret_in_another_action_input, "secret-outside-the-post-step:"),
        (
            _token_prefers_another_secret,
            "post-step-token-is-not-the-permitted-expression",
        ),
        (
            _token_prefers_a_bracket_secret,
            "post-step-token-is-not-the-permitted-expression",
        ),
        (
            _repoint_the_guarded_secret,
            "post-step-maintainer-env-is-not-the-permitted-expression",
        ),
        (
            _maintainer_env_falls_back_to_the_default_token,
            "post-step-maintainer-env-is-not-the-permitted-expression",
        ),
        (_bracket_secret_on_the_eligibility_step, "secret-outside-the-post-step:"),
        (_bracket_secret_in_workflow_env, "secret-outside-the-post-step:env."),
        (_bracket_secret_by_computed_name, "secret-outside-the-post-step:"),
        (_extra_secret_beside_the_permitted_one, "secret-outside-the-post-step:"),
        (_shared_with_mapping_via_anchor, "secret-outside-the-post-step:"),
        (_duplicate_post_step_name, "post-step-is-not-unique:"),
        (_launder_the_token_through_a_step_output, "unexpected-step-id:"),
        (_launder_the_token_through_a_step_output, "post-step-is-not-the-last-step:"),
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
