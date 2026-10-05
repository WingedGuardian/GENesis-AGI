"""External-I/O census guard tests."""

from __future__ import annotations

import ast
import importlib.util
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "check_external_io", _REPO_ROOT / "scripts" / "check_external_io.py"
)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def _violations(root: Path, cls: str):
    return check.scan(root, baseline={}, classes=(cls,))


def _shipped_inventory() -> set[str]:
    health_path = _REPO_ROOT / "src/genesis/observability/provider_health.py"
    tree = ast.parse(health_path.read_text(encoding="utf-8"))
    provider_urls = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_PROVIDER_URLS"
    )
    inventory = {
        host
        for url in provider_urls.values()
        if isinstance(url, str)
        if (host := urlsplit(url).hostname)
    }
    routing_yaml = (_REPO_ROOT / "config/model_routing.yaml").read_text(encoding="utf-8")
    inventory.update(re.findall(r"^\s*base_url:\s*https://([^/\s]+)", routing_yaml, re.MULTILINE))
    return inventory


def test_flags_planted_discord_egress(tmp_path):
    f = tmp_path / "sneaky.py"
    f.write_text('URL = "https://discord.com/api/v10/channels/1/messages"\n')

    violations = _violations(tmp_path, "egress")

    assert [(v.path, v.cls, v.kind) for v in violations] == [("sneaky.py", "egress", "unlisted")]


def test_flags_planted_webhook_env(tmp_path):
    (tmp_path / "hook.py").write_text('key = os.environ["DISCORD_WEBHOOK_ANNOUNCEMENTS"]\n')

    assert len(_violations(tmp_path, "egress")) == 1


def test_baseline_pins_sanctioned_egress_reference(tmp_path):
    f = tmp_path / "ok.py"
    f.write_text('base = "https://discord.com/api/v10"\n')
    baseline = {("ok.py", "egress"): (1, "fixture reference")}

    assert check.scan(tmp_path, baseline=baseline) == []


def test_flags_planted_provider_reference(tmp_path):
    f = tmp_path / "side_door.py"
    f.write_text('r = await client.post("https://api.deepinfra.com/v1/embeddings")\n')

    violations = _violations(tmp_path, "provider")

    assert [(v.path, v.cls, v.kind) for v in violations] == [
        ("side_door.py", "provider", "unlisted")
    ]


def test_baseline_pins_sanctioned_provider_reference(tmp_path):
    (tmp_path / "sanctioned.py").write_text('BASE = "https://api.deepinfra.com/v1/embeddings"\n')
    baseline = {("sanctioned.py", "provider"): (1, "fixture reference")}

    assert check.scan(tmp_path, baseline=baseline) == []


def test_no_false_positive_on_unrelated_url(tmp_path):
    (tmp_path / "compute.py").write_text(
        'r = await client.post("https://api.example.com/v1/embeddings")\n'
    )

    assert check.scan(tmp_path, baseline={}) == []


def test_provider_baseline_does_not_waive_egress(tmp_path):
    path = "src/genesis/research/perplexity.py"
    f = tmp_path / path
    f.parent.mkdir(parents=True)
    f.write_text(
        'PROVIDER = "https://api.perplexity.ai"\n'
        'post("https://discord.com/api/v10/channels/1/messages")\n'
    )
    baseline = {(path, "provider"): (1, "provider call site")}

    violations = check.scan(tmp_path, baseline=baseline)

    assert [(v.cls, v.kind) for v in violations] == [("egress", "unlisted")]


def test_egress_baseline_does_not_waive_provider(tmp_path):
    path = "src/genesis/mcp/discord_bot_mcp.py"
    f = tmp_path / path
    f.parent.mkdir(parents=True)
    f.write_text(
        'DISCORD = "https://discord.com/api/v10"\n'
        'post("https://api.openai.com/v1/chat/completions")\n'
    )
    baseline = {(path, "egress"): (1, "Discord call site")}

    violations = check.scan(tmp_path, baseline=baseline)

    assert [(v.cls, v.kind) for v in violations] == [("provider", "unlisted")]


def test_host_tld_continuation_is_not_flagged(tmp_path):
    (tmp_path / "unreach.py").write_text('URL = "https://api.openai.com.invalid/v1"\n')

    assert check.scan(tmp_path, baseline={}) == []


def test_host_label_continuation_is_not_flagged(tmp_path):
    (tmp_path / "fake.py").write_text(
        'A = "https://fakeslack.com/api/chat.postMessage"\nB = "https://myapi.openai.com/v1"\n'
    )

    assert check.scan(tmp_path, baseline={}) == []


def test_real_subdomain_and_port_still_flag(tmp_path):
    (tmp_path / "real.py").write_text(
        'A = "https://v2.api.openai.com/v1"\nB = "https://api.openai.com:8443/v1"\n'
    )

    assert len(_violations(tmp_path, "provider")) == 2


