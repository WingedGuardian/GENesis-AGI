"""attention_config.json parsing/compilation tests."""
import json
import re

from genesis.attention.config import AttentionConfig, StateModifiers, Thresholds, load_config


def _raw() -> dict:
    return {
        "version": "1.0.0",
        "aliases": ["genesis", "gen"],
        "domain_keywords": ["routing", "attention engine"],
        "known_entities": ["Alice"],
        "lexical_patterns": {"question": [r"\bhow do i\b", r"\?"]},
        "emotional_patterns": {"frustration": [r"\bugh\b"]},
        "suppressor_patterns": {"explicit_dismissal": [r"never mind", r"not you"]},
        "weights": {"question": 0.3, "multi_speaker": 0.4},
        "state_modifiers": {"cooldown_s": 45, "session_gap_s": 20},
        "thresholds": {"soft_perk": 0.55},
        "suppressors_enabled": ["explicit_dismissal"],
    }


def test_from_dict_parses_and_compiles_case_insensitive():
    c = AttentionConfig.from_dict(_raw())
    assert c.version == "1.0.0"
    assert c.aliases == ("genesis", "gen")
    pat = c.lexical_patterns["question"][0]
    assert isinstance(pat, re.Pattern)
    assert pat.search("HOW DO I do this")  # compiled with IGNORECASE
    assert c.weights["question"] == 0.3
    assert c.state_modifiers.cooldown_s == 45
    assert c.state_modifiers.session_gap_s == 20
    assert c.thresholds.soft_perk == 0.55


def test_defaults_applied_for_missing_sections():
    c = AttentionConfig.from_dict({"version": "x"})
    assert c.aliases == ()
    assert c.state_modifiers == StateModifiers()  # all defaults
    assert c.thresholds == Thresholds()
    assert c.lexical_patterns == {}
    assert c.suppressors_enabled == ()


def test_unknown_state_modifier_keys_ignored():
    c = AttentionConfig.from_dict({"version": "x", "state_modifiers": {"bogus": 1, "cooldown_s": 10}})
    assert c.state_modifiers.cooldown_s == 10  # known key applied, bogus dropped


def test_load_config_reads_file(tmp_path):
    p = tmp_path / "attention_config.json"
    p.write_text(json.dumps(_raw()))
    c = load_config(p)
    assert c.version == "1.0.0"
    assert c.suppressors_enabled == ("explicit_dismissal",)


def test_default_config_dict_compiles():
    from genesis.attention.config import default_config_dict

    c = AttentionConfig.from_dict(default_config_dict())
    assert c.aliases == ("genesis",)
    assert "routing" in c.domain_keywords
    assert c.thresholds.soft_perk == 0.6            # Thresholds default (empty dict -> defaults)
    assert "explicit_dismissal" in c.suppressors_enabled
    assert c.lexical_patterns["question"]           # compiled non-empty
