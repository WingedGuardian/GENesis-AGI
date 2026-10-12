"""Synthetic contract router returning exactly one recorded answer."""

from genesis.routing.types import RoutingResult


class Stub:
    def __init__(self, content):
        self.content, self.calls = content, []

    async def route_call(self, call_site_id, messages, **kwargs):
        self.calls.append((messages, self.content))
        return RoutingResult(success=True, call_site_id=call_site_id, content=self.content)
