# Manual-only skills in Genesis

Set `disable-model-invocation: true` in a skill's YAML frontmatter when it is
reserved for an explicit owner request. Genesis preserves that field as a JSON
boolean in the generated catalog, including its degraded parser paths. An
absent/false field retains the previous automatic behavior.

Flagged entries receive no keyword/name nudge, are omitted from the executor's
available-skill suggestions, and are skipped when assigned to an autonomous
step. Required delegation checks both the assigned skill and its delegate
before loading either body. Unmarked `research` still loads `web-research`.
The inventory and explicit foreground path lookup remain available. Usage
reporting retains flagged entries but marks them ineligible for nudges.

## Activation and verification

After the code and skill definition are installed through the controlled
deployment path, run `python3 scripts/generate_skill_catalog.py` from that
checkout. Verify the real entry contains the boolean field. Restart cached
executor consumers through the controlled deployment path; the executor caches
the catalog for its process lifetime. An ordinary deploy does not itself run
the generator. The prompt hook's delayed missing/stale refresh is insufficient
to establish immediate activation.

Qualify generator → catalog → hook and executor behavior together. Test an
ordinary skill as a positive control as well as a flagged name, keyword and
assigned/delegated body. The fixture-based tests establish Genesis's custom
discovery behavior. They do not establish native invocation behavior for every
installed Claude Code version or improved model judgment.

This is a workflow/discovery policy, not a filesystem or authorization sandbox.
Explicit loading still follows existing caller permissions and skill scope.
