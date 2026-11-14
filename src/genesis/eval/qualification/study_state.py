"""Read the complete bounded-study owner and its physical children together."""

import copy
from contextlib import ExitStack, suppress
from pathlib import Path

from . import lineage, preflight
from .evidence import Campaign, Incomplete, canonical, digest, load_json


def child_manifest(plan, alias, logical, ordinal, *, _hashes=None):
    if type(ordinal) is not int or ordinal not in (0, 1):
        raise Incomplete("study_child_ordinal_invalid")
    model = plan["models"][alias]["manifest"]
    spec = next(r for r in model["attempts"] if r["id"] == logical)
    plan_hash, admission_hash = _hashes or (digest(plan), digest(model["binding"]["admission"]))
    owner = {"plan_hash": plan_hash, "alias": alias, "logical": logical, "ordinal": ordinal,
             "admission_hash": admission_hash}
    identity = digest([owner["plan_hash"], alias, logical, ordinal])
    return {"version": 2, "budget": spec["max_charge"],
            "binding": {"study": owner, "transports": copy.deepcopy(model["binding"]["transports"])},
            "attempts": [{**copy.deepcopy(spec), "id": identity}]}


class Root:
    """One shared descriptor/plan per owner, including unsafe sibling liabilities."""

    def __init__(self, closure):
        self.stack = ExitStack()
        try:
            if not isinstance(closure, dict) or set(closure) != {
                "directory", "journal_sha256", "terminal_sha256"
            }:
                raise Incomplete("study_closure_invalid")
            self.closure = copy.deepcopy(closure)
            self.directory = Path(closure["directory"]).absolute()
            self.journal = self.stack.enter_context(lineage.Frozen(
                self.directory / "answers.jsonl", closure["journal_sha256"]))
            snapshot = Campaign.read(self.directory)
            if snapshot.torn_tail or not snapshot.lines or snapshot.lines[0]["kind"] != "manifest":
                raise Incomplete("study_root_incomplete")
            manifest = snapshot.lines[0]["manifest"]
            if (set(manifest) != {"version", "kind", "plan", "input_hashes"}
                    or type(manifest["version"]) is not int or manifest["version"] != 1
                    or manifest["kind"] != "bounded_study"
                    or any(e["kind"] != "failure" for e in snapshot.lines[1:])):
                raise Incomplete("study_root_invalid")
            self.plan, self.hash = manifest["plan"], digest(manifest["plan"])
            # A reader's physical location is explicit in its hash-bound closure;
            # only the live Controller uses plan.study_root for creation.
            self.model_hashes = {a: digest(m["manifest"]["binding"]["admission"])
                                 for a, m in self.plan["models"].items()}
            self.terminal, self.terminal_input = None, None
            self.terminal_state, candidate = "absent", None
            if closure["terminal_sha256"] is not None:
                self.terminal_input = self.stack.enter_context(lineage.Frozen(
                    self.directory / "terminal.json", closure["terminal_sha256"]))
                self.terminal_state = "invalid"
                with suppress(ValueError, UnicodeError):
                    candidate = load_json(self.terminal_input.raw)
            elif (self.directory / "terminal.json").exists():
                raise Incomplete("study_terminal_not_bound")
            self.child_directory = self.directory / "attempts"
            from .evidence import check_directory

            check_directory(self.child_directory)
            self.names = {p.name for p in self.child_directory.iterdir()}
            if any(not lineage.is_hash(name) for name in self.names):
                raise Incomplete("study_child_census_invalid")
            self.children = {}
            if candidate is not None:
                try:
                    self.children = self._terminal_children(candidate)
                except (ValueError, KeyError, TypeError):
                    self.children = {}
                else:
                    if set(self.children) != self.names:
                        raise Incomplete("study_sibling_census_changed")
                    self.terminal, self.terminal_state = candidate, "verified"
            self._references = None
            self.check(final=True)
        except BaseException:
            self.close()
            raise

    def close(self):
        self.stack.close()

    def _terminal_children(self, terminal):
        if (not isinstance(terminal, dict) or set(terminal) != {
            "version", "plan_hash", "root_journal_sha256", "children", "writers_closed",
            "admission_current", "status", "error"
        } or type(terminal["version"]) is not int or terminal["version"] != 1
                or terminal["plan_hash"] != self.hash
                or terminal["root_journal_sha256"] != self.journal.hash
                or type(terminal["writers_closed"]) is not bool
                or type(terminal["admission_current"]) is not bool
                or terminal["status"] not in ("pass", "incomplete")
                or not isinstance(terminal["children"], list)):
            raise Incomplete("study_terminal_invalid")
        children = {}
        for child in terminal["children"]:
            if (not isinstance(child, dict) or set(child) != {
                "id", "journal_sha256", "manifest_hash", "safe"
            } or child["id"] in children or type(child["safe"]) is not bool
                    or any(not lineage.is_hash(child[k]) for k in ("id", "journal_sha256", "manifest_hash"))):
                raise Incomplete("study_child_census_invalid")
            children[child["id"]] = child
        return children

    def check(self, *, final=False):
        self.journal.check(final=final)
        if self.terminal_input:
            self.terminal_input.check(final=final)
        elif (self.directory / "terminal.json").exists():
            raise Incomplete("study_terminal_changed")
        if {p.name for p in self.child_directory.iterdir()} != self.names:
            raise Incomplete("study_sibling_census_changed")

    def references(self):
        if self._references is not None:
            return copy.deepcopy(self._references)
        result = []
        for name in sorted(self.names):
            directory = self.child_directory / name
            child = self.children.get(name)
            if child is None:
                snapshot = Campaign.read(directory)
                if snapshot.torn_tail or not snapshot.lines:
                    raise Incomplete("study_child_incomplete")
                child = {"journal_sha256": preflight.file_hash(directory / "answers.jsonl"),
                         "manifest_hash": digest(snapshot.lines[0]["manifest"])}
            result.append({"directory": str(directory), "journal_sha256": child["journal_sha256"],
                           "manifest_hash": child["manifest_hash"], "closure": self.closure})
        self._references = result
        return copy.deepcopy(result)


