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

Polarity is ALLOWLIST throughout, at five levels, and every one of them was
learned from a checker that lacked it:

  ACTIONS    only a SHA-pinned `actions/github-script` may run, anchored at
             BOTH ends, so an action nobody anticipated fails rather than
             passes -- and so does a valid git ref component bolted onto the
             sha, which resolves as a mutable tag.
  KEYS       workflow-level and job-level keys are allowlisted. The denylist
             form shipped three times and missed a different code-running key
             each round: `uses`, then `container`, then `services`, which
             starts an arbitrary image with host mounts before any step here
             runs. `strategy`, `defaults` and `outputs` were never considered.
  LOCATIONS  every mention of the `secrets` CONTEXT is a violation except in
             the two KEYS the post step may hold one in, so a channel nobody
             anticipated fails. Every job is enumerated, because a single-job
             walk misses a SECOND job holding head code beside the same secret.
  VALUES     the post step's token, its MAINTAINER_TOKEN env and its `if`
             guard are compared to exact literals, so an EXPRESSION nobody
             anticipated fails.
  UNIQUENESS the post step and the eligibility producer are each required to
             be the only one of their kind, because both are identified by a
             string and the post step consumes the producer's output.

Four rounds went into that list, and every round's finding was the SAME mistake
in a new place: a set was enumerated instead of bounded. The reviewer supplied
`secrets['NAME']` after the dot form, `toJSON(secrets)` after both, and
`services` after `uses` and `container`. So nothing here enumerates the ways a
thing can be spelled any more; the checks name what is PERMITTED and everything
else fails by construction. A name is also not what a slot resolves to --
`${{ secrets.EXPECTED || github.token }}` names only the expected secret and
resolves, unset, to the default token -- which is why the value level compares
literals rather than reasoning about names at all.

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
import json
import os
import re
import shutil
import subprocess
import tempfile
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
#: Anchored at BOTH ends. A trailing `\b` let `actions/github-script@<40-hex>-x`
#: and `actions/github-script@<40-hex>/x` through, and both are valid git ref
#: components that resolve as a mutable tag rather than as the pinned commit.
#: The `# v9` that follows this in the YAML is a comment and is not part of the
#: parsed value, so anchoring the end is safe -- verified against the parse.
#: `\Z` and not `$`, because Python's `$` also matches before one trailing
#: newline, and a `uses:` written as a YAML literal block scalar parses with one.
_PERMITTED_USES = re.compile(r"^actions/github-script@[0-9a-f]{40}\Z")

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

#: ANY mention of the `secrets` context inside an Actions expression -- not any
#: particular way of reading one.
#:
#: This is the fourth polarity fix on this one check, and the pattern in the
#: three before it was always the same: enumerate the ways a credential can be
#: named, ship, and have the next review supply the way that was missed.
#: `secrets.NAME`, then `secrets['NAME']`, then `${{ toJSON(secrets) }}` --
#: which serialises the WHOLE context, including this workflow's PAT, and names
#: no individual secret at all, so every name-extracting pattern is blind to it.
#:
#: There is no list here any more, and there is no parsing either. The RAW value
#: is searched for the word `secrets`; `_secret_locations` reports WHERE, and the
#: two blessed keys are removed before the walk.
#:
#: An earlier version of THIS fix carved `${{ ... }}` bodies out first and
#: searched inside them. That was a fail-open, and an adversarial run produced
#: it: the non-greedy carve stops at the first `}}`, including one inside the
#: expression's own string literal, so
#:     ${{ fromJSON('{"a":{"b":1}}').c || secrets.REVIEW_REQUEST_TOKEN }}
#: dropped everything after the nested brace and reported CLEAN -- a case the
#: cruder pattern it replaced had caught. Nested JSON in `fromJSON` is where a
#: `}}` appears inside a literal, so the shape is ordinary, not exotic.
#:
#: The lesson, for the fourth time in this file: tokenizing input you do not
#: have to tokenize buys a way to be wrong. A false POSITIVE here costs a
#: reworded string; a false negative costs the credential. So the check does not
#: care whether the mention is in an expression, a comment inside a script body,
#: or text nobody meant as code -- it fires, and the shipped-file test is the
#: proof that the real workflow stays clean under that breadth.
#: CASE-INSENSITIVE, and that is not a nicety. GitHub resolves context names with
#: `StringComparer.OrdinalIgnoreCase` (READ: actions/runner
#: src/Sdk/DTExpressions2/Expressions2/ExpressionParser.cs, the
#: `ExtensionNamedValues` dictionary), so `${{ SECRETS.X }}` and
#: `${{ ToJson(SECRETS) }}` both resolve. A case-SENSITIVE pattern here restored
#: this file's original defect with a one-character edit, suite fully green.
#:
#: The general rule, because case is just another enumerated spelling: at this
#: gate, every comparison must err toward FLAGGING. Those that do are left
#: case-sensitive on purpose -- an oddly-cased `uses`, post-step name, step id,
#: schema key or permitted literal all fail, which is the safe direction. The two
#: that erred toward PASSING are this one and the write-scope check below.
_SECRETS_CONTEXT = re.compile(r"\bsecrets\b", re.I)
#: Only to LABEL a reference once `_SECRETS_CONTEXT` has already decided it is
#: one. Never used to decide whether something IS a reference. Both dereference
#: spellings are read, so a bracket form is not mislabelled as a whole-context
#: export -- a wrong label sends the next reader hunting for a `toJSON` that is
#: not there, and a diagnostic that lies is its own small defect.
_SECRET_NAME = re.compile(
    r"""\bsecrets\s*(?:\.\s*([A-Za-z_][A-Za-z0-9_]*)|\[\s*['"]([^'"]+)['"]\s*\]|(\[))""",
    re.I,
)

