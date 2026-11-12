"""Does a path component name a credential file?

One predicate shared by every surface that serves file names or contents to a
caller: the dashboard's file routes (which refuse to serve such a path) and the
``locate`` MCP tool (which never content-searches one). Moved here from
``genesis.dashboard.routes.files`` unchanged, so both use the same measured
vocabulary instead of each keeping its own list.
"""

from __future__ import annotations

import re

# Vocabulary that marks a path component as credential-bearing. Matched
# STRUCTURALLY rather than by exact filename, because an exact-name list is a
# list of the spellings someone thought of: ``_BLOCKED_NAMES`` carried
# ``credentials.json`` while the real Claude Code file is ``.credentials.json``,
# and a single leading dot was enough to serve an OAuth token to an anonymous
# caller.
#
# Each rule below is anchored rather than substring-matched, and the anchoring
# is MEASURED, not chosen. Sweeping 87,219 non-vendor files under the three
# allowed roots: this vocabulary refuses 68 (0.078%), against 12 (0.014%) for
# the exact-name predicate it replaces. Of the 56 newly refused, 48 are genuine
# credentials on this install and 8 are prose or source files that merely end a
# word in "token" (``tokens.css``, ``session-tokens.ts``). An earlier draft
# matched a stem ANYWHERE in the component and scored 4.226% — 3,591 of those
# were one installed plugin whose NAME contains "token", i.e. a guard that
# makes a whole tree unbrowsable is one an operator routes around.
_CREDENTIAL_STEMS = frozenset(
    {
        "token",
        "tokens",
        "credential",
        "credentials",
        "cred",
        "creds",
        "passwd",
        "password",
        "passphrase",
    }
)

# Extensions whose whole purpose is to carry a key or an environment full of
# them. ``_BLOCKED_NAMES`` already listed bare ``.env`` and ``secrets.env``;
# this is the generalisation it was reaching for, and it is what closes
# ``*_creds.env`` and the private-key families.
_CREDENTIAL_SUFFIXES = (
    ".env",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jwt",
    ".kdbx",
    ".asc",
    ".ovpn",
)

# Names that carry credentials while matching no stem and no suffix.
_CREDENTIAL_NAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".htpasswd",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
    }
)


def is_credential_component(part: str) -> bool:
    """Does one path component name a credential?

    Three anchored rules, in cost order. The stem rule matches the LAST word of
    each dot-separated segment, splitting on ``_`` and ``-``: that is what tells
    ``api-token`` and ``service_creds.env`` (credentials) apart from
    ``token-optimizer`` and ``analyze-token-usage.py`` (topics), because a
    credential file's name ENDS in the thing it holds while a topical one leads
    with it.

    Stated gap, so nobody reads this as a class guarantee: a credential whose
    name matches none of the three — ``token-store``, an ``.ini`` holding an
    API key, anything named for its service alone — is still served. This
    narrows a measured exposure; it does not close the category.
    """
    name = part.lower()
    if name in _CREDENTIAL_NAMES:
        return True
    if name.endswith(_CREDENTIAL_SUFFIXES):
        return True
    # A credential extension is not always the LAST one. The dotenv convention
    # puts the environment after it — `.env.local`, `.env.production` — so an
    # endswith test sees `.local` and passes the file through. Checking every
    # dot-segment boundary catches those without widening the vocabulary.
    if any(f".{segment}" in _CREDENTIAL_SUFFIXES for segment in name.split(".") if segment):
        return True
    for segment in name.lstrip(".").split("."):
        if segment and re.split(r"[_\-]+", segment)[-1] in _CREDENTIAL_STEMS:
            return True
    return False
