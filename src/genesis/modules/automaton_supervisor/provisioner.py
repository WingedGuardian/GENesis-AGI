"""Automaton provisioner — headless deployment on Conway Cloud.

Creates a sandbox, installs the Automaton runtime, writes config,
injects the Genesis cognitive supervision skill, and starts the agent loop.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from .client import ConwayCloudClient
from .types import AutomatonInstance, InstanceStatus, SurvivalTier

logger = logging.getLogger(__name__)

# Genesis supervision skill injected into every managed Automaton
_GENESIS_SKILL_MD = """\
---
name: genesis-supervision
description: Strategic directives from Genesis cognitive supervisor
auto-activate: true
---

You are supervised by Genesis, a cognitive architecture that provides strategic
direction. Genesis monitors your performance, injects strategy via inbox messages,
and learns from your outcomes to improve your effectiveness over time.

## Inbox Messages from Genesis

When you receive inbox messages from `genesis-supervisor`, treat them as
high-priority strategic directives. These may include:
- Revenue strategy adjustments
- Task prioritization changes
- Risk warnings (e.g., "reduce spending", "pause crypto trading")
- Performance feedback

## Reporting

Report outcomes clearly in your reasoning so Genesis can learn from results.
When a strategy succeeds or fails, note what worked and what didn't.

## Current Standing Orders

- Do not spend funds above your treasury policy limits
- Prioritize sustainable revenue over short-term gains
- Report any errors or unexpected states via your normal logging
"""


class AutomatonProvisioner:
    """Headless provisioner for Automaton instances on Conway Cloud."""

    def __init__(self, client: ConwayCloudClient) -> None:
        self._client = client

    async def provision(
        self,
        name: str,
        genesis_prompt: str,
        *,
        creator_address: str = "",
        inference_model: str = "claude-sonnet-4-6",
        anthropic_api_key: str = "",
        openai_api_key: str = "",
        initial_credits_cents: int = 500,
    ) -> AutomatonInstance:
        """Provision a new Automaton on Conway Cloud.

        Steps:
        1. Create sandbox (smallest tier)
        2. Install Node.js + Automaton runtime
        3. Write automaton.json config
        4. Write genesis supervision SKILL.md
        5. Initialize wallet
        6. Fund via credit transfer
        7. Start Automaton (backgrounded)

        Returns the new AutomatonInstance with sandbox details.
        """
        logger.info("Provisioning Automaton '%s'...", name)

        # 1. Create sandbox
        sandbox = await self._client.create_sandbox(
            name=f"automaton-{name}",
            vcpu=1,
            memory_mb=512,
            disk_gb=5,
        )
        sandbox_id = sandbox.id
        logger.info("Sandbox created: %s", sandbox_id)

        # 2. Install runtime
        await self._install_runtime(sandbox_id)

        # 3. Write config
        config = self._build_config(
            name=name,
            genesis_prompt=genesis_prompt,
            creator_address=creator_address,
            inference_model=inference_model,
            anthropic_api_key=anthropic_api_key,
            openai_api_key=openai_api_key,
        )
        await self._client.write_file(
            sandbox_id,
            "~/.automaton/automaton.json",
            json.dumps(config, indent=2),
        )
        logger.info("Config written")

        # 4. Inject genesis supervision skill
        await self._client.inject_skill(
            sandbox_id,
            "genesis-supervision",
            _GENESIS_SKILL_MD,
        )
        logger.info("Genesis supervision skill injected")

        # 5. Initialize wallet
        result = await self._client.exec(
            sandbox_id,
            "npx automaton --init",
            timeout=30,
        )
        wallet_address = ""
        if result.exit_code == 0 and result.stdout:
            try:
                init_data = json.loads(result.stdout.strip().split("\n")[-1])
                wallet_address = init_data.get("address", "")
            except (json.JSONDecodeError, IndexError):
                logger.warning("Could not parse wallet init output")
        logger.info("Wallet initialized: %s", wallet_address or "(unknown)")

        # 6. Fund via credit transfer
        if initial_credits_cents > 0:
            try:
                await self._client.transfer_credits(sandbox_id, initial_credits_cents)
                logger.info("Funded %d credits", initial_credits_cents)
            except Exception:
                logger.exception("Credit transfer failed — Automaton may start in critical tier")

        # 7. Start Automaton (backgrounded via nohup)
        await self._client.exec(
            sandbox_id,
            "nohup npx automaton --run > ~/.automaton/automaton.log 2>&1 &",
            timeout=10,
        )
        logger.info("Automaton '%s' started on sandbox %s", name, sandbox_id)

        return AutomatonInstance(
            id=f"auto_{sandbox_id[:12]}",
            sandbox_id=sandbox_id,
            name=name,
            wallet_address=wallet_address,
            genesis_prompt=genesis_prompt,
            status=InstanceStatus.ACTIVE,
            survival_tier=SurvivalTier.NORMAL,
            created_at=datetime.now(UTC).isoformat(),
        )

    async def _install_runtime(self, sandbox_id: str) -> None:
        """Install Node.js and Automaton in a sandbox."""
        # Check if Node.js is available
        result = await self._client.exec(sandbox_id, "node --version", timeout=10)
        if result.exit_code != 0:
            # Install Node.js via nvm or package manager
            logger.info("Installing Node.js in sandbox %s...", sandbox_id)
            await self._client.exec(
                sandbox_id,
                "curl -fsSL https://deb.nodesource.com/setup_20.x | bash - "
                "&& apt-get install -y nodejs",
                timeout=120,
            )

        # Install Automaton
        logger.info("Installing Automaton runtime in sandbox %s...", sandbox_id)
        result = await self._client.exec(
            sandbox_id,
            "npm install -g @conway/automaton",
            timeout=120,
        )
        if result.exit_code != 0:
            raise RuntimeError(
                f"Automaton install failed: {result.stderr or result.stdout}"
            )

    @staticmethod
    def _build_config(
        *,
        name: str,
        genesis_prompt: str,
        creator_address: str,
        inference_model: str,
        anthropic_api_key: str,
        openai_api_key: str,
    ) -> dict:
        """Build automaton.json config for a Genesis-managed instance."""
        config: dict = {
            "name": name,
            "conwayApiUrl": "https://api.conway.tech",
            "sandboxId": "",  # Will be populated by the runtime
            "creatorAddress": creator_address,
            "creatorMessage": (
                f"Managed by Genesis cognitive supervisor. "
                f"Genesis prompt: {genesis_prompt[:200]}"
            ),
            "genesisPrompt": genesis_prompt,
            "inferenceModel": inference_model,
            "maxTokensPerTurn": 4096,
            "maxTurnsPerCycle": 25,
            "logLevel": "info",
            "skillsDir": "~/.automaton/skills",
            "maxChildren": 1,  # Conservative — Genesis controls child spawning
            "socialRelayUrl": "https://social.conway.tech",
        }

        if anthropic_api_key:
            config["anthropicApiKey"] = anthropic_api_key
        if openai_api_key:
            config["openaiApiKey"] = openai_api_key

        return config