#: Job keys are an ALLOWLIST, because the denylist form shipped three times and
#: missed a different code-running key each time: `uses` (a reusable workflow),
#: `container`, and `services` -- which starts an arbitrary Docker image, with
#: docker options and host volume mounts, BEFORE any step in this file runs.
#: `strategy`, `defaults`, `environment` and `outputs` were never considered at
#: all. Anything not named here fails, so the next key GitHub adds fails too.
_PERMITTED_JOB_KEYS = frozenset(
    {"name", "runs-on", "if", "permissions", "steps", "timeout-minutes", "concurrency"}
)

#: Same reasoning one level up. The workflow level can also carry `env`,
#: `defaults` and `run-name`, and an inherited scope is exactly how a credential
#: reached every step without naming one.
#:
#: Read with `_normalised_key`, not with a `True` member. Under YAML 1.1, which
#: PyYAML implements, a bare `on` is a BOOLEAN, so the trigger block parses as the
#: key `True` -- and `doc["on"]` is a KeyError, silently, because the key is not
#: where it was looked for. Putting `True` in the allowlist instead was wrong in
#: BOTH directions: `1 == True` in Python, so a workflow key `1:` was admitted,
#: while the QUOTED `"on":` spelling -- the one yamllint's `truthy` rule pushes
#: authors toward -- was rejected as unpermitted. A guard that fires on a
#: lint-recommended edit is a guard that gets silenced.
_PERMITTED_WORKFLOW_KEYS = frozenset({"name", "on", "permissions", "concurrency", "jobs"})

#: The post step's ENTIRE environment, keys and values, as exact literals.
#:
#: Allowlisting only the credential left the rest of the block free, and an `env`
#: on this step is a process-startup channel, not just a data channel:
#: `NODE_OPTIONS: ${{ github.event.pull_request.body }}` lets an outside
#: contributor put `--import=data:text/javascript,...` in their PR body and have
#: node execute it BEFORE the pinned action runs, with MAINTAINER_TOKEN already in
#: the environment. Reproduced by the reviewer on Node 24.15.0. No amount of
#: checking the script body finds that, because the payload never enters the
#: script.
#:
#: So the environment is pinned whole. A new variable here is a deliberate edit
#: with a reviewer attached, which for the one step that holds the credential is
#: the correct amount of friction.
_PERMITTED_POST_ENV = {
    "MAINTAINER_TOKEN": "${{ secrets.REVIEW_REQUEST_TOKEN }}",
    "TARGET_PR": "${{ steps.eligible.outputs.pr }}",
    "TARGET_ISSUE": "${{ steps.eligible.outputs.issue }}",
}

