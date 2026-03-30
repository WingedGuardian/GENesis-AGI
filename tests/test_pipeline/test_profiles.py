"""Tests for genesis.pipeline.profiles."""

from textwrap import dedent

from genesis.pipeline.profiles import ProfileLoader, ResearchProfile, SourceConfig
from genesis.pipeline.types import Tier


class TestSourceConfig:
    def test_defaults(self):
        sc = SourceConfig(name="google", type="web_search")
        assert sc.name == "google"
        assert sc.type == "web_search"
        assert sc.queries == []
        assert sc.endpoint is None
        assert sc.refresh_interval_hours == 4.0
        assert sc.params == {}


class TestResearchProfile:
    def test_defaults(self):
        p = ResearchProfile(name="test")
        assert p.enabled is True
        assert p.tier0_interval_minutes == 30
        assert p.tier1_batch_size == 50
        assert p.tier2_trigger_threshold == 10
        assert p.sources == []
        assert p.relevance_keywords == []
        assert p.exclude_keywords == []
        assert p.min_relevance == 0.3
        assert p.notify_on_tier == Tier.JUDGMENT
        assert p.store_config == {}


class TestProfileLoader:
    def test_load_all_from_directory(self, tmp_path):
        p = tmp_path / "crypto.yaml"
        p.write_text(dedent("""\
            name: crypto
            enabled: true
            relevance_keywords:
              - bitcoin
              - ethereum
            sources:
              - name: google
                type: web_search
                queries:
                  - "bitcoin price"
        """))
        loader = ProfileLoader(tmp_path)
        profiles = loader.load_all()
        assert "crypto" in profiles
        assert profiles["crypto"].relevance_keywords == ["bitcoin", "ethereum"]
        assert len(profiles["crypto"].sources) == 1
        assert profiles["crypto"].sources[0].name == "google"
        assert profiles["crypto"].sources[0].queries == ["bitcoin price"]

    def test_missing_directory_returns_empty(self, tmp_path):
        loader = ProfileLoader(tmp_path / "nonexistent")
        profiles = loader.load_all()
        assert profiles == {}

    def test_invalid_yaml_skipped(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("{{invalid yaml content [[[")
        good = tmp_path / "good.yaml"
        good.write_text(dedent("""\
            name: good_profile
            enabled: true
        """))
        loader = ProfileLoader(tmp_path)
        profiles = loader.load_all()
        assert "good_profile" in profiles
        assert len(profiles) == 1

    def test_get_returns_none_for_unknown(self, tmp_path):
        loader = ProfileLoader(tmp_path)
        loader.load_all()
        assert loader.get("nonexistent") is None

    def test_get_returns_loaded_profile(self, tmp_path):
        p = tmp_path / "test.yaml"
        p.write_text("name: test_profile\n")
        loader = ProfileLoader(tmp_path)
        loader.load_all()
        result = loader.get("test_profile")
        assert result is not None
        assert result.name == "test_profile"

    def test_list_enabled_filters_disabled(self, tmp_path):
        (tmp_path / "a.yaml").write_text("name: enabled_one\nenabled: true\n")
        (tmp_path / "b.yaml").write_text("name: disabled_one\nenabled: false\n")
        (tmp_path / "c.yaml").write_text("name: enabled_two\nenabled: true\n")
        loader = ProfileLoader(tmp_path)
        loader.load_all()
        enabled = loader.list_enabled()
        names = [p.name for p in enabled]
        assert "enabled_one" in names
        assert "enabled_two" in names
        assert "disabled_one" not in names

    def test_notify_on_tier_from_string(self, tmp_path):
        p = tmp_path / "t.yaml"
        p.write_text("name: tier_test\nnotify_on_tier: analysis\n")
        loader = ProfileLoader(tmp_path)
        loader.load_all()
        profile = loader.get("tier_test")
        assert profile is not None
        assert profile.notify_on_tier == Tier.ANALYSIS

    def test_notify_on_tier_from_int(self, tmp_path):
        p = tmp_path / "t.yaml"
        p.write_text("name: tier_int\nnotify_on_tier: 2\n")
        loader = ProfileLoader(tmp_path)
        loader.load_all()
        profile = loader.get("tier_int")
        assert profile is not None
        assert profile.notify_on_tier == Tier.ANALYSIS

    def test_yml_extension_loaded(self, tmp_path):
        p = tmp_path / "alt.yml"
        p.write_text("name: yml_profile\n")
        loader = ProfileLoader(tmp_path)
        loader.load_all()
        assert loader.get("yml_profile") is not None

    def test_yaml_takes_precedence_over_yml(self, tmp_path):
        (tmp_path / "dup.yaml").write_text("name: yaml_version\n")
        (tmp_path / "dup.yml").write_text("name: yml_version\n")
        loader = ProfileLoader(tmp_path)
        loader.load_all()
        # .yaml loaded, .yml skipped
        assert loader.get("yaml_version") is not None
        assert loader.get("yml_version") is None

    def test_source_defaults(self, tmp_path):
        p = tmp_path / "s.yaml"
        p.write_text(dedent("""\
            name: src_test
            sources:
              - name: minimal
                type: api
        """))
        loader = ProfileLoader(tmp_path)
        loader.load_all()
        profile = loader.get("src_test")
        assert profile is not None
        src = profile.sources[0]
        assert src.queries == []
        assert src.endpoint is None
        assert src.refresh_interval_hours == 4.0
        assert src.params == {}

    def test_empty_yaml_file(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("")
        loader = ProfileLoader(tmp_path)
        profiles = loader.load_all()
        # Empty YAML yields {}, should still produce a profile with stem name
        assert "empty" in profiles