def test_real_tree_is_clean_under_baseline():
    assert check.scan(_REPO_ROOT, subdirs=check.SCAN_ROOTS) == []


def test_baseline_entries_are_live_and_have_rationales():
    measured = Counter(
        (violation.path, violation.cls)
        for violation in check.scan(
            _REPO_ROOT,
            baseline={},
            classes=tuple(check.CLASSES),
            subdirs=check.SCAN_ROOTS,
        )
    )
    for (relpath, cls), (expected_count, reason) in check.BASELINE.items():
        assert (_REPO_ROOT / relpath).is_file(), f"stale baseline entry: {relpath} ({cls})"
        assert measured[(relpath, cls)] == expected_count, f"baseline drift: {relpath} ({cls})"
        assert reason.strip(), f"empty rationale: {relpath} ({cls})"


def test_flags_uppercase_provider_hostname(tmp_path):
    (tmp_path / "uppercase.py").write_text('URL = "https://API.OPENAI.COM/v1/responses"\n')

    assert len(_violations(tmp_path, "provider")) == 1


def test_waived_file_gaining_reference_exceeds_baseline(tmp_path):
    (tmp_path / "invoker.py").write_text(
        '# https://api.anthropic.com outage note\npost("https://api.anthropic.com/v1/messages")\n'
    )
    baseline = {("invoker.py", "provider"): (1, "comment reference only")}

    violations = check.scan(tmp_path, baseline=baseline, classes=("provider",))

    assert len(violations) == 2
    assert {v.kind for v in violations} == {"over_baseline"}


def test_waived_file_losing_reference_is_stale(tmp_path):
    (tmp_path / "invoker.py").write_text("# outage note remains, no provider host\n")
    baseline = {("invoker.py", "provider"): (1, "comment reference only")}

    violations = check.scan(tmp_path, baseline=baseline, classes=("provider",))

    assert [(v.path, v.lineno, v.cls, v.kind) for v in violations] == [
        ("invoker.py", 0, "provider", "stale_baseline")
    ]


def test_flags_provider_host_in_shell_file(tmp_path):
    (tmp_path / "probe.sh").write_text("curl https://api.openai.com/v1/models\n")

    assert len(_violations(tmp_path, "provider")) == 1


def test_ignores_javascript_under_node_modules(tmp_path):
    package = tmp_path / "node_modules" / "sample"
    package.mkdir(parents=True)
    (package / "index.js").write_text('const url = "https://api.openai.com/v1";\n')

    assert check.scan(tmp_path, baseline={}) == []


def test_scan_limits_files_and_stale_baselines_to_selected_subdirs(tmp_path):
    provider_host = ".".join(("api", "openai", "com"))
    provider_url = f"https://{provider_host}/v1"
    venv_file = tmp_path / ".venv/lib/x.py"
    venv_file.parent.mkdir(parents=True)
    venv_file.write_text(f'URL = "{provider_url}"\n')
    source_file = tmp_path / "src/y.py"
    source_file.parent.mkdir()
    source_file.write_text(f'URL = "{provider_url}"\n')
    baseline = {(".venv/lib/x.py", "provider"): (1, "outside selected roots")}

    violations = check.scan(
        tmp_path,
        baseline=baseline,
        classes=("provider",),
        subdirs=("src",),
    )

    assert [(v.path, v.cls, v.kind) for v in violations] == [("src/y.py", "provider", "unlisted")]


def test_main_prints_guidance_only_for_violating_classes(monkeypatch, capsys):
    monkeypatch.setattr(
        check,
        "scan",
        lambda _root, **_kwargs: [
            check.Violation("src/new.py", 7, "discord", "egress", "unlisted")
        ],
    )

    assert check.main() == 1
    output = capsys.readouterr().out
    assert "External-world egress" in output
    assert "Provider endpoint" not in output


def test_provider_patterns_cover_shipped_provider_inventories():
    inventory = _shipped_inventory()

    assert inventory <= set(check.PROVIDER_HOSTS)


def test_provider_hosts_are_reconciled_with_inventories_and_call_sites():
    inventory = _shipped_inventory()

    # Rule YAML and _DEGRADED_GATED drift are hook-surface and tracked in #2227.
    assert set(check.PROVIDER_HOSTS) <= inventory | set(check.CALL_SITE_ONLY_HOSTS)
    source_config_files = [
        path
        for root in (_REPO_ROOT / "src", _REPO_ROOT / "config")
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".py", ".yaml", ".yml"}
    ]
    for host, reason in check.CALL_SITE_ONLY_HOSTS.items():
        assert reason.strip()
        found = False
        for path in source_config_files:
            content = path.read_text(encoding="utf-8")
            if host in content:
                found = True
                break
            # The alternate MiniMax domain is declared by the hook rule's
            # optional-label regex rather than as a concrete source URL.
            if host == "api.minimax.com" and r"api\.minimaxi?\.com" in content:
                found = True
                break
        assert found, f"call-site-only host not found in src/ or config/: {host}"
