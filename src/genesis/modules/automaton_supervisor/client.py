"""Conway Cloud API client.

Async HTTP client for Conway Cloud sandbox management, credits, and
Automaton state interaction. Mapped from the Automaton TypeScript source
(src/conway/client.ts) — 26 endpoints.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from .types import ExecResult, PricingTier, SandboxInfo

logger = logging.getLogger(__name__)

# Conway Cloud has an intermittent 404 load balancer bug.
# Retry 404s with backoff (documented in their client.ts).
_MAX_404_RETRIES = 3
_DEFAULT_TIMEOUT = 30.0


class ConwayCloudError(Exception):
    """Error from Conway Cloud API."""

    def __init__(self, message: str, status: int = 0, method: str = "", path: str = ""):
        super().__init__(message)
        self.status = status
        self.method = method
        self.path = path


class ConwayCloudClient:
    """Async client for the Conway Cloud REST API.

    All sandbox operations target a specific sandbox_id. Auth and credit
    operations are account-level.
    """

    def __init__(
        self,
        api_url: str = "https://api.conway.tech",
        api_key: str = "",
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._api_url,
                timeout=self._timeout,
                headers={
                    "Authorization": self._api_key,
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        retry_404: bool = True,
    ) -> Any:
        """Make an API request with 404 retry logic."""
        client = await self._ensure_client()
        max_retries = _MAX_404_RETRIES if retry_404 else 0

        for attempt in range(max_retries + 1):
            try:
                if method == "GET":
                    resp = await client.get(path)
                elif method == "POST":
                    resp = await client.post(path, json=body)
                elif method == "DELETE":
                    resp = await client.delete(path)
                else:
                    resp = await client.request(method, path, json=body)

                if resp.status_code == 404 and attempt < max_retries:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue

                if resp.status_code >= 400:
                    text = resp.text
                    raise ConwayCloudError(
                        f"Conway API error: {method} {path} -> {resp.status_code}: {text}",
                        status=resp.status_code,
                        method=method,
                        path=path,
                    )

                content_type = resp.headers.get("content-type", "")
                if "application/json" in content_type:
                    return resp.json()
                return resp.text

            except httpx.TimeoutException as exc:
                if attempt < max_retries:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                raise ConwayCloudError(
                    f"Conway API timeout: {method} {path}: {exc}",
                    method=method,
                    path=path,
                ) from exc

        raise ConwayCloudError(f"Conway API exhausted retries: {method} {path}")

    # ── Sandbox Lifecycle ──────────────────────────────────────────

    async def create_sandbox(
        self,
        name: str,
        *,
        vcpu: int = 1,
        memory_mb: int = 512,
        disk_gb: int = 5,
        region: str | None = None,
    ) -> SandboxInfo:
        """Create a new Conway Cloud sandbox (VM).

        WARNING: Sandboxes are non-deletable and non-refundable.
        """
        body: dict[str, Any] = {
            "name": name,
            "vcpu": vcpu,
            "memory_mb": memory_mb,
            "disk_gb": disk_gb,
        }
        if region:
            body["region"] = region

        result = await self._request("POST", "/v1/sandboxes", body)
        return SandboxInfo(
            id=result.get("id") or result.get("sandbox_id", ""),
            status=result.get("status", "running"),
            region=result.get("region", ""),
            vcpu=result.get("vcpu", vcpu),
            memory_mb=result.get("memory_mb", memory_mb),
            disk_gb=result.get("disk_gb", disk_gb),
            terminal_url=result.get("terminal_url"),
            created_at=result.get("created_at", ""),
        )

    async def list_sandboxes(self) -> list[SandboxInfo]:
        """List all sandboxes for this account."""
        result = await self._request("GET", "/v1/sandboxes")
        sandboxes = result if isinstance(result, list) else result.get("sandboxes", [])
        return [
            SandboxInfo(
                id=s.get("id") or s.get("sandbox_id", ""),
                status=s.get("status", "unknown"),
                region=s.get("region", ""),
                vcpu=s.get("vcpu", 0),
                memory_mb=s.get("memory_mb", 0),
                disk_gb=s.get("disk_gb", 0),
                terminal_url=s.get("terminal_url"),
                created_at=s.get("created_at", ""),
            )
            for s in sandboxes
        ]

    async def exec(
        self,
        sandbox_id: str,
        command: str,
        timeout: int = 30,
    ) -> ExecResult:
        """Execute a command in a sandbox."""
        # Conway wraps commands in cd /root && ... for consistency
        wrapped = f"cd /root && {command}"
        result = await self._request(
            "POST",
            f"/v1/sandboxes/{sandbox_id}/exec",
            {"command": wrapped, "timeout": timeout},
        )
        return ExecResult(
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
            exit_code=result.get("exit_code") or result.get("exitCode", -1),
        )

    async def write_file(
        self,
        sandbox_id: str,
        path: str,
        content: str,
    ) -> None:
        """Write a file in a sandbox."""
        await self._request(
            "POST",
            f"/v1/sandboxes/{sandbox_id}/files/upload/json",
            {"path": path, "content": content},
        )

    async def read_file(self, sandbox_id: str, path: str) -> str:
        """Read a file from a sandbox."""
        from urllib.parse import quote

        result = await self._request(
            "GET",
            f"/v1/sandboxes/{sandbox_id}/files/read?path={quote(path)}",
            retry_404=False,
        )
        if isinstance(result, str):
            return result
        return result.get("content", "")

    async def expose_port(self, sandbox_id: str, port: int) -> dict:
        """Expose a port on a sandbox."""
        return await self._request(
            "POST",
            f"/v1/sandboxes/{sandbox_id}/ports/expose",
            {"port": port},
        )

    # ── Credits ────────────────────────────────────────────────────

    async def get_credits_balance(self) -> int:
        """Get current credit balance in cents."""
        result = await self._request("GET", "/v1/credits/balance")
        return result.get("balance_cents") or result.get("credits_cents", 0)

    async def get_pricing(self) -> list[PricingTier]:
        """Get available credit pricing tiers."""
        result = await self._request("GET", "/v1/credits/pricing")
        tiers = result if isinstance(result, list) else result.get("tiers", [])
        return [
            PricingTier(
                name=t.get("name", ""),
                amount_usd=t.get("amount_usd", 0.0),
                credits_cents=t.get("credits_cents", 0),
            )
            for t in tiers
        ]

    async def transfer_credits(
        self,
        to_sandbox: str,
        amount_cents: int,
    ) -> dict:
        """Transfer credits to another sandbox."""
        return await self._request(
            "POST",
            "/v1/credits/transfer",
            {"to_sandbox_id": to_sandbox, "amount_cents": amount_cents},
        )

    # ── Automaton State (via exec + SQLite) ────────────────────────

    async def query_state(
        self,
        sandbox_id: str,
        sql: str,
    ) -> list[dict]:
        """Run a read-only SQLite query against the Automaton's state.db.

        Uses the exec endpoint to run sqlite3 CLI in the sandbox.
        """
        # Escape single quotes in SQL for shell
        escaped = sql.replace("'", "'\\''")
        result = await self.exec(
            sandbox_id,
            f"sqlite3 -json ~/.automaton/state.db '{escaped}'",
            timeout=10,
        )
        if result.exit_code != 0:
            logger.warning(
                "state query failed (exit %d): %s", result.exit_code, result.stderr
            )
            return []
        try:
            return json.loads(result.stdout) if result.stdout.strip() else []
        except json.JSONDecodeError:
            logger.warning("state query returned non-JSON: %s", result.stdout[:200])
            return []

    async def get_agent_state(self, sandbox_id: str) -> str:
        """Get the Automaton's current agent state (setup/running/sleeping/dead/etc)."""
        rows = await self.query_state(
            sandbox_id,
            "SELECT value FROM kv WHERE key = 'agent_state'",
        )
        if rows:
            return rows[0].get("value", "unknown")
        return "unknown"

    async def get_turn_count(self, sandbox_id: str) -> int:
        """Get total turn count."""
        rows = await self.query_state(sandbox_id, "SELECT count(*) as cnt FROM turns")
        if rows:
            return rows[0].get("cnt", 0)
        return 0

    async def get_recent_turns(
        self,
        sandbox_id: str,
        limit: int = 10,
    ) -> list[dict]:
        """Get recent reasoning turns."""
        return await self.query_state(
            sandbox_id,
            f"SELECT id, timestamp, state, thinking, tool_calls, cost_cents "
            f"FROM turns ORDER BY created_at DESC LIMIT {limit}",
        )

    async def get_transactions(
        self,
        sandbox_id: str,
        limit: int = 20,
    ) -> list[dict]:
        """Get recent financial transactions."""
        return await self.query_state(
            sandbox_id,
            f"SELECT id, type, amount_cents, balance_after_cents, description, created_at "
            f"FROM transactions ORDER BY created_at DESC LIMIT {limit}",
        )

    async def get_skills(self, sandbox_id: str) -> list[dict]:
        """Get installed skills."""
        return await self.query_state(
            sandbox_id,
            "SELECT name, description, auto_activate, enabled FROM skills WHERE enabled = 1",
        )

    async def inject_inbox(
        self,
        sandbox_id: str,
        message: str,
        from_addr: str = "genesis-supervisor",
    ) -> None:
        """Inject a message into the Automaton's inbox.

        The Automaton claims up to 10 inbox messages per wake cycle and
        processes them as high-priority input.

        Uses base64 encoding to safely transport arbitrary message content
        through the shell without injection risk.
        """
        import base64

        msg_b64 = base64.b64encode(message.encode()).decode()
        from_b64 = base64.b64encode(from_addr.encode()).decode()

        # Decode in the sandbox and pipe into sqlite3 via a parameterized script
        # This avoids any shell quoting / SQL injection issues
        script = (
            f"python3 -c \""
            f"import base64,sqlite3,uuid;"
            f"db=sqlite3.connect('/root/.automaton/state.db');"
            f"db.execute("
            f"'INSERT INTO inbox_messages (id,from_address,content,received_at,status) "
            f"VALUES (?,?,?,datetime(\\'now\\'),\\'received\\')',"
            f"(uuid.uuid4().hex,"
            f"base64.b64decode('{from_b64}').decode(),"
            f"base64.b64decode('{msg_b64}').decode()));"
            f"db.commit();db.close()\""
        )
        await self.exec(sandbox_id, script, timeout=10)

    async def inject_skill(
        self,
        sandbox_id: str,
        skill_name: str,
        skill_md: str,
    ) -> None:
        """Inject a SKILL.md file into the Automaton's skills directory.

        The Automaton reloads skills from ~/.automaton/skills/ at the start
        of every agent loop iteration.
        """
        skill_dir = f"~/.automaton/skills/{skill_name}"
        # Create directory and write SKILL.md
        await self.exec(sandbox_id, f"mkdir -p {skill_dir}", timeout=5)
        await self.write_file(sandbox_id, f"{skill_dir}/SKILL.md", skill_md)
        logger.info("Injected skill '%s' into sandbox %s", skill_name, sandbox_id)
