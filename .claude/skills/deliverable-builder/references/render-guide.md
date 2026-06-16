# Render guide — choose the format, produce the real file

Markdown is an *authoring* format, not a *delivery* format. For any external / under-the-
user's-name deliverable, **the final artifact is never a raw `.md` file.** Pick the format at
Intake from audience + purpose, using the matrix below.

## Format decision matrix (only tools verified present in this environment)

| Deliverable | Audience | Format | Tool (verified) |
|---|---|---|---|
| Job take-home / technical submission | Hiring team (email) | **PDF** | `pandoc --pdf-engine=pdflatex`, or `/make-pdf` for branded polish |
| Client report | External stakeholder | **PDF**, or **DOCX** if they'll edit | `pandoc` / `pandoc --reference-doc=template.docx` |
| Executive one-pager | C-suite / board | **PDF** (1 page) | `/make-pdf` (preferred for design), else `pandoc` |
| Slide deck | Present vs. edit | **PDF** (beamer) / **PPTX** (editable) | `pandoc -t beamer` / `pandoc -t pptx` |
| Data hand-off (tabular) | Analyst / client | **XLSX** | *deferred v1* — `xlsxwriter` not installed; flag and offer CSV (`pandoc -t csv` / write `.csv`) |
| Diagram | Reviewer | SVG/PNG embedded in the primary format | `/drawio-skill` |
| Internal spec / wiki / blog draft | Eng team / CMS | Markdown | (repo / CMS) — the only cases raw `.md` is allowed |

**Confirmed available:** `pandoc` 3.1.3 + `pdflatex` (`/usr/bin`), the `make-pdf` skill
(`~/.claude/skills/gstack/make-pdf/`, needs the browse daemon), `drawio-skill`, `fpdf`/`fitz`
in the venv. **NOT installed** (do not assume): marp, typst, weasyprint, wkhtmltopdf,
docxtpl, python-docx, python-pptx, xlsxwriter, openpyxl.

## Render commands

Write the shaped+voiced draft to `draft_path` (a `.md`), then render:

```bash
# PDF (default for documents) — clean, headless, math-capable
pandoc "$DRAFT" --pdf-engine=pdflatex -V geometry:margin=1in -o "$OUT.pdf"

# PDF (branded / designed one-pager) — richer typography, needs browse daemon
#   via the Skill tool:  /make-pdf   (input: the markdown draft)

# DOCX (editable) — inherits styling from a reference doc if provided
pandoc "$DRAFT" -o "$OUT.docx"                          # plain
pandoc "$DRAFT" --reference-doc=template.docx -o "$OUT.docx"   # styled

# Deck
pandoc "$DRAFT" -t pptx -o "$OUT.pptx"                  # editable, basic
pandoc "$DRAFT" -t beamer --pdf-engine=pdflatex -o "$OUT.pdf"  # present
```

**Quality check (do not skip):** open the rendered file and look. A bare
`pandoc --pdf-engine=pdflatex` PDF has a recognizable LaTeX-article look — if it reads as
templated for the audience, re-render with `/make-pdf` or a styled reference doc. The point of
this skill is a deliverable that looks *made by a person*; a default-styled PDF is its own tell.

## After rendering — update the spec

Set in `~/.genesis/sessions/$SID/deliverable.json`:
- `rendered_path` → absolute path of the produced file
- `audit_trail.render` → `{"ran": true, "tool": "pandoc|make-pdf|...", "cmd": "<exact command>"}`
- `status` → **`"rendered_unverified"`** (this is the state the Stop-hook gate watches; it now
  blocks session-end until Gate 2 records a PASS or the user cancels)

Then proceed to Verify (`references/qa-protocol.md`).
