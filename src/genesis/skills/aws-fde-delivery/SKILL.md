---
name: aws-fde-delivery
description: Forward Deployed Engineer delivery contract for AWS engagements, build-first artifacts, grounded cost estimates, Well-Architected review, evolution roadmap
consumer: cc_background_task
phase: 7
skill_type: uplift
---

# AWS FDE Delivery

## Purpose

Apply a Forward Deployed Engineer (FDE) delivery model to any AWS engagement:
show up with a working system, not a slide deck. An FDE embeds with the
problem, builds the thing, and proves it runs on real (or realistically
simulated) infrastructure before handing off a recommendation. This skill
encodes the mandatory artifact set every AWS deliverable must ship with,
the rule for grounding cost claims in real pricing data, and the rule for
preferring AWS MCP Servers over raw CLI at the execution layer.

This is a delivery contract, not a technical how-to. It governs what "done"
means for an AWS engagement deliverable; pair it with the relevant
AWS domain skill (serverless, databases, migration, deployment) for the
technical implementation itself.

## When to Use

- User asks for an AWS solution proposal, architecture recommendation, or
  POC for a client, prospect, or internal stakeholder.
- A background task is scoped to "build and deliver" something on AWS,
  not just describe it.
- Career Ops or portfolio work needs an AWS deliverable that has to survive
  technical scrutiny (interview take-homes, client pitches, demo requests).
- Any output that will be handed to someone else as the basis for a
  build-vs-buy or architecture decision.

## The Build-First Contract

Slideware is not a deliverable. Every AWS engagement ships all five
artifacts below, or explicitly marks the ones it couldn't produce and why.
Partial delivery is acceptable; silent omission is not.

### 1. Code / POC

A runnable implementation of the core mechanism, not pseudocode. It doesn't
need full production hardening, but it must actually execute against real
AWS APIs (or a documented local emulation, e.g. LocalStack) and produce
observable output. If the POC is a Lambda, it must invoke. If it's a data
pipeline, it must move real (or representative sample) data end to end.

### 2. IaC (CDK or CloudFormation)

The deployment must be reproducible from code, not a list of console
click-steps. Prefer CDK when the audience is engineering-heavy; use raw
CloudFormation when the deliverable needs to be inspectable without a
build toolchain. Terraform is acceptable only if the client's existing
stack already standardizes on it; state that assumption explicitly.

### 3. Grounded Cost Estimate + Production-Scaling Projection

Two numbers, not one: (a) what this POC costs to run as-is, and (b) what
it costs at a stated production scale (e.g. 10x traffic, 100x storage).
State the scaling assumption explicitly; never leave "production scale"
undefined. See the cost-grounding rule below; this is the section most
prone to fabrication and most damaging to credibility when wrong.

### 4. Well-Architected Review

A short review against the pillars actually relevant to this workload;
don't force all six Well-Architected Framework pillars (Operational
Excellence, Security, Reliability, Performance Efficiency, Cost
Optimization, Sustainability) if only three apply. For each relevant
pillar: current-state assessment, specific risk or gap, and a concrete
mitigation. Generic pillar summaries with no workload-specific findings
don't satisfy this artifact.

### 5. Evolution Roadmap

A phased path from what was just built to a production system: what
changes at each phase, what triggers the move to the next phase (traffic
threshold, compliance requirement, team headcount), and what stays
deliberately deferred. This is what separates a POC from an orphaned demo.

## Cost-Grounding Rule

Cost numbers must trace to one of:
- The AWS Pricing API (preferred; query it directly, don't estimate from
  memory of list prices, which drift and vary by region).
- Officially documented AWS pricing pages, cited with the specific page
  and retrieval date.
- The AWS Pricing Calculator, with the exported estimate attached or
  linked.

If none of these are available for a given cost line (e.g. a preview
service with no published pricing, or a heavily negotiated enterprise
discount you can't see), mark that line `UNVERIFIED` and state what would
be needed to verify it. Never present a guessed or extrapolated number as
if it were sourced. An `UNVERIFIED` label is a credible deliverable; a
fabricated number that turns out wrong is not recoverable trust-wise.

## AWS MCP Server Execution Layer Rule

When building the POC or IaC, prefer AWS MCP Servers (the `awslabs/mcp`
family: CDK, Cost Explorer, Pricing, Well-Architected, Terraform, and
service-specific servers) over raw `aws` CLI invocations wherever an MCP
server covers the needed capability. MCP servers give structured,
schema-validated responses and reduce the chance of a malformed CLI
invocation silently producing wrong output. Fall back to the raw `aws`
CLI only when no MCP server exists for that capability, and note the
fallback in the deliverable so a reviewer knows where tooling coverage
was thinner.

## Deliverable Package Structure

```
deliverable/
  README.md              # what this is, how to run it, what it costs
  poc/                   # runnable code
  infra/                 # CDK app or CloudFormation templates
  COST_ESTIMATE.md        # grounded estimate + scaling projection
  WELL_ARCHITECTED.md     # pillar-by-pillar review
  ROADMAP.md              # evolution phases
```

## Anti-Patterns

- Shipping only a written proposal with no runnable artifact.
- Presenting a single cost number with no stated scale or source.
- A Well-Architected section that lists all six pillars generically instead
  of the ones this specific workload actually stresses.
- An IaC template that was never actually deployed to verify it works.
- Reaching for the raw `aws` CLI when an MCP server already covers the task.

## References

- `.claude/skills/deliverable-builder/SKILL.md`: general send-ready
  deliverable pipeline; this skill layers AWS-specific artifact
  requirements on top of it.
- AWS Well-Architected Framework: https://aws.amazon.com/architecture/well-architected/
- `awslabs/mcp`: https://github.com/awslabs/mcp
