"""Tests for genesis.resilience.network_watchdog_events.

The root network watchdog records Tailscale events in its /run telemetry; this
module, running as the owning user, turns a NEW event into one queued alert.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from genesis.guardian.alert import queue as q
from genesis.resilience.network_watchdog_events import enqueue_new_events

NOW = 1_900_000_000


def _telemetry(path: Path, event: dict | None, **extra) -> Path:
    ts = {"last_action": "none", **extra}
    if event is not None:
        ts["last_event"] = event
    path.write_text(json.dumps({"heal_count": 0, "tailscale": ts}))
    return path


def _event(action: str = "healed", at: int = NOW - 60, **kw) -> dict:
    return {
        "action": action,
        "at": at,
        "peer": "peer-a (100.64.0.7)",
        "handshake_age_s": 325,
        "rc": None,
        "rate_limit_s": 3600,
        **kw,
    }


@pytest.fixture
def paths(tmp_path):
    return {
        "state_file": tmp_path / "wd.json",
        "queue_root": tmp_path / "alerts" / "queue",
        "seen_file": tmp_path / "alerts" / "network-watchdog-seen.json",
    }


def _run(paths, **kw):
    return enqueue_new_events(now=NOW, **paths, **kw)


def _queued(paths) -> list[dict]:
    return [e for _, e in q.list_queued(paths["queue_root"])]


def test_a_heal_becomes_one_queued_alert(paths):
    _telemetry(paths["state_file"], _event())
    assert _run(paths) is True
    (entry,) = _queued(paths)
    assert entry["source"] == "network-watchdog"
    assert entry["severity"] == "warning"
    assert (
        entry["title"] == "Tailscale tunnel to peer-a (100.64.0.7) was stuck — restarted tailscaled"
    )
    assert "for 325s" in entry["body"]
    assert "after 60 min" in entry["body"]
    assert entry["dedupe_key"] == f"network-watchdog:tailscale:healed:{NOW - 60}"


def test_the_same_event_alerts_once(paths):
    _telemetry(paths["state_file"], _event())
    _run(paths)
    # Drained, then the next tick reads the same telemetry again.
    for path, _ in q.list_queued(paths["queue_root"]):
        path.unlink()
    assert _run(paths) is False
    assert _queued(paths) == []


def test_a_newer_event_alerts_again(paths):
    _telemetry(paths["state_file"], _event(at=NOW - 7200))
    _run(paths)
    _telemetry(paths["state_file"], _event(at=NOW - 60))
    assert _run(paths) is True
    assert len(_queued(paths)) == 2


@pytest.mark.parametrize(
    ("action", "severity", "title_part", "body_part"),
    [
        ("observed", "warning", "not restarted", "sudo systemctl restart tailscaled"),
        ("restart-failed", "critical", "restart FAILED", "(rc=1)"),
    ],
)
def test_each_action_has_its_own_message(paths, action, severity, title_part, body_part):
    _telemetry(
        paths["state_file"], _event(action=action, rc=1 if action == "restart-failed" else None)
    )
    _run(paths)
    (entry,) = _queued(paths)
    assert entry["severity"] == severity
    assert title_part in entry["title"]
    assert body_part in entry["body"]


def test_first_run_skips_an_old_event_but_records_it(paths):
    # Upgrading onto an install with a week-old heal in /run is not news.
    _telemetry(paths["state_file"], _event(at=NOW - 7 * 86400))
    assert _run(paths) is False
    assert _queued(paths) == []
    assert json.loads(paths["seen_file"].read_text()) == {"tailscale_event_at": NOW - 7 * 86400}


def test_first_run_alerts_a_recent_event(paths):
    _telemetry(paths["state_file"], _event(at=NOW - 3600))
    assert _run(paths) is True


@pytest.mark.parametrize(
    "content",
    [
        None,  # file absent
        "not json",
        json.dumps([]),
        json.dumps({"tailscale": []}),
        json.dumps({"tailscale": {"last_event": "x"}}),
        json.dumps({"tailscale": {"last_event": _event(action="none")}}),
        json.dumps({"tailscale": {"last_event": _event(at=True)}}),
        json.dumps({"tailscale": {"last_event": _event(at="123")}}),
        json.dumps({"tailscale": {"last_event": _event(at=0)}}),
    ],
)
def test_no_or_malformed_telemetry_queues_nothing_and_never_raises(paths, content):
    if content is not None:
        paths["state_file"].write_text(content)
    assert _run(paths) is False
    assert _queued(paths) == []


def test_oversized_telemetry_is_ignored(paths):
    paths["state_file"].write_text(
        json.dumps({"tailscale": {"last_event": _event()}, "pad": "x" * 1_100_000})
    )
    assert _run(paths) is False


def test_a_failed_enqueue_is_retried_next_tick(paths, monkeypatch):
    _telemetry(paths["state_file"], _event())
    monkeypatch.setattr(q, "enqueue_alert", lambda *a, **k: False)
    assert _run(paths) is False
    assert not paths["seen_file"].exists()  # not recorded, so not lost
    monkeypatch.undo()
    assert _run(paths) is True


def test_an_already_queued_event_is_recorded_as_seen(paths):
    _telemetry(paths["state_file"], _event())
    _run(paths)
    paths["seen_file"].unlink()  # e.g. the seen write failed last tick
    assert _run(paths) is False  # enqueue dedupes on the live key
    assert paths["seen_file"].exists()
    assert len(_queued(paths)) == 1


def test_the_state_file_follows_the_environment_override(paths, monkeypatch):
    _telemetry(paths["state_file"], _event())
    monkeypatch.setenv("GENESIS_NETWD_STATE_FILE", str(paths["state_file"]))
    assert (
        enqueue_new_events(now=NOW, queue_root=paths["queue_root"], seen_file=paths["seen_file"])
        is True
    )


@pytest.mark.asyncio
async def test_the_tick_drainer_delivers_a_watchdog_event(tmp_path, monkeypatch):
    # Wiring: the awareness tick's drainer reads the telemetry BEFORE draining,
    # so the event is delivered on the same tick.
    from genesis.outreach.types import OutreachResult, OutreachStatus
    from genesis.runtime.init import alert_drain

    root = tmp_path / "queue"
    monkeypatch.setattr("genesis.env.alert_queue_root", lambda: root)
    state = _telemetry(tmp_path / "wd.json", _event(at=int(time.time()) - 60))
    monkeypatch.setenv("GENESIS_NETWD_STATE_FILE", str(state))

    sent = []

    class _Pipe:
        async def submit_raw(self, text, request, **kw):
            sent.append((text, request))
            return OutreachResult(
                outreach_id="x",
                status=OutreachStatus.DELIVERED,
                channel="telegram",
                message_content=text,
                governance_result=None,
            )

    class _RT:
        _outreach_pipeline = _Pipe()
        _awareness_loop = None

    await alert_drain._make_drainer(_RT())()
    assert len(sent) == 1
    assert "restarted tailscaled" in sent[0][0]
    assert q.list_queued(root) == []
    assert (root.parent / "network-watchdog-seen.json").exists()


def test_repeated_observations_of_one_peer_share_one_key(paths):
    # Observe mode with an unwritable stamp records a new observation every
    # run; the owner must get one page, not one per run.
    for at in (NOW - 360, NOW - 240, NOW - 120):
        _telemetry(paths["state_file"], _event(action="observed", at=at))
        _run(paths)
    keys = {e["dedupe_key"] for e in _queued(paths)}
    assert keys == {"network-watchdog:tailscale:observed:peer-a (100.64.0.7)"}
    assert len(_queued(paths)) == 1


def test_a_sub_minute_limit_is_stated_in_seconds(paths):
    _telemetry(paths["state_file"], _event(rate_limit_s=30))
    _run(paths)
    assert "after 30s" in _queued(paths)[0]["body"]

