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


def test_sub_one_level_floors_to_1(tmp_path):
    # A 0/negative default is a nonsensical autonomy level → clamp UP to the L1 floor
    # (Codex P2, corrected: only < 1 is invalid).
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text("defaults:\n  direct_session: 0\n  outreach: -3\n")
    assert read_autonomy_default_level("direct_session", path=cfg) == 1
    assert read_autonomy_default_level("outreach", path=cfg) == 1


def test_high_but_valid_level_passes_through(tmp_path):
    # direct_session's ceiling is 7 ("effectively uncapped"), so a legitimately-high
    # default must NOT be clamped down — the generic reader floors, never caps.
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text("defaults:\n  direct_session: 6\n")
    assert read_autonomy_default_level("direct_session", path=cfg) == 6


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
