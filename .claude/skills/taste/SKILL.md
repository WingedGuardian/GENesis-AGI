---
name: taste
description: >
  Use before generating, editing, or reviewing any user interface — a
  dashboard panel, a landing page, an email, a slide, an app screen. Sets
  three deliberate design dials (variance, motion, density) BEFORE generation
  so the output has a point of view instead of defaulting to the timid,
  uniform look that reads as "AI made this". Applies to Genesis's OWN
  dashboard UI, not only things built for others.
keywords: [ui, taste, layout, visual, dashboard, aesthetic, slop, variance, motion, density, spacing, typography, frontend, css]
consumer: cc_foreground
---

## The Rule

**Set the three dials on purpose before you generate a single element.**

AI-generated UI has a signature: every dial parked in the timid middle.
Uniform cards on a uniform grid, one gratuitous fade on everything, medium
whitespace everywhere, a purple-to-blue gradient, everything centered. It is
not ugly — it is *characterless*. The fix is not "more polish". It is a
deliberate choice on each dial, made before generation, matched to the
product and its audience.

State the three values out loud (in a comment, a plan line, or your reasoning)
before writing markup or styles. If you cannot say why each dial sits where it
does, you are about to produce slop.

## The Three Dials

### 1. Variance — sameness ↔ contrast
How much elements differ from one another in size, weight, shape, and rhythm.

- **Low** (calm, systematic): dashboards, data tables, settings, docs. The job
  is scanning and trust, so repetition and a strict grid help.
- **High** (expressive, hierarchical): landing pages, hero sections, pitch
  decks. One element must dominate; the rest recede. Deliberate asymmetry.
- **Slop tell:** every card the same size and weight, so nothing leads the eye.
  Fix by making the most important thing visibly bigger/bolder/first, and
  letting secondary things be genuinely smaller.

### 2. Motion — stillness ↔ animation
How much moves, and whether motion carries meaning.

- **Low** (still, fast): productivity tools, dense dashboards, anything used
  many times a day. Motion is friction here; prefer instant state changes.
- **High** (guided, narrative): onboarding, marketing, a single celebratory
  moment. Motion should *explain* (where a thing came from, what changed), not
  decorate.
- **Slop tell:** a blanket fade/slide on every element, or hover effects that
  do nothing. Every animation must answer "what does this help the user
  understand?" If nothing, cut it.

### 3. Density — airy ↔ packed
How much information and how little whitespace per screen.

- **Low** (airy, focused): landing pages, empty states, a single primary
  action. Whitespace is the message.
- **High** (packed, professional): trading/ops dashboards, tables, power-user
  tools where more-on-screen beats more-scrolling. Density is a feature for
  experts, not a failure.
- **Slop tell:** the same medium padding on everything regardless of context —
  a data table that wastes half the screen, or a landing hero crammed edge to
  edge. Match density to how the surface is actually used.

## Anti-Slop Checklist

Before shipping any UI, scan for the generic tells and kill them:

- [ ] **No default gradient as the whole identity** (purple→blue, teal→green).
      A gradient is a spice, not the meal.
- [ ] **Not everything centered.** Left-aligned text is easier to read; reserve
      centering for genuinely short, singular elements.
- [ ] **Type scale has real contrast** (not 14/16/18px). Heading vs body should
      be obviously different; use weight, not just size.
- [ ] **Color is intentional**, not one hue applied to everything. Neutrals do
      most of the work; accent means something.
- [ ] **Spacing follows a scale** (a 4- or 8-px rhythm), not ad-hoc values.
- [ ] **Empty, loading, and error states are designed**, not afterthoughts.
- [ ] **No emoji as functional iconography** in a serious UI.
- [ ] **One thing clearly leads** each screen — a single primary action or focal
      point, not a wall of equal-weight options.

## Pre-Generation Ritual

1. Name the surface and its audience (e.g. "ops dashboard, power users, used
   hourly" vs "public landing, first-time visitors").
2. Set each dial and justify it in one clause:
   `variance: low (scanning), motion: low (frequent use), density: high (experts)`.
3. Generate against those dials. When reviewing, compare the result back to the
   stated dials — drift toward the timid middle is the failure to catch.

## For Genesis's Own Dashboard

This is not only for client work. Genesis's dashboard is an ops surface: bias
**low variance, low motion, high density** — fast to scan, still, information-
rich. When adding a panel, set the dials to match the existing surface rather
than importing a marketing aesthetic into a tool used every day.
