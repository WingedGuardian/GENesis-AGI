"""entity_adjudication has an explicit TTL — no 'Unknown observation type' warning.

The entity-adjudication drain emits a per-run ``entity_adjudication`` observation
(memory/entity_adjudication.py). Before it was categorized it fell through to the
14-day default and logged a benign per-run warning; it now sits in the 7-day
operational bucket alongside guardian_diagnosis.
"""

from datetime import timedelta

from genesis.db.crud.observations import _TTL_BY_TYPE, _compute_ttl


def test_entity_adjudication_has_explicit_ttl():
    assert _TTL_BY_TYPE.get("entity_adjudication") == timedelta(days=7)


def test_compute_ttl_for_entity_adjudication_is_seven_days():
    # Before categorization this returned the 14-day default (with a warning).
    assert _compute_ttl("entity_adjudication") == timedelta(days=7)
