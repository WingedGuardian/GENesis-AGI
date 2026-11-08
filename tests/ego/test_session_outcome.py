"""The ego cycle's session row records how the cycle ended (cc/final_status.py).

Output the cycle cannot parse is a failed cycle whatever the session reported;
otherwise the session's own report is the outcome.
"""

# ruff: noqa: F811 -- pytest fixtures are imported, then requested by parameter name

from __future__ import annotations

from dataclasses import replace

import pytest

from genesis.cc.final_status import FinalStatus
from genesis.cc.types import CCModel
from tests.ego.test_session import (  # noqa: F401 -- fixtures used by name
    _cc_output,
    _make_signal,
    config,
    db,
    dispatcher,
    ego_session,
    mock_compaction,
    mock_context_builder,
    mock_invoker,
    mock_proposal_workflow,
    mock_session_manager,
    prompt_file,
)


def _outcome(mock_session_manager) -> tuple:
    kwargs = mock_session_manager.complete.await_args.kwargs
    return kwargs["outcome"], kwargs["outcome_reason"]


@pytest.mark.asyncio
async def test_a_cycle_records_the_sessions_report(ego_session, mock_session_manager):
    output = replace(_cc_output(), final_status=FinalStatus("blocked", "no access"))
    await ego_session._process_cycle_output(output, CCModel.SONNET, "bg_sess_1")
    assert _outcome(mock_session_manager) == ("blocked", "no access")


@pytest.mark.asyncio
async def test_unparseable_output_is_a_failed_cycle_whatever_was_reported(
    ego_session, mock_session_manager
):
    output = replace(
        _cc_output(text="definitely not json"), final_status=FinalStatus("completed", "done")
    )
    await ego_session._process_cycle_output(output, CCModel.SONNET, "bg_sess_1")
    assert _outcome(mock_session_manager) == ("failed", "unparseable_output")


@pytest.mark.asyncio
async def test_the_cycle_asks_for_a_report(ego_session, mock_invoker):
    await ego_session.run_unified_cycle([_make_signal()])
    flags = [call.args[0].report_status for call in mock_invoker.run.await_args_list]
    # The cycle (first call) owns the row and asks; its gates own none and do not.
    assert flags[0] is True
    assert not any(flags[1:])
