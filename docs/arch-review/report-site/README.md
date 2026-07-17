# Scout Architecture Review — interactive report

A single, self-contained HTML view of the 2026-06-12 multi-fleet architecture review.
Open `scout-arch-review-2026-06-12.html` in any browser — no server, no network, no
build step required to *view* it. Share the file directly or drop it behind any static
host / private server.

## What's in it

| Tab | Source |
|---|---|
| **Overview** | Executive summary + live stat dashboard (clickable charts) + as-built architecture map (`synthesis.md` §1–2) |
| **Methodology** | How the review was run — the 5-phase pipeline, the reviewer roster, and the full methodology doc (`docs/arch-review-methodology.md`) |
| **Findings explorer** | All 148 adjudicated findings, interactive: full-text search; filter by status / impact / complexity / verdict; group by subsystem / status / impact; sort by severity / replication / id. Each card expands to the claim, the entry-point→consequence evidence chain, the files, every reviewer who independently found it, and the adversarial-verification verdict + per-verifier evidence. Built from `findings/batch-*.json`. |
| **By subsystem / Patterns / Recommendations / What's actually fine / Coverage** | The rest of `synthesis.md` (§3–7), rendered. Every `NN#i` reference is a clickable link into the explorer. Long sections get an auto-generated "On this page" index. |
| **Key & glossary** | Definitions of every scoring term — `BROKEN-NOW` / `LATENT` / `DEBT` / `COSMETIC` (status), the five impact classes, the verification verdicts, the complexity axis, and `rK` replication — each with its live finding count. The same definitions appear as hover tooltips on every badge and in a collapsible legend inside the explorer. |

Other niceties: light/dark toggle (persisted), URL hash routing (`#findings`,
`#methodology`, …), deep-linkable findings via the in-page cross-references, and
`/` to jump to search.

## Design / fonts

A clean, neutral technical document: **IBM Plex Sans** for body and headings,
**IBM Plex Mono** for code/ids/data, on a light grey/white palette with color reserved
for the semantic badges. Fonts load from Google Fonts; with no network they fall back to
the system sans/mono stack and the layout is unchanged. The findings data, narrative,
CSS and JS are all inlined — the only external request is the font stylesheet.

## Rebuilding

The HTML is generated, not hand-edited. To regenerate (e.g. after a re-run of the
review, or to tweak the layout):

```bash
uv run docs/arch-review/report-site/build.py
```

- `build.py` — reads the review artifacts under `docs/arch-review/2026-06-12/`
  (findings, coverage, `synthesis.md`) plus `docs/arch-review-methodology.md`,
  renders the markdown, links the `NN#i` references, and injects everything into the
  template. Only dependency is `markdown` (pulled in automatically by `uv`).
- `template.html` — the shell: all CSS + JS inline, with `{{PLACEHOLDER}}` tokens.
  Edit styling/behaviour here, then re-run `build.py`.

To point it at a future review run, change `DATE` (and `HEAD`) at the top of `build.py`.
The methodology is repeatable quarterly; re-running the build against a new dated
directory produces the matching report.

## Finding-id convention

`NN#i` = `findings/batch-NN.json`, array index `i` (e.g. `00#0` is the refresh
data-loss finding). `rK` on a card = the number of independent reviewers who found it.