#: Step keys are an allowlist for the same reason job keys are. `continue-on-error`
#: is the one that made it necessary: on the credential-bearing step it tells
#: GitHub to tolerate a non-zero outcome, so both `core.setFailed` paths -- an
#: unreadable comment list, and a revoked or under-scoped PAT -- stop turning the
#: job red. That deletes the signal this change relies on to tell a broken
#: credential apart from a successful request, and it deletes it silently.
_PERMITTED_STEP_KEYS = frozenset({"name", "id", "if", "uses", "with", "env"})

#: The ONE runner this workflow may use, as a literal. `runs-on` passed the key
#: allowlist while its VALUE stayed unbounded, and the value decides which machine
#: decrypts MAINTAINER_TOKEN: `runs-on: [self-hosted, attacker-pool]` relocates
#: the maintainer PAT onto a host outside GitHub's control with the checker
#: reporting clean. Bounding the key and leaving the value free is half a bound.
_PERMITTED_RUNS_ON = "ubuntu-latest"


def _normalised_key(key):
    """YAML 1.1 turns a bare `on` into `True`; both spellings mean the trigger."""
    return "on" if key is True else key


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
    raw = str(node)
    if not _SECRETS_CONTEXT.search(raw):
        return []
    # One violation per VALUE, not per dereference. The labels below are
    # diagnostic only -- every one of them is a violation when it sits outside
    # the two blessed keys:
    #   a NAME            a readable dot or quoted-bracket dereference
    #   <unresolved>      a bracket index no static reader can resolve
    #   <whole-context>   the word appears but nothing is dereferenced, i.e.
    #                     `toJSON(secrets)` -- every secret the workflow can
    #                     see, naming none of them
    named = _SECRET_NAME.search(raw)
    if named is None:
        label = "<whole-context>"
    else:
        label = named.group(1) or named.group(2) or "<unresolved>"
    return [f"{path}:{label}"]


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
            # Case-folded for the same reason as `_SECRETS_CONTEXT`: this is the
            # other comparison at this gate whose case-sensitivity erred toward
            # PASSING, so `contents: WRITE` read as read-only.
            if str(level).strip().lower() == "write":
                out.append(f"default-token-write-scope:{where}:{scope}")

    # A job need not have steps at all. A reusable-workflow call (`uses:` at JOB
    # level) runs code from another repository, and `secrets: inherit` hands it
    # every secret this repository holds -- strictly more privilege than the
    # checkout this file already forbids, and invisible to any walk that only
    # iterates `job["steps"]`. `container:` is the same shape.
    for key in doc:
        if _normalised_key(key) not in _PERMITTED_WORKFLOW_KEYS:
            out.append(f"unpermitted-workflow-key:{key}")

    for name, job in _jobs(doc).items():
        if not isinstance(job, dict):
            out.append(f"job-not-a-mapping:{name}")
            continue
        # The allowlist is the MECHANISM. The three named checks below it stay
        # only because they say WHICH escape hatch was opened, which a generic
        # "unpermitted key" never will; they are diagnostics, not the bound.
        for key in job:
            if _normalised_key(key) not in _PERMITTED_JOB_KEYS:
                out.append(f"unpermitted-job-key:{name}:{key}")
        # The key is allowlisted; so is its VALUE, because the value is what
        # decides which machine decrypts the credential.
        if job.get("runs-on") != _PERMITTED_RUNS_ON:
            out.append(f"unpermitted-runner:{name}:{job.get('runs-on')!r}")
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

    # The eligibility PRODUCER must be unique too, and for the same reason the
    # post step must be. `steps.eligible.outputs.pr` resolves against whichever
    # step carries that id IN THE POST STEP'S OWN JOB, so a second job holding
    # its own `id: eligible` beside a second pinned github-script satisfies the
    # same-job check while the post step consumes an output the real predicate
    # never produced.
    producers = [st for _j, st in _all_steps(doc) if st.get("id") == "eligible"]
    if len(producers) != 1:
        out.append(f"eligibility-step-is-not-unique:{len(producers)}")

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
        for step_key in step:
            if step_key not in _PERMITTED_STEP_KEYS:
                out.append(f"unpermitted-step-key:{label}:{step_key}")

        if step.get("name") == _POST_STEP:
            saw_post = True
            # The WHOLE environment, keys and values. See _PERMITTED_POST_ENV:
            # an env var on this step can reach node's startup, not just the
            # script's data.
            if (step.get("env") or {}) != _PERMITTED_POST_ENV:
                out.append(
                    f"post-step-env-is-not-the-permitted-set:{sorted(step.get('env') or {})}"
                )
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
        else:
            if token:
                # A secret on a non-post step is caught by location above,
                # whatever channel carries it. This catches the remaining case: a
                # credential that is not a secret reference at all, such as
                # `${{ github.token }}`, handed to a step that applies
                # author-influenced predicates.
                out.append(f"non-post-step-holds-a-token:{label}")
            if step.get("env"):
                # No step but the poster needs an environment, and `env` is a
                # process-startup channel: NODE_OPTIONS reaches node before the
                # action does. Nothing here needs one, so nothing here may have
                # one.
                out.append(f"non-post-step-has-an-env:{label}")

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


