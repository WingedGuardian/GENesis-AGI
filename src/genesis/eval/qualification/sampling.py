"""Manifest-bound incumbent subsets; full references and Flash gates stay intact."""
from . import corpus
from .evidence import Incomplete, digest

PRO = "openrouter-deepseek-v4"
FLASH = "openrouter-deepseek-flash"
BASELINE = "sampled_incumbent_baseline"


def has_sample(manifest):
    """Detect marker presence before strict validation, including malformed markers."""
    return (isinstance(manifest, dict) and isinstance(manifest.get("binding"), dict)
            and "baseline_sample" in manifest["binding"])


def coordinates(manifest, checked, alias, *, cases=None):
    full = {name: list(rows) for name, rows in checked["plan"].items()}
    if "baseline_sample" not in manifest["binding"]:
        return full
    marker = manifest["binding"]["baseline_sample"]
    if (alias != PRO or not isinstance(marker, dict)
            or set(marker) != {"purpose", "corpus_hash", "case_ids", "selection_evidence"}
            or marker["purpose"] != "incumbent_baseline"
            or marker["corpus_hash"] != checked["corpus_hash"]
            or not isinstance(marker["selection_evidence"], str)
            or not marker["selection_evidence"].strip()
            or not isinstance(cases, dict) or set(cases) != set(full)
            or digest(cases) != checked["corpus_hash"]
            or not isinstance(marker["case_ids"], dict) or set(marker["case_ids"]) != set(full)):
        raise Incomplete("incumbent_sample_invalid")
    for name, ids in marker["case_ids"].items():
        if (not isinstance(ids, list) or not ids or len(ids) >= len(full[name])
                or any(not isinstance(i, str) or not i.strip() for i in ids)
                or len(set(ids)) != len(ids) or not set(ids) <= set(full[name])):
            raise Incomplete("incumbent_sample_invalid")
        rows = {row["id"]: row for row in cases[name]}
        if set(rows) != set(full[name]) or {corpus.label(name, rows[i]) for i in ids} != {False, True}:
            raise Incomplete("incumbent_sample_classes_invalid")
    return marker["case_ids"]


def campaign_cases(cases, checked, manifest, alias):
    selected = coordinates(manifest, checked, alias, cases=cases)
    return {name: [c for c in rows if c["id"] in selected[name]] for name, rows in cases.items()}


def records_complete(cases, records):
    """A served malformed grade is observed; missing/local evidence is unfinished."""
    if not cases or not isinstance(records, dict) or set(records) != set(cases):
        return False
    for name, rows in cases.items():
        reps = records[name]
        if not isinstance(reps, dict) or set(reps) != {1, 2, 3} or any(type(r) is not int for r in reps):
            return False
        for values in reps.values():
            if not isinstance(values, dict) or set(values) != {c["id"] for c in rows}:
                return False
            for row in values.values():
                if (not isinstance(row, dict) or not {"local", "error", "prediction", "agreed"} <= row.keys()
                        or row["local"] is not None or row["error"] not in (None, "model_output_invalid")
                        or type(row["agreed"]) is not bool):
                    return False
    return True


def baseline_report(result, selected, records, full):
    result.update(status="baseline_only", scope=BASELINE, qualification_status="not_assessed",
                  baseline_completion="complete" if records_complete(selected, records) else "incomplete",
                  selected_cases=sum(map(len, selected.values())), reference_population=sum(map(len, full.values())))
    result["routes"] = dict.fromkeys(result["routes"], "not_assessed")
    for contract in result["contracts"].values():
        contract["status"] = "baseline_only"
        for row in contract["repetitions"]:
            row["status"] = "baseline_only"
    result["limitations"].append("Sampled incumbent results do not establish full model qualification.")


def aggregate_status(reports, financial, *, sampled=False):
    from . import run

    marked = any(r.get("scope") == BASELINE or "baseline_completion" in r for r in reports.values())
    if not sampled and not marked:
        return {"status": "incomplete" if financial["unresolved_attempts"] else
                run.worst((r["status"] for r in reports.values()), run.OVERALL)}
    baseline, flash = reports.get(PRO, {}), reports.get(FLASH, {})
    complete = (set(reports) == {PRO, FLASH} and not financial["unresolved_attempts"]
                and baseline.get("scope") == BASELINE and baseline.get("status") == "baseline_only"
                and baseline.get("qualification_status") == "not_assessed"
                and baseline.get("baseline_completion") == "complete"
                and flash.get("status") in ("pass", "fail")
                and flash.get("scope") != BASELINE and "baseline_completion" not in flash)
    return {"status": flash["status"] if complete else "incomplete",
            "scope": "full_flash_with_sampled_pro_baseline",
            "comparison_completion": "complete" if complete else "incomplete"}


def _case_stats(name, rows, records):
    """One repeated-measure unit per selected case, within the declared group."""
    from genesis.eval.stats import compute_winrate

    from . import run

    arms = [[sum(records[a][name][rep][c["id"]]["agreed"] for rep in run.REPETITIONS) >= 2
             for c in rows] for a in (PRO, FLASH)]
    stats = compute_winrate(*arms)
    stats["exploratory_p_value"] = stats.pop("p_value")
    for key in ("significant", "recommendation"):
        stats.pop(key)
    return stats


def comparison(cases, reports, records, states, financial):
    """One execution/detached-report population and aggregate-status contract."""
    from . import run

    sampled = any(has_sample(state.manifest) for state in states.values())
    selected = cases
    if sampled:
        if (set(states) != {PRO, FLASH} or not has_sample(states[PRO].manifest)
                or has_sample(states[FLASH].manifest)):
            raise Incomplete("incumbent_sample_population_invalid")
        manifest = states[PRO].manifest
        selected = campaign_cases(cases, manifest["binding"]["admission"], manifest, PRO)
    result = {**aggregate_status(reports, financial, sampled=sampled),
              "models": reports, "financial": financial}
    aliases = [PRO, FLASH] if sampled else list(reports)
    if sampled:
        result["paired_aliases"] = aliases
    complete = not sampled or (records_complete(selected, records[PRO])
                               and records_complete(cases, records[FLASH]))
    if sampled and not complete:
        result.update(status="incomplete", comparison_completion="incomplete")
    result["paired"] = run.pair(selected, *(records[a] for a in aliases)) if complete else {}
    if sampled:
        result["per_case_comparison"] = {}
        if complete:
            for name, rows in selected.items():
                result["per_case_comparison"][name] = {
                    **_case_stats(name, rows, records),
                    "classes": {str(label).lower(): _case_stats(
                        name, [c for c in rows if corpus.label(name, c) is label], records
                    ) for label in (False, True)},
                }
        result["limitations"] = [
            "Three responses are summarized per case, not treated as independent cases.",
            "Stratified selected-case effects are not unweighted whole-corpus or production estimates.",
            "McNemar p-values are exploratory: independent cases are assumed, but source-family correlation is unverified.",
            "No significance, equivalence, full Pro qualification or promotion follows from these summaries.",
        ]
    return result
