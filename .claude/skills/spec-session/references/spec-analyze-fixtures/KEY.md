# Answer key for the spec-analyze fixtures

Do not give this file to a session that runs the pass. Copy the four fixture
files to a directory of their own and point the runs at the copies. This key
names each planted defect by its position and kind only, and quotes none of the
fixture's words, so an analyzer that searches the repo to check a reference
does not land on the answer here.

## What a correct run reports

`fixture-a-spec.md` is the control: the same feature with every planted defect
fixed. A correct run reports no CRITICAL finding on it. MEDIUM and LOW findings
are allowed.

`fixture-b-spec.md` carries five planted defects:

| Kind | Where | Pass | Expected id | Expected severity |
|---|---|---|---|---|
| A reference to a section and a setting that no document defines | Behaviour 7 | UND | `UND-R7` | HIGH or MEDIUM |
| One requirement stated twice with different numbers | Behaviour 4, and the Storage section | DUP | `DUP-R4` | HIGH |
| A requirement no ticket covers | Behaviour 6 | COV | `COV-R6` | any |
| An unmeasurable word on a performance requirement | Behaviour 6 | AMB | `AMB-R6` | HIGH |
| A conflict with one of the Traps in CLAUDE.md | Behaviour 5 | RULE | `RULE-R5` | CRITICAL |

The expected ids assume the numbering rules in `spec-analyze.md`, Step 1.

## The acceptance bar (issue #4521)

Three fresh sessions, each on both fixtures:
- at least four of the five planted defects found in every run on fixture B;
- the Behaviour 5 conflict CRITICAL in all three;
- no CRITICAL on fixture A.

A miss is reported, and the contract is revised before merge.
