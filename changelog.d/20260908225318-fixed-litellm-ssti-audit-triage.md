- **The dependency audit no longer fails on a litellm advisory Genesis cannot
  reach.** `CVE-2026-37004` (CVSS 9.8) is server-side template injection through
  the `dotprompt_content` parameter of the `/prompts/test` endpoint, from an
  unsandboxed `jinja2.Environment`. The sink is
  `litellm/proxy/prompts/prompt_endpoints.py` and it is served only by the proxy,
  which Genesis never starts — the same structural reason the four `2026-11`
  proxy advisories are already triaged. Verified rather than assumed: all five
  `litellm` imports in the tree are the client library, and the tree contains no
  reference to `litellm.proxy`, `proxy_server`, or that endpoint.

  It is triaged rather than upgraded because pip-audit names 1.83.7 as the fix,
  which is past the 1.82.x releases the pin exists to avoid. Here the
  reachability argument is what holds, not the version — so the pin stays and the
  advisory is recorded with its reasoning next to the four of its own class.
