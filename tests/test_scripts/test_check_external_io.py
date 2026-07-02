"""WS5 external-I/O regression guard (scripts/check_external_io.py)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "check_external_io", _REPO_ROOT / "scripts" / "check_external_io.py",
)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def test_flags_planted_discord_egress(tmp_path):
    f = tmp_path / "sneaky.py"
    f.write_text('URL = "https://discord.com/api/v10/channels/1/messages"\n')
    violations = check.scan(tmp_path)
    assert [v[0] for v in violations] == [f.as_posix()]


def test_flags_planted_webhook_env(tmp_path):
    f = tmp_path / "hook.py"
    f.write_text('key = os.environ["DISCORD_WEBHOOK_ANNOUNCEMENTS"]\n')
    assert len(check.scan(tmp_path)) == 1


def test_allowlisted_file_is_skipped(tmp_path):
    f = tmp_path / "ok.py"
    f.write_text('base = "https://discord.com/api/v10"\n')
    assert check.scan(tmp_path, allowlist={f.as_posix()}) == []


def test_no_false_positive_on_legit_compute_post(tmp_path):
    # Legit compute/read egress (embeddings/search/etc.) must NOT trip the guard.
    (tmp_path / "compute.py").write_text(
        'r = await client.post("https://api.deepinfra.com/v1/embeddings", json=payload)\n'
    )
    assert check.scan(tmp_path) == []


def test_real_tree_is_clean_under_allowlist(monkeypatch):
    # The shipped ALLOWLIST must cover every current egress endpoint (fail-closed but
    # currently-clean). This is the completeness invariant the CI step also enforces.
    monkeypatch.chdir(_REPO_ROOT)
    assert check.scan(check.SCAN_ROOT) == []