def _node_options_from_the_pr_body(doc):
    """Author-controlled process startup, before the pinned action even loads.

    `--import=data:text/javascript,...` in a PR body executes in the node process
    that runs github-script, with MAINTAINER_TOKEN already present. The script
    body is never involved, so scanning it finds nothing.
    """
    for _j, step in _all_steps(doc):
        if step.get("name") == _POST_STEP:
            step["env"]["NODE_OPTIONS"] = "${{ github.event.pull_request.body }}"


def _continue_on_error_on_the_post_step(doc):
    """Tells GitHub to tolerate the failure this change relies on to be loud."""
    for _j, step in _all_steps(doc):
        if step.get("name") == _POST_STEP:
            step["continue-on-error"] = True


def _env_on_the_eligibility_step(doc):
    _first_job(doc)["steps"][0]["env"] = {"NODE_OPTIONS": "--import=data:x"}


def _uppercase_secrets_context(doc):
    """Actions resolves context names case-insensitively; a one-char bypass."""
    _first_job(doc)["steps"][0]["env"] = {"T": "${{ SECRETS.REVIEW_REQUEST_TOKEN }}"}


def _uppercase_whole_context_export(doc):
    _first_job(doc)["steps"][0]["env"] = {"T": "${{ ToJson(SECRETS) }}"}


def _uppercase_write_permission(doc):
    doc["permissions"]["pull-requests"] = "WRITE"


def _self_hosted_runner(doc):
    """Relocates the machine that decrypts the PAT."""
    _first_job(doc)["runs-on"] = ["self-hosted", "attacker-pool"]


def _numeric_workflow_key(doc):
    """A non-string key must not be admitted.

    NOT `doc[1]`, which cannot express the case: `1 == True` and dict lookup is
    by equality, so assigning key 1 REPLACES the `True` that `on:` parsed to
    rather than adding a key. The two cannot coexist after parsing, which is
    also why an allowlist containing the bare `True` admitted a numeric key --
    `_normalised_key` maps only the `True` singleton, by identity.
    """
    doc[2] = "anything"


def _quoted_on_key(doc):
    """The spelling yamllint's truthy rule pushes toward must NOT false-positive."""
    doc["on"] = doc.pop(True)


def _secret_after_a_nested_brace(doc):
    """A `}}` inside a string literal used to hide everything after it.

    Nested JSON in `fromJSON` is where a literal `}}` naturally appears, so this
    is an ordinary expression rather than a contrived one -- and an earlier
    version of THIS check carved `${{ ... }}` bodies out with a non-greedy match
    and reported clean on it.
    """
    _first_job(doc)["steps"][0]["env"] = {
        "X": '${{ fromJSON(\'{"a":{"b":1}}\').c || secrets.REVIEW_REQUEST_TOKEN }}'
    }


def _secret_after_an_injected_double_brace(doc):
    _first_job(doc)["steps"][0]["env"] = {
        "X": "${{ format('{0}{1}', '}}', secrets.REVIEW_REQUEST_TOKEN) }}"
    }


def _whole_context_secret_export(doc):
    """`toJSON(secrets)` serialises EVERY secret and names none of them."""
    _first_job(doc)["steps"][0]["env"] = {"ALL": "${{ toJSON(secrets) }}"}


def _whole_context_secret_export_at_workflow_level(doc):
    doc["env"] = {"ALL": "${{ toJSON(secrets) }}"}


