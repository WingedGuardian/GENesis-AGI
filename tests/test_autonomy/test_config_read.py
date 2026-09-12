"""Tests for the fail-safe autonomy default-level reader (``config_read``).

The reader feeds the dashboard ``setup-status`` readiness enrichment, which must never
500 first-run — so EVERY failure mode degrades to the conservative L1 floor rather than
raising.
"""

from __future__ import annotations

from genesis.autonomy.config_read import read_autonomy_default_level


def test_reads_shipped_default_per_category(tmp_path):
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text("defaults:\n  direct_session: 2\n  outreach: 1\n")
    assert read_autonomy_default_level("direct_session", path=cfg) == 2
    assert read_autonomy_default_level("outreach", path=cfg) == 1


def test_default_category_is_direct_session(tmp_path):
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text("defaults:\n  direct_session: 4\n  outreach: 1\n")
    assert read_autonomy_default_level(path=cfg) == 4


def test_missing_file_is_level_1(tmp_path):
    assert read_autonomy_default_level(path=tmp_path / "nope.yaml") == 1


def test_malformed_yaml_is_level_1(tmp_path):
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text("defaults: [unbalanced, flow, seq\n")  # unclosed -> YAMLError
    assert read_autonomy_default_level(path=cfg) == 1


def test_absent_defaults_block_is_level_1(tmp_path):
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text("ceilings:\n  direct_session: 7\n")  # no `defaults:`
    assert read_autonomy_default_level("direct_session", path=cfg) == 1


def test_absent_category_is_level_1(tmp_path):
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text("defaults:\n  direct_session: 3\n")
    assert read_autonomy_default_level("sub_agent", path=cfg) == 1


def test_non_int_value_is_level_1(tmp_path):
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text("defaults:\n  direct_session: high\n")
    assert read_autonomy_default_level("direct_session", path=cfg) == 1


def test_infinity_value_is_level_1(tmp_path):
    # YAML `.inf` parses to float('inf'); int(inf) raises OverflowError (NOT ValueError/
    # TypeError) — must still fail-safe to L1, honoring the never-raise contract.
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text("defaults:\n  direct_session: .inf\n")
    assert read_autonomy_default_level("direct_session", path=cfg) == 1


def test_non_constructible_level_is_1(tmp_path):
    # Only L1–L4 are constructible AutonomyLevel members (L5–L7 deferred to V5); the
    # yaml `ceilings` cap ACTIONS, not the enum. A default outside the enum (0, negative,
    # or 5–7) would ValueError at AutonomyStateMachine.load_or_create_defaults, so the
    # display read must fall back to L1 rather than advertise a non-constructible level.
    for bad in ("0", "-3", "5", "6", "7", "99"):
        cfg = tmp_path / f"autonomy_{bad}.yaml"
        cfg.write_text(f"defaults:\n  direct_session: {bad}\n")
        assert read_autonomy_default_level("direct_session", path=cfg) == 1, bad


def test_all_constructible_levels_pass_through(tmp_path):
    # Every valid enum level (L1–L4) is returned as-is.
    for good in (1, 2, 3, 4):
        cfg = tmp_path / f"autonomy_{good}.yaml"
        cfg.write_text(f"defaults:\n  direct_session: {good}\n")
        assert read_autonomy_default_level("direct_session", path=cfg) == good


def test_empty_file_is_level_1(tmp_path):
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text("")  # yaml.safe_load -> None
    assert read_autonomy_default_level(path=cfg) == 1


def test_non_mapping_root_is_level_1(tmp_path):
    # A top-level scalar or list parses to a truthy non-dict; `.get` would
    # AttributeError without the isinstance guard. Must still fail-safe, not raise.
    scalar = tmp_path / "scalar.yaml"
    scalar.write_text("just a bare string\n")
    assert read_autonomy_default_level(path=scalar) == 1
    seq = tmp_path / "seq.yaml"
    seq.write_text("- a\n- b\n")
    assert read_autonomy_default_level(path=seq) == 1


def test_non_mapping_defaults_is_level_1(tmp_path):
    # `defaults:` present but not a mapping (a list) → fail-safe, not raise.
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text("defaults:\n  - direct_session\n  - outreach\n")
    assert read_autonomy_default_level(path=cfg) == 1


def test_real_shipped_config_is_conservative_l1():
    # The actual config/autonomy.yaml ships every category at L1 ("conservative start").
    # Pins the reader against the real file, not just synthetic fixtures.
    assert read_autonomy_default_level("direct_session") == 1
