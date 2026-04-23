# ADR 008 — Quarantine collection in S1 instead of all-or-nothing import

**Status**: Accepted
**Date**: 2026-04-23
**Deciders**: project owner
**Supersedes**: —
**Superseded by**: —

---

## Context

Stage 04 of S1 runs a cascade (04a-d) that tries progressively more
expensive sources to recover bibliographic metadata for an orphan PDF
attachment:

- 04a — identifier regex on pages 1-3, retry Route A via OpenAlex.
- 04b — fuzzy title match against OpenAlex.
- 04c — fuzzy title match against Semantic Scholar.
- 04d — `gpt-4o-mini` extraction from the first two pages of the PDF.

**A non-trivial fraction of the corpus will fall through all four.**
Empirical upper bounds from plan_01 §3 Etapa 04:

- **<10%** of the corpus for **anglo-dominant** corpora (journal
  articles indexed by CrossRef / PMC / arXiv).
- **<25%** for **LATAM-heavy** corpora (CEPAL Review, Desarrollo
  Económico, Estudios Económicos, BID / CAF / BCRA working papers,
  SciELO / RedALyC journals) until the v1.1 extension lands with
  LATAM-specific metadata sources (tracked in issue #46).

The items that fall through are not malformed — they are real papers
whose metadata happens to be unreachable by our cascade. Common causes:
pre-2010 books without DOIs, LATAM journals not indexed by OpenAlex,
reports published outside the academic ecosystem, scans where the
title page is missing or illegible.

The question is **what to do with those items**:

## Decision

**Keep them in Zotero, but in a separate collection called
`Quarantine`, tagged `needs-manual-review`.**

Implementation is Stage 04e:

- **Idempotent collection creation.** `_ensure_quarantine_collection`
  looks up the collection by name (`Quarantine`, configurable in a
  future revision) and creates it only if absent.
- **Tagging.** `add_tags(item, [needs-manual-review])` on the orphan
  attachment. The tag is how the user surfaces them in Zotero Search
  or exports them via Better BibTeX.
- **Linking to the collection.** `addto_collection(collection, item)`.
- **Paper trail.** A dedicated `reports/quarantine_report_<ts>.csv`
  with columns `(sha256, source_path, text_snippet, reason)` so the
  user can decide in a single pass whether the item deserves manual
  research or should be left as-is.
- **State flag.** `Item.in_quarantine=True`, `stage_completed=4`.
  Stage 05 (tagging) skips quarantined items because their metadata
  is insufficient for LLM taxonomy matching.

The Quarantine collection is named in the glossary (`plan_glossary.md`)
and in the README so the user sees it up-front as an expected outcome,
not a failure mode.

## Consequences

### Positive

- **Resolves the completeness vs. quality tension.** The "all-or-
  nothing" alternative either loses low-quality items (completeness
  loss) or pollutes the main library with stubs (quality loss).
  Quarantine gives the user both: completeness (every PDF is in Zotero
  somewhere) and quality (the main library stays clean).
- **Matches the 1-pass triage the user already does.** The
  `quarantine_report.csv` is explicitly shaped as a triage list —
  path + text snippet + reason — so the user can pick the 5% worth
  chasing down manually and leave the rest alone.
- **Composable with Stage 06 validation.** Stage 06 reports the
  quarantine ratio against the thresholds in plan_01 §3 Etapa 04
  criterion (<10% anglo / <25% LATAM), and nudges the user toward
  issue #46 if the ratio exceeds the LATAM threshold.
- **No data loss.** The PDF is already attached in Zotero (Stage 03
  Route C orphan); quarantine doesn't move or re-upload it, just
  tags + collection-links.
- **Scoped negative signal.** A "quarantined" item is not the same as
  a "failed" item — the user can re-run `zotai s1 enrich --substage
  all` after adding a new metadata source (e.g. once #46 lands
  SciELO support) and items move out of quarantine automatically.

### Negative / Costs assumed

- **User cognitive load: a third collection name to understand.** The
  library now has three reserved names: main (root), `Quarantine`,
  `Inbox S2`. README + `plan_glossary.md` mitigate by documenting
  them once, with an explicit sentence that tells the user: "You
  don't manage these; the system creates and manages them on
  demand."
- **Tag name coordination.** `needs-manual-review` is hardcoded in
  `stage_04_enrich.py`. It's not under `config/taxonomy.yaml`
  because the researcher's taxonomy and the system's
  housekeeping tags are orthogonal (the former is their scientific
  vocabulary; the latter is pipeline metadata). Renaming requires a
  code change.
- **Re-running `--substage 04e` is safe but not idempotent at the
  Zotero level.** `add_tags` and `addto_collection` are both
  idempotent in effect (Zotero silently dedup-coerces); the stage
  also checks `Item.in_quarantine` before adding a row to the CSV,
  so `quarantine_report.csv` only grows on genuinely new
  quarantinings.
- **Renaming the Quarantine collection** in Zotero Desktop between
  runs → the next run creates a new `Quarantine` collection. Same
  tradeoff as the `Inbox S2` collection (plan_02 §10): the user who
  renames also changes the env var. Not bundled as a first-class
  feature today; promoted to one if the use case appears.

## Alternatives considered and discarded

**A. Hard fail: skip items that don't enrich, leave them as orphan
attachments in the main library.** Discarded. Users would not see the
trail from Stage 04; the items would look identical to Route-C
orphans that just hadn't been processed. Any later cascade run
(e.g. after adding a new metadata source) would re-process them, which
is fine, but the UX cost in the meantime is high — the user has no
way to know that 100-250 items in the main library are "stuck" vs.
"in the normal state before 04 ran".

**B. Tag only, no collection.** Discarded. The tag surfaces items in
search, but the user's mental model of "where in my Zotero are the
failures" wants a place, not a filter. Zotero's tag UI is secondary
to its collection UI, and the user's triage pattern in other tools
(Rayyan / Covidence) also uses "buckets" (=collections), not tags.

**C. Separate Zotero library.** Discarded. Too heavy — library
creation is a manual, mult-step Zotero operation, not idempotent via
the API, and interferes with Better BibTeX / sync setup. Collections
are the right unit.

**D. LLM quality escalation before quarantine.** I.e., re-run 04d
with `gpt-4o` (or a longer prompt with the full PDF) on items that
failed the first 04d attempt. Discarded for v1; revisit with data
(ADR 005 §Alternatives D). The tradeoff: users pay 10-20× more on
items that are *probably* unrecoverable (the failure mode is usually
PDF quality, not model quality), for a single-digit reduction in the
quarantine fraction.

## Relation to other ADRs

- **ADR 005** (`gpt-4o-mini` for tagging/extraction) — complementary:
  the bound on 04d's per-item spend is only acceptable because we
  have a fallback for items the model can't extract.
- **ADR 010** (Ruta A uses OpenAlex, not Zotero translator) — context:
  OpenAlex coverage gaps in LATAM are the single biggest driver of
  items reaching 04e. Issue #46 is the v1.1 fix for that.
- **ADR 014** (Stage 03 dedup skips attach when existing item has a
  PDF) — orthogonal: applies on the way in, before Stage 04 runs.
- **Plan_01** §3 Etapa 04 "Criterio de éxito" thresholds (<10% anglo /
  <25% LATAM) are directly enforced by this ADR's reporting path.