def _job_service_container(doc):
    """A service starts an arbitrary image, with host mounts, before any step."""
    _first_job(doc)["services"] = {"evil": {"image": "evil/image:latest", "options": "-v /:/host"}}


def _job_matrix_strategy(doc):
    """Not a code-running key in itself -- the point is that it is UNLISTED."""
    _first_job(doc)["strategy"] = {"matrix": {"n": [1, 2]}}


def _mutable_suffix_on_the_pinned_sha(doc):
    """A valid git ref component after the sha, so it resolves as a TAG."""
    step = _first_job(doc)["steps"][0]
    step["uses"] = f"{step['uses']}-mutable"


def _second_eligibility_producer_in_another_job(doc):
    """The post step then consumes an output the real predicate never produced."""
    original = _first_job(doc)["steps"]
    post = [s for s in original if s.get("name") == _POST_STEP]
    _first_job(doc)["steps"] = [s for s in original if s.get("name") != _POST_STEP]
    fake = copy.deepcopy(original[0])
    fake["with"] = {"script": "core.setOutput('pr', '1'); core.setOutput('issue', '1')"}
    doc["jobs"]["shadow"] = {"runs-on": "ubuntu-latest", "steps": [fake, *post]}


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
        (_whole_context_secret_export, "secret-outside-the-post-step:"),
        (_secret_after_a_nested_brace, "secret-outside-the-post-step:"),
        (_secret_after_an_injected_double_brace, "secret-outside-the-post-step:"),
        (_node_options_from_the_pr_body, "post-step-env-is-not-the-permitted-set:"),
        (_continue_on_error_on_the_post_step, "unpermitted-step-key:"),
        (_env_on_the_eligibility_step, "non-post-step-has-an-env:"),
        (_uppercase_secrets_context, "secret-outside-the-post-step:"),
        (_uppercase_whole_context_export, "secret-outside-the-post-step:"),
        (_uppercase_write_permission, "default-token-write-scope:"),
        (_self_hosted_runner, "unpermitted-runner:"),
        (_numeric_workflow_key, "unpermitted-workflow-key:"),
        (
            _whole_context_secret_export_at_workflow_level,
            "unpermitted-workflow-key:env",
        ),
        (_job_service_container, "unpermitted-job-key:"),
        (_job_matrix_strategy, "unpermitted-job-key:"),
        (_mutable_suffix_on_the_pinned_sha, "unpermitted-action:"),
        (
            _second_eligibility_producer_in_another_job,
            "eligibility-step-is-not-unique:",
        ),
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


def test_the_quoted_on_key_is_not_a_false_positive():
    """A guard that fires on a lint-recommended edit is a guard that gets silenced.

    YAML 1.1 parses a bare `on:` as the boolean `True`, and yamllint's `truthy`
    rule pushes authors to quote it. Both spellings mean the trigger block, so
    both must pass -- an earlier version of the key allowlist accepted only the
    boolean and rejected the quoted form.
    """
    assert _violations(_mutated(_quoted_on_key)) == []


def test_the_secret_walk_does_not_fire_on_an_ordinary_expression():
    """The breadth has a limit, and an absence-assertion group needs its sibling.

    The walk searches the RAW value for the word `secrets` rather than parsing
    expressions, which is deliberate breadth. That only means something if it
    still distinguishes: an expression with no secret in it must not trip, or
    every "caught" above would be vacuous.
    """
    doc = _mutated(
        lambda d: _first_job(d)["steps"][0].__setitem__(
            "env", {"N": "${{ github.event.pull_request.number }}"}
        )
    )
    assert not [v for v in _violations(doc) if v.startswith("secret-outside")]


def test_second_job_with_a_checkout_is_caught_as_an_action_too():
    """The second-job escape hatch must trip the ALLOWLIST, not only run steps."""
    found = _violations(_mutated(_second_job_with_head_checkout))
    assert any(v.startswith("unpermitted-action:sneaky/") for v in found), found


# ------------------------------------------------- the spent-budget predicate
# The budget is a courtesy bound, not a security control -- assignment is what
# authorises. But it is the thing that stops an ordinary repeat request, and
# both halves below were defects found by review rather than theory.


