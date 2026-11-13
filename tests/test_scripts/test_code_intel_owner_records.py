"""Finite owner channels retain bound startup facts without journal I/O."""
import json
import runpy
import time
from copy import deepcopy
from pathlib import Path

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_owned_child import toy as toy
from tests.test_scripts.test_code_intel_owner_control import prepared
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue


def records(queue, tmp_path, toy, evidence):
    owner, calls, facts, manager, claimed = prepared(queue, tmp_path, toy, evidence)
    owner.early()
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/lib/code_intel_owner.py'))
    return namespace['OwnerRecords'](owner), owner, calls, facts


def test_partial_and_coalesced_records_preserve_exact_action_order(queue, tmp_path, toy, evidence):
    reader, owner, calls, _ = records(queue, tmp_path, toy, evidence)
    assert reader.feed(b'rea') == []
    assert reader.feed(b'dy\nfreeze\nth') == ['ready', 'freeze']
    assert reader.feed(b'aw\npressure_stop\n') == ['thaw', 'pressure_stop']
    assert not calls, 'decoding never performs a control or journal action'
    reader.eof()


@pytest.mark.parametrize('raw', [b'freeze\n', b'wall\n', b'ready\nready\n', b'ready\nunknown\n',
                                 b'\n', b'ready\x00\n', b'\xff\n', b'x' * 4097])
def test_invalid_channel_never_normalizes_to_ready(queue, tmp_path, toy, evidence, raw):
    reader, _, _, _ = records(queue, tmp_path, toy, evidence)
    with pytest.raises(ValueError):
        reader.feed(raw)


def test_partial_frame_eof_is_not_clean_channel_end(queue, tmp_path, toy, evidence):
    reader, _, _, _ = records(queue, tmp_path, toy, evidence)
    reader.feed(b'ready\nfree')
    with pytest.raises(ValueError):
        reader.eof()


@pytest.mark.parametrize('counter', ['events', 'local_events'])
def test_fresh_original_owned_oom_is_retained_without_publishing(queue, tmp_path, toy, evidence, counter):
    reader, owner, calls, facts = records(queue, tmp_path, toy, evidence)
    scope = owner.startup['scope']
    sample = owner.modules['queued'].StartupGate(owner.queue, owner.proof, owner.context,
        owner.reserved).observation(scope, dict(reservation_sha256=owner.native.digest(owner.reserved),
        scope_sha256=owner.native.digest(scope), facts=facts, observed_ns=owner.startup['observed_ns']))
    sample['facts'][counter]['oom_kill'] = 1
    assert reader.feed(b'ready\n' + owner.native.encode(sample) + b'\n') == ['ready', 'owned_oom']
    assert owner.startup['facts'][counter]['oom_kill'] == 1 and not calls


def test_foreign_scope_is_refused(queue, tmp_path, toy, evidence):
    reader, owner, _, facts = records(queue, tmp_path, toy, evidence)
    sample = dict(reservation_sha256=owner.native.digest(owner.reserved), scope_sha256='a' * 64,
                  facts=facts, observed_ns=owner.startup['observed_ns'])
    with pytest.raises(ValueError):
        reader.feed(owner.native.encode(sample) + b'\n')
    assert owner.startup['facts']['events']['oom_kill'] == 0


def test_prestartup_report_cannot_establish_owner_scope(queue, tmp_path, toy, evidence):
    reader, owner, _, _ = records(queue, tmp_path, toy, evidence)
    owner.startup = None
    with pytest.raises(ValueError):
        reader.feed(json.dumps({}).encode() + b'\n')


