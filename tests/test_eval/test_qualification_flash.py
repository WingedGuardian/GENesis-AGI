"""Single-candidate admission and orchestration; all consent here is synthetic."""
import copy
from types import SimpleNamespace

import pytest

from genesis.eval.qualification import __main__ as cli
from genesis.eval.qualification import execution, preflight, run
from genesis.eval.qualification.evidence import Incomplete, digest
from tests.test_eval.test_qualification_operating import plan as operating_plan
from tests.test_eval.test_qualification_operating import receipt_row, select_flash, write_input

FLASH = execution.ALIASES[1]


@pytest.fixture
def plan(tmp_path):
    return operating_plan.__wrapped__(tmp_path)


def test_singleton_binds_exact_population_without_mutating_plan(plan):
    select_flash(plan)
    before = copy.deepcopy(plan.value)
    manifests = execution.validate(plan.value, plan.consent, plan.checked)
    assert tuple(manifests) == (FLASH,)
    assert len(manifests[FLASH]["attempts"]) == 2250
    assert manifests[FLASH]["binding"]["operating_plan_hash"] == digest(plan.value)
    assert plan.value == before


@pytest.mark.parametrize("models", [None, [], {}, {execution.ALIASES[0]: {}},
                                    {"unknown": {}}, {FLASH: {}, "unknown": {}}])
def test_unsupported_populations_refuse(models):
    with pytest.raises(Incomplete):
        execution.selected_aliases(models)


@pytest.mark.parametrize("population", ["pair", "flash"])
@pytest.mark.parametrize("change", ["scope", "stop", "hash", "admission", "empty", "extra"])
def test_population_cannot_reuse_other_scope_or_admission(plan, population, change):
    if population == "flash":
        select_flash(plan)
    if change == "scope":
        plan.consent["scope"] = "4500_requests" if population == "flash" else "2250_requests"
    elif change == "stop":
        plan.value["stop_semantics"] = execution.STOP if population == "flash" else execution.SINGLE_STOP
        plan.consent["plan_hash"] = digest(plan.value)
    elif change == "hash":
        plan.consent["plan_hash"] = "0" * 64
    elif change == "admission":
        if population == "flash":
            plan.checked["models"][execution.ALIASES[0]] = "different-model"
        else:
            plan.checked["models"].pop(execution.ALIASES[0])
    else:
        if change == "empty":
            plan.value["models"] = {}
        else:
            plan.value["models"]["unknown"] = {}
        plan.consent["plan_hash"] = digest(plan.value)
    with pytest.raises(Incomplete):
        execution.validate(plan.value, plan.consent, plan.checked)


def args(tmp_path, *, paired=False):
    return SimpleNamespace(command="run", alias=FLASH, campaign=tmp_path / "flash",
                           against=execution.ALIASES[0] if paired else None,
                           against_campaign=tmp_path / "mimo" if paired else None,
                           operating_plan=tmp_path / "plan", spending_approval=tmp_path / "consent",
                           temp_root=tmp_path / "scratch")


@pytest.mark.parametrize("paired", [False, True])
async def test_cli_selects_exact_candidates_before_orchestration(tmp_path, monkeypatch, paired):
    selected = args(tmp_path, paired=paired)
    configured = {a: {} for a in execution.selected_aliases(
        {selected.alias: None, **({selected.against: None} if paired else {})})}
    events = []

    async def check(*_, candidates, **kwargs):
        events.append(("preflight", tuple(candidates)))
        return {"models": {alias: "synthetic" for alias in candidates}}

    async def execute(_, checked, paths, *rest, **kwargs):
        events.append(("execute", tuple(paths)))
        assert set(paths) == set(checked["models"]) == set(configured)
        assert paths[selected.alias] == selected.campaign
        if paired:
            assert paths[selected.against] == selected.against_campaign
        return {"status": "pass"}

    monkeypatch.setattr(preflight, "check", check)
    monkeypatch.setattr(execution, "execute", execute)
    options = dict.fromkeys(("reference_policy", "current_reference_policy",
                            "reference_approval", "source_files"))
    assert await cli.admit(selected, [], configured, options) == {"status": "pass"}
    assert events[0] == ("preflight", tuple(configured))
    assert events[1][0] == "execute" and set(events[1][1]) == set(configured)