def _eligibility_script() -> str:
    return _eligibility_step(_load())["with"]["script"]


def _post_script() -> str:
    return _post_steps(_load())[0]["with"]["script"]


#: The shared predicate, from its first declaration to the end of the arrow
#: function. Extraction is fail-loud by construction: a renamed parameter breaks
#: `\(body\)`, a moved declaration breaks the name check below, and a braced
#: body truncates to invalid JavaScript that node refuses to parse.
_PREDICATE_RE = re.compile(
    r"const REQUEST = [\s\S]*?const alreadyRequested = \(body\) =>[\s\S]*?;\n"
)


def _node_or_skip() -> str:
    """Node, or a skip that can never be silent in CI.

    GitHub Actions sets `CI` platform-wide and `ci.yml` installs no node step --
    node comes from the runner image. So a missing node must turn this file RED
    on CI rather than green-but-blind, which is exactly what a bare skip would
    do, and what a green check that did not look looks like.
    """
    node = shutil.which("node")
    if node is None:
        assert not os.environ.get("CI"), "node is required in CI to run this test"
        pytest.skip("node is not installed on this machine")
    return node


def _run_predicate_in_node(node: str, source: str, driver: str, payload) -> list:
    """Execute the SHIPPED predicate. A Python re-implementation would test itself.

    JavaScript and `re` differ on flags, on `\\b` at the edges, and on what `^`
    does after a lone carriage return -- and which bodies match is the entire
    substance of this change.
    """
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "harness.js"
        harness.write_text(source + "\n" + driver)
        done = subprocess.run(
            [node, str(harness), json.dumps(payload)],
            capture_output=True,
            text=True,
            check=True,
        )
    return json.loads(done.stdout)


def _predicate_source(script: str) -> str:
    found = _PREDICATE_RE.search(script)
    assert found, "could not locate the shared predicate -- extraction must fail loudly"
    source = found.group(0)
    for name in (
        "REQUEST_MARKER",
        "OUR_REQUEST",
        "REQUEST_STARTS_LINE",
        "REQUEST_ENDS_LINE",
        "alreadyRequested",
    ):
        assert name in source, f"extraction lost {name}"
    return source


def test_both_steps_share_one_byte_identical_predicate():
    """Drift here is silent: the budget stops recognising its own requests.

    One step writes the request; the other decides whether one was already made.
    They are separate scripts in separate steps, so nothing but this test couples
    them -- and an earlier version of this change had the post step re-derive a
    WEAKER marker-only check, which meant a human who typed the request between
    the two steps was invisible and got a duplicate on top of their own.
    """
    eligibility = _predicate_source(_eligibility_script())
    post = _predicate_source(_post_script())
    assert eligibility == post, (
        "the two copies of the spent-request predicate have drifted:\n"
        f"--- eligibility ---\n{eligibility}\n--- post ---\n{post}"
    )
    assert "<!--" in eligibility, (
        "the marker must be an HTML comment so it does not render for readers"
    )


def test_the_posted_body_carries_the_marker_and_the_request():
    script = _post_script()
    assert "'@codex review'" in script, "the reviewer matches on this literal"
    assert "`${REQUEST}\\n\\n${REQUEST_MARKER}`" in script, (
        "the posted body must carry BOTH the request the reviewer matches and "
        "the marker the budget recognises"
    )


def test_the_post_step_rechecks_for_a_request_before_writing():
    """An idempotence check, and it must fail CLOSED and precede the write.

    These are position assertions, and position assertions are weak: an audit
    deleted the ENTIRE duplicate-suppression block and every test still passed,
    because nothing here read what the block DECIDES. The behavioural half is
    `test_the_recheck_actually_declines_a_duplicate` below; this one only pins
    the ordering and the fail-closed arm that a behavioural test cannot see.
    """
    script = _post_script()
    read = script.index("listComments")
    write = script.index("createComment")
    assert read < write, "the re-check must happen before the post, not after"
    between = script[read:write]
    assert "core.setFailed" in between, (
        "an unreadable comment list must fail closed -- 'could not read' is not "
        "'nothing found', and a duplicate request cannot be refunded"
    )
    assert "paginate" in script[:read], (
        "a capped read of a long thread looks exactly like 'no request exists'"
    )


