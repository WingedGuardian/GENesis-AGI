"""Closed research operation schemas and trusted snapshot provenance."""

import asyncio
import json
import time
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from genesis.peers.broker import BrokerRefusal, EmptyArguments
from genesis.peers.digests import operation_digest
from genesis.peers.research_fetch import canonical_url, fetch_public
from genesis.security.output_scanner import scan_outbound
from genesis.security.sanitizer import ContentSanitizer, ContentSource
from genesis.web.search import WebSearcher


class SearchArguments(EmptyArguments):
    query: str
    max_results: int = Field(default=5, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def bounded_query(cls, value):
        if not value.strip() or len(value.encode()) > 4096:
            raise ValueError("Research arguments refused")
        return value


class FetchArguments(EmptyArguments):
    url: str

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        canonical_url(value)
        return value


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SearchItem(Closed):
    title: str = Field(max_length=16384)
    url: str = Field(max_length=16384)
    snippet: str = Field(max_length=32768)


class SearchData(Closed):
    backend: Literal["searxng", "brave"]
    results: list[SearchItem] = Field(max_length=10)
    source: Literal["external_untrusted"] = "external_untrusted"


class FetchData(Closed):
    original_url: str = Field(max_length=16384)
    final_url: str = Field(max_length=16384)
    title: str = Field(max_length=16384)
    content: str = Field(max_length=1049600)
    source: Literal["external_untrusted"] = "external_untrusted"


_SCHEMAS = {
    "research_search": (SearchArguments, SearchData),
    "research_fetch": (FetchArguments, FetchData),
}


def search_url(value):
    """Validate displayed search links without DNS or fetching the target."""
    try:
        if not isinstance(value, str) or not value or len(value.encode()) > 8192:
            raise ValueError
        if any(ord(c) <= 32 or ord(c) == 127 for c in value):
            raise ValueError
        parsed = httpx.URL(value)
        authority = urlsplit(value).netloc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.host
            or "@" in authority
            or parsed.userinfo
        ):
            raise ValueError
    except (ValueError, httpx.InvalidURL):
        raise BrokerRefusal("operation_refused", 400) from None
    return value


def validate_receipt(result, digest):
    if not isinstance(result, dict) or result.keys() != {"operation", "arguments", "data"}:
        raise ValueError("Research receipt refused")
    schemas = _SCHEMAS.get(result["operation"]) if isinstance(result["operation"], str) else None
    if schemas is None:
        raise ValueError("Research receipt refused")
    argument_schema, data_schema = schemas
    arguments = argument_schema.model_validate(result["arguments"])
    data = data_schema.model_validate(result["data"])
    if data.model_dump() != result["data"]:
        raise ValueError("Research receipt refused")
    if (
        arguments.model_dump() != result["arguments"]
        or operation_digest(result["operation"], arguments.model_dump()) != digest
    ):
        raise ValueError("Research receipt refused")
    if isinstance(data, SearchData) and len(data.results) > arguments.max_results:
        raise ValueError("Research receipt refused")
    if not scan_outbound(json.dumps(result, ensure_ascii=False, allow_nan=False)).safe:
        raise ValueError("Research receipt refused")


class PeerResearch:
    def __init__(self, broker):
        self.broker = broker
        self.searcher = None
        self.sanitizer = ContentSanitizer()
        broker.register_operation(
            "research_search", "research", SearchArguments, self.search, immutable_read=True
        )
        broker.register_operation(
            "research_fetch", "research", FetchArguments, self.fetch, immutable_read=True
        )

    def validate(self, result, digest):
        operation = result.get("operation") if isinstance(result, dict) else None
        expected = {
            "research_search": (SearchArguments, self.search, "research"),
            "research_fetch": (FetchArguments, self.fetch, "research"),
        }
        if (
            operation not in expected
            or self.broker._operations.get(operation) != expected[operation]
            or operation not in self.broker._read_operations
        ):
            raise ValueError("Research receipt refused")
        validate_receipt(result, digest)

    async def close(self):
        if self.searcher is not None:
            await self.searcher._client.aclose()

    def _text(self, value, limit, *, source=ContentSource.WEB_SEARCH):
        if (
            not isinstance(value, str)
            or len(value.encode()) > limit
            or not scan_outbound(value).safe
        ):
            raise BrokerRefusal("operation_refused", 400)
        return self.sanitizer.wrap_content(value, source)

    def _inputs(self, arguments):
        if not scan_outbound(json.dumps(arguments.model_dump(), ensure_ascii=False)).safe:
            raise BrokerRefusal("operation_refused", 400)

    def _result(self, operation, arguments, data):
        result = {"operation": operation, "arguments": arguments.model_dump(), "data": data}
        validate_receipt(result, operation_digest(operation, arguments.model_dump()))
        return result

    async def search(self, task, decisions, arguments):
        self._inputs(arguments)
        if self.searcher is None:
            self.searcher = WebSearcher()
        with_timeout = min(20.0, max(0.0, task_deadline(task) - time.time()))
        async with asyncio.timeout(with_timeout):
            response = await self.searcher.search(
                arguments.query, max_results=arguments.max_results, private_observability=True
            )
            if (
                response.error
                or response.backend_used is None
                or len(response.results) > arguments.max_results
            ):
                raise BrokerRefusal("operation_refused", 400)
            data = SearchData(
                backend=response.backend_used.value,
                results=[
                    SearchItem(
                        title=self._text(item.title, 8192),
                        url=self._text(search_url(item.url), 8192),
                        snippet=self._text(item.snippet, 24576),
                    )
                    for item in response.results
                ],
            )
            return self._result("research_search", arguments, data.model_dump())

    async def fetch(self, task, decisions, arguments):
        self._inputs(arguments)
        data = await fetch_public(
            arguments.url, timeout_s=max(0.0, task_deadline(task) - time.time())
        )
        wrapped = FetchData(
            **{
                key: self._text(
                    value, 1048576 if key == "content" else 8192, source=ContentSource.WEB_FETCH
                )
                for key, value in data.items()
            }
        )
        return self._result("research_fetch", arguments, wrapped.model_dump())


def task_deadline(task):
    from datetime import datetime

    return datetime.fromisoformat(task["expires_at"]).timestamp()
