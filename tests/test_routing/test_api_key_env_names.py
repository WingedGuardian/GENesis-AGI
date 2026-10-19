from __future__ import annotations

from genesis.routing import litellm_delegate


def test_resolve_api_key_honors_all_names_from_api_key_env_names(monkeypatch) -> None:
    provider_type = "example"
    names = litellm_delegate.api_key_env_names(provider_type)
    assert names == ("API_KEY_EXAMPLE", "EXAMPLE_API_KEY", "EXAMPLE_API_TOKEN")

    for name in names:
        for candidate in names:
            monkeypatch.delenv(candidate, raising=False)
        monkeypatch.setenv(name, f"value-for-{name}")
        assert litellm_delegate._resolve_api_key(provider_type) == f"value-for-{name}"
