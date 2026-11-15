"""P3 authentication into a nonsecret, complete SDK session principal."""

import json
from dataclasses import asdict

from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_http_request
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

from genesis.caller_context import CallerContext
from genesis.caller_credentials import FORWARDER, CallerResolver, credential_file_name


def _unique_fields(pairs):
    value = dict(pairs)
    if len(pairs) != len(value):
        raise ValueError("Duplicate caller field")
    return value


def _invalid_constant(_value):
    raise ValueError("Invalid caller constant")


def usable_credentials(resolver, service):
    if not isinstance(resolver, CallerResolver):
        raise ValueError("Shared MCP requires the P3 resolver")
    usable = {FORWARDER}
    if service in {"health", "memory"}:
        usable |= {credential_file_name("external"), credential_file_name("validator")}
    if not resolver.credential_kinds & usable:
        raise ValueError("No usable shared service credential kind")


class CallerAuth(TokenVerifier):
    def __init__(self, resolver, service):
        super().__init__()
        self.resolver, self.service = resolver, service

    async def verify_token(self, token):
        headers = get_http_request().headers
        forwarded = headers.getlist("x-genesis-caller")
        if len(headers.getlist("authorization")) != 1 or len(forwarded) > 1:
            return None
        fields = None
        if forwarded:
            value = forwarded[0]
            if len(value) > 4096 or not value.isascii():
                return None
            try:
                fields = json.loads(value, object_pairs_hook=_unique_fields, parse_constant=_invalid_constant)
            except (ValueError, RecursionError):
                return None
        caller = self.resolver.resolve(token, fields)
        if not isinstance(caller, CallerContext):
            return None
        if caller.profile is not None and (
            caller.profile not in {"external", "validator"} or self.service not in {"health", "memory"}
        ):
            return None
        return AccessToken(token=token, client_id="genesis-" + self.service, scopes=[],
            subject=json.dumps(asdict(caller), sort_keys=True, separators=(",", ":")),
            claims={"genesis_caller": caller})


def authenticated_caller(scope) -> CallerContext:
    user = scope.get("user")
    if isinstance(user, AuthenticatedUser):
        caller = user.access_token.claims.get("genesis_caller")
        if isinstance(caller, CallerContext):
            return caller
    raise ToolError("Validated caller scope is unavailable")