@pytest.mark.parametrize("invalid", ["missing_against", "missing_path", "same_alias", "same_path",
                                     "extra_config", "missing_config", "mimo_only", "unknown"])
async def test_cli_refuses_population_paths_before_preflight(tmp_path, monkeypatch, invalid):
    selected = args(tmp_path)
    configured = {FLASH: {}}
    if invalid == "missing_against":
        selected.against_campaign = tmp_path / "mimo"
    elif invalid == "missing_path":
        selected.against = execution.ALIASES[0]
    elif invalid in ("same_alias", "same_path"):
        selected = args(tmp_path, paired=True)
        configured = dict.fromkeys(execution.ALIASES, {})
        if invalid == "same_alias":
            selected.against = FLASH
        else:
            selected.against_campaign = selected.campaign
    elif invalid == "extra_config":
        configured[execution.ALIASES[0]] = {}
    elif invalid == "missing_config":
        configured = {}
    else:
        selected.alias = execution.ALIASES[0] if invalid == "mimo_only" else "unknown"
        configured = {selected.alias: {}}

    async def forbidden(*_, **kwargs):
        pytest.fail("invalid population reached admission")

    monkeypatch.setattr(preflight, "check", forbidden)
    with pytest.raises(Incomplete):
        await cli.admit(selected, [], configured, {})


async def test_execute_singleton_has_one_writer_and_no_fabricated_pair(plan, monkeypatch):
    select_flash(plan)
    (plan.tmp / ".genesis").mkdir(mode=0o700)
    monkeypatch.setattr(execution.Path, "home", lambda: plan.tmp)
    for name, value in (("plan", plan.value), ("consent", plan.consent)):
        item = write_input(plan.tmp / name, value)
        item.close()
    calls = []

    async def qualify(alias, cases, params, journal, scratch, *, operating, dispatch, **kwargs):
        assert dispatch and operating.aliases == (FLASH,)
        assert tuple(operating.journals) == (FLASH,)
        calls.append(alias)
        return {"status": "pass"}, []

    monkeypatch.setattr(run, "qualify", qualify)
    result = await execution.execute([], plan.checked, {FLASH: plan.tmp / "flash"}, plan.tmp,
                                     plan_path=plan.tmp / "plan", approval_path=plan.tmp / "consent",
                                     options={})
    assert calls == [FLASH] and "paired" not in result
    assert tuple(result["models"]) == (FLASH,)
    assert result["financial"]["observed_receipt_cost"] == "0"


@pytest.mark.parametrize("collision", [False, True])
async def test_paired_detached_report_censuses_exact_scored_snapshots(tmp_path, monkeypatch, collision):
    from genesis.eval.qualification import corpus
    from genesis.eval.qualification.accounting import currency

    selected = args(tmp_path, paired=True)
    selected.command = "report"
    selected.corpus = tmp_path / "corpus"
    selected.params = tmp_path / "params"
    selected.params.write_text('{"openrouter-mimo":{},"openrouter-deepseek-flash":{}}')
    rows = {a: receipt_row("0.10") for a in execution.ALIASES}
    for alias, row in rows.items():
        row["dispatched"] = True
        if not collision:
            for receipt in row["receipts"]:
                receipt["generation_id"] = alias
    views, scored = {}, []

    class View:
        def __init__(self, path):
            alias = FLASH if path == selected.campaign else execution.ALIASES[0]
            assert alias not in views, "snapshot was reopened after scoring"
            self.state = SimpleNamespace(attempts={"a": rows[alias]}, settled=currency("0.10"),
                                         reserved=currency("0"))
            views[alias] = self

    async def qualify(alias, cases, params, campaign, *_, **kwargs):
        assert campaign is views[alias]
        scored.append(alias)
        return {"status": "pass", "admission": "same-shared-identity"}, []

    monkeypatch.setattr(corpus, "load", lambda *_, **kwargs: {})
    monkeypatch.setattr(corpus, "coverage", lambda *_, **kwargs: [])
    monkeypatch.setattr(run, "ReadView", View)
    monkeypatch.setattr(run, "qualify", qualify)
    result = await cli.run(selected)
    assert set(scored) == set(execution.ALIASES) == set(views)
    assert result["status"] == ("incomplete" if collision else "pass")
    assert result["financial"]["observed_receipt_cost"] == ("0" if collision else "0.20")
    assert result["financial"]["unresolved_attempts"] == (2 if collision else 0)
