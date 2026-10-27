"""Explicit Pro–Flash population; every plan/approval below is synthetic."""
import copy
from types import SimpleNamespace

import pytest

from genesis.eval.qualification import __main__ as cli
from genesis.eval.qualification import execution, preflight
from genesis.eval.qualification.evidence import Incomplete, digest
from tests.test_eval.test_qualification_operating import population_plan

PRO, FLASH = execution.PRO_COMPARISON


@pytest.fixture
def plan(tmp_path):
    return population_plan(tmp_path, execution.PRO_COMPARISON)


def test_full_pro_pair_binds_distinct_models_and_every_coordinate(plan):
    before = copy.deepcopy(plan.value)
    manifests = execution.validate(plan.value, plan.consent, plan.checked)
    assert tuple(manifests) == (PRO, FLASH)
    assert [plan.checked["models"][a] for a in manifests] == [
        "deepseek/deepseek-v4-pro", "deepseek/deepseek-v4.1-flash",
    ]
    assert all(len(m["attempts"]) == 2250 for m in manifests.values())
    assert all(m["binding"]["operating_plan_hash"] == digest(before)
               for m in manifests.values())
    assert plan.value == before


def test_input_order_does_not_change_serial_schedule():
    assert execution.selected_aliases({FLASH: {}, PRO: {}}) == (PRO, FLASH)
    assert execution.selected_aliases({FLASH: {}}) == (FLASH,)
    assert execution.selected_aliases(dict.fromkeys(reversed(execution.ALIASES))) == execution.ALIASES


@pytest.mark.parametrize("aliases", [(PRO,), (PRO, execution.ALIASES[0]),
                                    (*execution.ALIASES, PRO), (PRO, FLASH, "unknown")])
def test_extension_does_not_admit_other_populations(aliases):
    with pytest.raises(Incomplete):
        execution.selected_aliases(dict.fromkeys(aliases))


@pytest.mark.parametrize("change", ["scope", "old_consent", "model", "coordinate",
                                    "request", "allowance", "limit", "stop"])
def test_pro_pair_preserves_consent_schedule_and_funding_constraints(plan, change):
    if change == "scope":
        plan.consent["scope"] = "2250_requests"
    elif change == "old_consent":
        (plan.tmp / "old").mkdir()
        plan.consent["plan_hash"] = digest(population_plan(plan.tmp / "old", execution.ALIASES).value)
    elif change in ("model", "coordinate", "request"):
        first = plan.value["models"][PRO]["manifest"]["attempts"][0]
        first[{"model": "model", "coordinate": "repetition", "request": "request_hash"}[change]] = {
            "model": "xiaomi/mimo-v2.6-pro", "coordinate": 4, "request": "0" * 64,
        }[change]
    elif change == "allowance":
        plan.value["combined_allowance"] = "4.49"
    elif change == "limit":
        plan.value["expected_limit"] = "4.49"
    else:
        plan.value["stop_semantics"] = execution.SINGLE_STOP
    if change not in ("scope", "old_consent"):
        plan.consent["plan_hash"] = digest(plan.value)
    with pytest.raises(Incomplete):
        execution.validate(plan.value, plan.consent, plan.checked)


@pytest.mark.parametrize("reverse", [False, True])
async def test_cli_selects_exact_pro_population_before_acquisition(tmp_path, monkeypatch, reverse):
    first, second = (FLASH, PRO) if reverse else (PRO, FLASH)
    selected = SimpleNamespace(command="run", alias=first, against=second,
                               campaign=tmp_path / first, against_campaign=tmp_path / second,
                               temp_root=tmp_path / "scratch", operating_plan=tmp_path / "plan",
                               spending_approval=tmp_path / "consent")
    events = []

    async def check(*_, candidates, **kwargs):
        events.append(tuple(candidates))
        return {"models": dict.fromkeys(candidates, "synthetic")}

    async def execute(_, checked, paths, *rest, **kwargs):
        assert set(paths) == set(checked["models"]) == {PRO, FLASH}
        assert paths[first] == selected.campaign and paths[second] == selected.against_campaign
        events.append("execute")
        return {"status": "pass"}

    monkeypatch.setattr(preflight, "check", check)
    monkeypatch.setattr(execution, "execute", execute)
    options = dict.fromkeys(("reference_policy", "current_reference_policy",
                            "reference_approval", "source_files"))
    assert await cli.admit(selected, [], dict.fromkeys((first, second), {}), options) == {"status": "pass"}
    assert events == [(PRO, FLASH), "execute"]


async def test_pro_pair_without_spending_approval_never_acquires(tmp_path, monkeypatch):
    selected = SimpleNamespace(command="run", alias=PRO, against=FLASH,
                               campaign=tmp_path / "pro", against_campaign=tmp_path / "flash",
                               temp_root=tmp_path / "scratch", operating_plan=tmp_path / "plan",
                               spending_approval=None)

    async def check(*_, **kwargs):
        return {"models": dict.fromkeys((PRO, FLASH), "synthetic")}

    async def forbidden(*_, **kwargs):
        pytest.fail("missing consent reached acquisition")

    monkeypatch.setattr(preflight, "check", check)
    monkeypatch.setattr(execution, "execute", forbidden)
    with pytest.raises(Incomplete, match="financial_eligibility_unavailable"):
        await cli.admit(selected, [], dict.fromkeys((PRO, FLASH), {}),
                        dict.fromkeys(("reference_policy", "current_reference_policy",
                                       "reference_approval", "source_files")))