def read_root(directory):
    directory = Path(directory)
    terminal = directory / "terminal.json"
    return Root({"directory": str(directory), "journal_sha256": preflight.file_hash(directory / "answers.jsonl"),
                 "terminal_sha256": preflight.file_hash(terminal) if terminal.exists() else None})


class Owners:
    """Flattened ancestry must include every sibling, even one ineligible for reuse."""

    def __init__(self, references, expenses):
        self.references, self.expenses, self.roots = references, expenses, {}
        self.reference_set = {canonical(r) for r in references}
        self.expense_set = {canonical(e) for e in expenses}

    def close(self):
        for root in self.roots.values():
            root.close()

    def resolve(self, history, closure):
        key = canonical(closure)
        if key not in self.roots:
            root = Root(closure)
            self.roots[key] = root
            required = [*root.plan["histories"], *root.references()]
            if any(canonical(ref) not in self.reference_set for ref in required):
                raise Incomplete("study_ancestor_or_sibling_omitted")
            if any(canonical(e) not in self.expense_set for e in root.plan["expense_evidence"]):
                raise Incomplete("study_ancestor_expense_omitted")
        root = self.roots[key]
        marker = history._state.manifest["binding"]["study"]
        if not isinstance(marker, dict) or set(marker) != {
            "plan_hash", "alias", "logical", "ordinal", "admission_hash"
        } or marker["plan_hash"] != root.hash:
            raise Incomplete("study_child_owner_changed")
        expected = child_manifest(root.plan, marker["alias"], marker["logical"], marker["ordinal"],
                                  _hashes=(root.hash, root.model_hashes[marker["alias"]]))
        if canonical(expected) != canonical(history._state.manifest):
            raise Incomplete("study_child_manifest_changed")
        identity = expected["attempts"][0]["id"]
        if history.directory != root.child_directory / identity:
            raise Incomplete("study_child_path_changed")
        semantic = root.plan["models"][marker["alias"]]["manifest"]
        history.semantic_manifest = semantic
        child = root.children.get(identity)
        history.closure_safe = bool(root.terminal and root.terminal["writers_closed"]
                                    and root.terminal["admission_current"] and child and child["safe"])
        history.owner = root

    def check(self, *, final=False):
        for root in self.roots.values():
            root.check(final=final)
