# Render guide — choose the format, produce the real file

Markdown is an *authoring* format, not a *delivery* format. For any external / under-the-
user's-name deliverable, **the final artifact is never a raw `.md` file.** Pick the format at
Intake from audience + purpose, using the matrix below.

## Format decision matrix (only tools verified present in this environment)

| Deliverable | Audience | Format | Tool (verified) |
|---|---|---|---|
| Job take-home / technical submission | Hiring team (email) | **PDF** | `pandoc --pdf-engine=xelatex` + a `visual_style` font (below), or `/make-pdf` for branded polish |
| Client report | External stakeholder | **PDF**, or **DOCX** if they'll edit | `pandoc` / `pandoc --reference-doc=template.docx` |
| Executive one-pager | C-suite / board | **PDF** (1 page) | `/make-pdf` (preferred for design), else `pandoc` |
| Slide deck | Present vs. edit | **PDF** (beamer) / **PPTX** (editable) | `pandoc -t beamer` / `pandoc -t pptx` |
| Data hand-off (tabular) | Analyst / client | **XLSX** | *deferred v1* — `xlsxwriter` not installed; flag and offer CSV (`pandoc -t csv` / write `.csv`) |
| Diagram | Reviewer | SVG/PNG embedded in the primary format | `/drawio-skill` |
| Internal spec / wiki / blog draft | Eng team / CMS | Markdown | (repo / CMS) — the only cases raw `.md` is allowed |

**Confirmed available:** `pandoc` 3.1.3 + `xelatex` *and* `pdflatex` (`/usr/bin`); business fonts
**Lato, Georgia, Liberation Sans/Serif, Arial, Noto Sans/Serif** (via `fc-list`); the `make-pdf`
skill (`~/.claude/skills/gstack/make-pdf/`, needs the browse daemon), `drawio-skill`,
`fpdf`/`fitz` in the venv. **NOT installed** (do not assume): marp, typst, weasyprint,
wkhtmltopdf, docxtpl, python-docx, python-pptx, xlsxwriter, openpyxl.

## The font IS a tell — pick it from `visual_style`

A bare `pandoc` PDF renders in Computer Modern (the default LaTeX serif). That font is
*instantly recognizable as a compiled-by-LaTeX academic paper.* For a business / take-home /
consulting deliverable, a human hands you something that looks like Word/Docs/Notion — a clean
sans or a business serif — **not** Computer Modern. So the font is itself a document-level tell
when the audience isn't academic. Verified 2026-06-17 by rendering a real packet three ways.

**Choose the body font from the spec's `visual_style` (set at Gate 1):**

| `visual_style` | Font | `pandoc` flag |
|---|---|---|
| *(unspecified)* / `modern` / business / tech | **Lato** (modern sans — default) | `-V mainfont="Lato"` |
| `formal` / corporate / executive | **Georgia** (business serif) | `-V mainfont="Georgia"` |
| `academic` / research / paper | Computer Modern (LaTeX default) | *(omit `mainfont`)* |

Lato is the default because most of what this skill produces is business/tech, where Computer
Modern reads wrong. `visual_style` always wins over the default.

**Glyph caveat:** Lato/Georgia don't include decorative symbols (e.g. `✓`, emoji) — xelatex emits
"Missing character" and drops them. Decorative glyphs are themselves an effort-artifact tell
(see `structure-altitude.md`), so the anti-slop pass should have removed them already. If genuine
symbols/math are required, either keep `academic` (Computer Modern math) or render with a
wide-coverage font (`-V mainfont="Noto Serif"` / `"DejaVu Serif"`).

## Render commands

Write the shaped+voiced draft to `draft_path` (a `.md`), then render:

```bash
# PDF (default for documents) — xelatex is the default engine. pdflatex FAILS on common
# Unicode (− × ÷ → curly quotes) that real technical/data deliverables contain. Both installed.
# Default look (business/tech): Lato. Swap mainfont per the visual_style table above.
pandoc "$DRAFT" --pdf-engine=xelatex -V mainfont="Lato" -V geometry:margin=1in -o "$OUT.pdf"
#   formal   ->  -V mainfont="Georgia"
#   academic ->  (omit -V mainfont, gets Computer Modern)
# For branded/designed output use /make-pdf (Skill tool; input: the markdown draft).

# DOCX (editable) — inherits styling from a reference doc if provided
pandoc "$DRAFT" -o "$OUT.docx"                          # plain
pandoc "$DRAFT" --reference-doc=template.docx -o "$OUT.docx"   # styled

# Deck
pandoc "$DRAFT" -t pptx -o "$OUT.pptx"                  # editable, basic
pandoc "$DRAFT" -t beamer --pdf-engine=xelatex -o "$OUT.pdf"  # present
```

**Quality check (do not skip):** open the rendered file and look. If the body is Computer Modern
on a non-academic deliverable, you forgot the `mainfont` — re-render. If it still reads as
templated, escalate the `visual_style`: *designed* → `/make-pdf` or `/design-html`→PDF.
`visual_style` (how it looks) and `authenticity_target` (human-made vs AI-assisted-OK) are
separate Gate-1 calls. See `structure-altitude.md` for the effort-artifact tells (elaborate
tables, dense formatting) that read as AI *only* when the target is human-made. Match what the
user asked for; do not impose a one-size "looks human" default.

## After rendering — update the spec

Set in `~/.genesis/sessions/$SID/deliverable.json`:
- `rendered_path` → absolute path of the produced file
- `audit_trail.render` → `{"ran": true, "tool": "pandoc|make-pdf|...", "cmd": "<exact command>"}`
- `status` → **`"rendered_unverified"`** (this is the state the Stop-hook gate watches; it now
  blocks session-end until Gate 2 records a PASS or the user cancels)

Then proceed to Verify (`references/qa-protocol.md`).