def test_buffered_reporter_sample_does_not_reset_newer_owner_sample(queue, tmp_path, toy, evidence):
    reader, owner, _, facts = records(queue, tmp_path, toy, evidence)
    scope = owner.startup['scope']
    older = dict(reservation_sha256=owner.native.digest(owner.reserved), scope_sha256=owner.native.digest(scope),
                 facts=deepcopy(facts), observed_ns=owner.startup['observed_ns'])
    facts['events']['high'] += 1
    owner._retain_facts(facts)
    assert reader.feed(b'ready\n' + owner.native.encode(older) + b'\n') == ['ready', 'sample']
    assert owner.startup['facts']['events']['high'] == 1


@pytest.mark.parametrize('counter', ['events', 'local_events'])
def test_late_reporter_oom_is_retained_even_after_a_newer_owner_sample(queue, tmp_path, toy, evidence, counter):
    reader, owner, _, facts = records(queue, tmp_path, toy, evidence)
    scope = owner.startup['scope']
    older = dict(reservation_sha256=owner.native.digest(owner.reserved), scope_sha256=owner.native.digest(scope),
                 facts=deepcopy(facts), observed_ns=owner.startup['observed_ns'])
    older['facts'][counter]['oom_kill'] = 1
    owner._retain_facts(facts)
    assert reader.feed(b'ready\n' + owner.native.encode(older) + b'\n') == ['ready', 'owned_oom']
    assert owner.startup['facts'][counter]['oom_kill'] == 1


def test_reporter_stream_itself_still_rejects_counter_reset(queue, tmp_path, toy, evidence):
    reader, owner, _, facts = records(queue, tmp_path, toy, evidence)
    scope = owner.startup['scope']
    facts['events']['high'] += 1
    sample = dict(reservation_sha256=owner.native.digest(owner.reserved), scope_sha256=owner.native.digest(scope),
                  facts=facts, observed_ns=time.monotonic_ns())
    reader.feed(b'ready\n' + owner.native.encode(sample) + b'\n')
    facts['events']['high'] = 0
    sample['observed_ns'] = time.monotonic_ns()
    with pytest.raises(ValueError, match='counter reset'):
        reader.feed(owner.native.encode(sample) + b'\n')


def test_exact_evidence_rank_tie_keeps_existing_actual_snapshot(queue, tmp_path, toy, evidence):
    reader, owner, _, facts = records(queue, tmp_path, toy, evidence)
    scope, original = owner.startup['scope'], deepcopy(owner.startup)
    facts['events']['high'] += 1
    sample = dict(reservation_sha256=owner.native.digest(owner.reserved), scope_sha256=owner.native.digest(scope),
                  facts=facts, observed_ns=original['observed_ns'])
    reader.feed(b'ready\n' + owner.native.encode(sample) + b'\n')
    assert owner.startup == original


@pytest.mark.parametrize('owner_counts,reporter_counts,prefer_reporter', [
    ((1, 0), (0, 1), False), ((0, 1), (1, 0), True),
    ((0, 2), (0, 1), False), ((1, 1), (1, 2), True),
])
def test_incomparable_streams_keep_one_actual_snapshot_and_existing_resource_priority(
        queue, tmp_path, toy, evidence, owner_counts, reporter_counts, prefer_reporter):
    reader, owner, calls, facts = records(queue, tmp_path, toy, evidence)
    scope = deepcopy(owner.startup['scope'])
    reported = deepcopy(facts)
    reported['events']['oom_kill'], reported['local_events']['oom_kill'] = reporter_counts
    observed_ns = owner.startup['observed_ns']
    facts['events']['oom_kill'], facts['local_events']['oom_kill'] = owner_counts
    owner._retain_facts(facts)  # Newer owner stream; the report was independently observed earlier.
    sample = dict(reservation_sha256=owner.native.digest(owner.reserved),
                  scope_sha256=owner.native.digest(scope), facts=reported, observed_ns=observed_ns)
    assert reader.feed(b'ready\n' + owner.native.encode(sample) + b'\n') == ['ready', 'owned_oom']
    assert owner.startup['facts'] == (reported if prefer_reporter else facts)
    assert owner.startup['scope'] == scope and not calls
