"""Guardian ↔ Genesis dialogue — two-way health concern protocol.

Before the Guardian attempts any manual recovery, it first tries to contact
Genesis and make it aware of the problem. Genesis gets a chance to fix itself.
Only if Genesis is truly dark or explicitly says it can't handle the problem
does the Guardian proceed to manual recovery.

Protocol:
  Guardian → POST /api/genesis/guardian-dialogue
  Genesis  → {acknowledged, status, action, eta_s, context}

Response statuses:
  "handling"   — Genesis is aware and acting. Guardian waits eta_s, re-probes.
  "need_help"  — Genesis can't fix it. Guardian proceeds to recovery.
  "stand_down" — Expected state (maintenance, pause). Guardian enters PAUSED.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum

from genesis.guardian.config import GuardianConfig
from genesis.guardian.health_signals import HealthSnapshot

logger = logging.getLogger(__name__)


class DialogueStatus(StrEnum):
    """Genesis's response to a Guardian health concern."""

    HANDLING = "handling"
    NEED_HELP = "need_help"
    STAND_DOWN = "stand_down"


@dataclass(frozen=True)
class DialogueRequest:
    """What the Guardian sends to Genesis."""

    signals_failing: list[str]
    signals_ok: list[str]
    duration_s: float
    guardian_state: str
    suspicious: dict[str, str]

    def to_dict(self) -> dict:
        return {
            "type": "health_concern",
            "signals_failing": self.signals_failing,
            "signals_ok": self.signals_ok,
            "duration_s": self.duration_s,
            "guardian_state": self.guardian_state,
            "suspicious": self.suspicious,
        }


@dataclass(frozen=True)
class DialogueResponse:
    """What Genesis sends back to the Guardian."""

    acknowledged: bool
    status: DialogueStatus
    action: str
    eta_s: int
    context: str

    @classmethod
    def unreachable(cls) -> DialogueResponse:
        """Factory for when Genesis can't be reached at all."""
        return cls(
            acknowledged=False,
            status=DialogueStatus.NEED_HELP,
            action="",
            eta_s=0,
            context="Genesis unreachable — no response to dialogue",
        )

    @classmethod
    def error(cls, detail: str) -> DialogueResponse:
        """Factory for when Genesis responds with an error."""
        return cls(
            acknowledged=False,
            status=DialogueStatus.NEED_HELP,
            action="",
            eta_s=0,
            context=f"Genesis responded with error: {detail}",
        )


def build_request(
    snapshot: HealthSnapshot,
    duration_s: float,
    guardian_state: str,
) -> DialogueRequest:
    """Build a dialogue request from the current health snapshot."""
    failing = [s.name for s in snapshot.failed_signals]
    ok = [s.name for s in snapshot.signals.values() if s.alive]
    suspicious = {
        s.name: s.detail
        for s in snapshot.suspicious_warnings
    }

    return DialogueRequest(
        signals_failing=failing,
        signals_ok=ok,
        duration_s=duration_s,
        guardian_state=guardian_state,
        suspicious=suspicious,
    )


async def send_dialogue(
    config: GuardianConfig,
    request: DialogueRequest,
) -> DialogueResponse:
    """Send a health concern to Genesis and get its response.

    This is a synchronous HTTP POST — if Genesis's web layer is dead,
    we get a connection error, which IS the "truly dark" signal.
    """
    url = f"{config.health_url}/api/genesis/guardian-dialogue"
    payload = json.dumps(request.to_dict()).encode("utf-8")

    try:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        # Use executor to avoid blocking
        import asyncio
        loop = asyncio.get_running_loop()

        def _do_post() -> tuple[int, str]:
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                    return resp.status, body
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                return exc.code, body
            except (urllib.error.URLError, TimeoutError, OSError):
                return 0, ""

        status, body = await loop.run_in_executor(None, _do_post)

        if status == 0:
            logger.warning("Genesis dialogue: connection failed (truly dark)")
            return DialogueResponse.unreachable()

        if status == 503:
            logger.info("Genesis dialogue: 503 (bootstrapping)")
            return DialogueResponse.error("bootstrapping (503)")

        if status >= 500:
            logger.warning("Genesis dialogue: server error %d", status)
            return DialogueResponse.error(f"HTTP {status}: {body[:200]}")

        if status != 200:
            logger.warning("Genesis dialogue: unexpected status %d", status)
            return DialogueResponse.error(f"HTTP {status}")

        # Parse response
        data = json.loads(body)

        acknowledged = data.get("acknowledged", False)
        status_str = data.get("status", "need_help")

        try:
            dialogue_status = DialogueStatus(status_str)
        except ValueError:
            logger.warning("Unknown dialogue status from Genesis: %s", status_str)
            dialogue_status = DialogueStatus.NEED_HELP

        return DialogueResponse(
            acknowledged=acknowledged,
            status=dialogue_status,
            action=data.get("action", ""),
            eta_s=int(data.get("eta_s", 0)),
            context=data.get("context", ""),
        )

    except json.JSONDecodeError as exc:
        logger.warning("Genesis dialogue: invalid JSON response: %s", exc, exc_info=True)
        return DialogueResponse.error("invalid JSON response")
    except Exception as exc:
        logger.error("Genesis dialogue failed: %s", exc, exc_info=True)
        return DialogueResponse.unreachable()
