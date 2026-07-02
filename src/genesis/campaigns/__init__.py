"""Campaign subsystem — persistent, scheduled, LLM-driven operations.

Public/private contract (enshrined; do not violate):

    Campaign INFRASTRUCTURE ships in the public repo — this package (runner,
    control, models, prechecks), the ``campaigns`` table schema,
    ``db/crud/campaigns.py``, and ``mcp/health/campaign_tools.py``.

    Individual CAMPAIGNS are USER DATA, never infrastructure. A campaign's
    name, strategy doc, cadence, session profile, and state live only in the
    ``campaigns`` DB table (created by the user at runtime) and are backed up to
    the user's PRIVATE backups repo. They MUST NEVER enter the public repo.

    Unlike modules — which ship built-in defaults under ``config/modules/*.yaml``
    — campaigns ship ZERO defaults. There is no ``config/campaigns/`` directory;
    a fresh install starts with an empty campaigns table.

    Therefore: never hardcode a specific campaign's name, prompt, target, or
    schedule into tracked source (code, docstrings, examples, comments). If a
    session type is genuinely reusable, express it as a GENERIC role (e.g. the
    ``community-responder`` DirectSession profile), not one named after a live
    campaign.
"""