def test_the_recheck_actually_declines_a_duplicate():
    """The re-check must DECIDE, not merely be present.

    Deleting the suppression block, or repointing it at a different string, both
    survived every other test in this file. So the decision is exercised here:
    the shipped predicate is run against a comment list that carries our own
    request and one that does not, and the two must disagree.
    """
    node = _node_or_skip()
    source = _predicate_source(_post_script())
    suppression = _post_script()
    assert "posted.some(c => alreadyRequested(c.body))" in suppression, (
        "the re-check must consult the SHARED predicate; a marker-only variant "
        "cannot see a human who typed the request in the meantime"
    )

    ours = "@codex review\n\n<!-- genesis-auto-review-request -->"
    lists = [
        [{"body": ours}],  # our own request already there -> decline
        [{"body": "looks good to me"}],  # unrelated chatter -> proceed
        [],  # nothing at all -> proceed
        [{"body": "please @codex review"}],  # a human already asked -> decline
    ]
    got = _run_predicate_in_node(
        node,
        source,
        "const lists = JSON.parse(process.argv[2]);\n"
        "console.log(JSON.stringify("
        "lists.map(l => l.some(c => alreadyRequested(c.body)))));\n",
        lists,
    )
    assert got == [True, False, False, True], got


_OUR_BODY = "@codex review\n\n<!-- genesis-auto-review-request -->"

_ALREADY_REQUESTED_CASES = [
    # (comment body, is this already a request?)
    # --- our own request, and real human ones ---------------------------------
    (_OUR_BODY, True),
    (_OUR_BODY.replace("\n", "\r\n"), True),  # CRLF round trip
    ("@codex review", True),
    ("  @codex review", True),
    ("@codex review\n\nplease look at the parser", True),
    ("Some context first.\n@codex review", True),
    ("Some context first.\r\n@codex review", True),
    ("please @codex review", True),  # end-anchored: 0 measured, precaution
    ("cc @codex review", True),
    ("> @codex review", True),  # a quote is evidence a request happened
    # --- prose that merely NAMES the request ---------------------------------
    # With no retry behind the budget, one of these silently spends an issue's
    # only automatic review.
    ("we should check whether @codex review fires for forks", False),
    ("I think @codex review is the wrong trigger here", False),
    ("nothing relevant to see", False),
    ("", False),
    # The REVIEWER'S OWN footer. MEASURED: 95 of the 1000 most recent comments
    # on this repository carry this line, it arrives before any human asks, and
    # under the old predicate it sealed the issue by itself.
    ('- Comment "@codex review" or "@codex security review".', False),
    # The marker QUOTED inside prose. Its literal is published in this file, in
    # the workflow and in the changelog, so a maintainer explaining the
    # mechanism must not spend the budget. This is why the marker arm is exact
    # equality and not containment.
    ("the workflow posts <!-- genesis-auto-review-request --> as a marker", False),
    ("```yaml\nbody: <!-- genesis-auto-review-request -->\n```", False),
    # A bare marker with no request is not a request either.
    ("<!-- genesis-auto-review-request -->", False),
    # `\b` must not admit these.
    ("@codex reviewing the parser now", False),
    ("foo@codex review", False),
]


def test_already_requested_matches_real_requests_and_not_prose():
    """Run the SHIPPED predicate in node, not a Python re-implementation.

    Translating a JavaScript regex into `re` and asserting on that would test
    the translation. The flags differ, `\\b` differs at the edges, and the whole
    point of the change is which bodies match.
    """
    got = _run_predicate_in_node(
        _node_or_skip(),
        _predicate_source(_eligibility_script()),
        "const cases = JSON.parse(process.argv[2]);\n"
        "console.log(JSON.stringify(cases.map(alreadyRequested)));\n",
        [body for body, _ in _ALREADY_REQUESTED_CASES],
    )
    expected = [want for _, want in _ALREADY_REQUESTED_CASES]
    mismatches = [
        (body, want, actual)
        for (body, want), actual in zip(_ALREADY_REQUESTED_CASES, got, strict=True)
        if want != actual
    ]
    assert not mismatches, f"predicate disagrees on: {mismatches}"
    # Both polarities must be exercised, or this passes against a predicate
    # that simply returns a constant.
    assert any(expected) and not all(expected)
