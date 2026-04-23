# ADR 014 — Stage 03 dedup: skip attach when existing Zotero item already has a PDF

**Status**: Accepted
**Date**: 2026-04-22
**Deciders**: project owner
**Supersedes**: —
**Superseded by**: —

---

## Context

S1 Stage 03 imports PDFs into Zotero. Ruta A (plan_01 §3 Etapa 03,
ADR 010) resolves a DOI → OpenAlex metadata → `create_items` in
Zotero. Before creating, it checks whether the DOI already exists in
the user's library via `_find_existing_doi` (a `pyzotero.items(q=doi,
qmode="everything")` quicksearch). If it does, the plan's edge-case
rule says: *"Item ya existe en Zotero (detectable por DOI duplicado):
no crear de nuevo, asociar nuestro `state.db` con el `item_key`
existente."*

The current implementation honours the "do not create" half of that
rule, but it still calls `zotero_client.attachment_simple([our_pdf],
parent_key=existing_key)`. Result: the existing item collects a second
PDF child. If the user had already imported that paper with its own
PDF (via Zotero's browser connector, or a prior S1 run that got
interrupted), they end up with two PDFs hanging off one bibliographic
record — visibly duplicated in Zotero's item tree, with no flag as to
which was "the user's" copy.

The plan did not specify the attach behaviour here, only the
"associate" behaviour on the parent key side. We need to pick a
policy and write it down.

## Decision

**On dedup, skip the attach if the existing Zotero item already has at
least one PDF attachment. Attach our PDF only if the existing item has
none (metadata-only prior import).**

Concretely:

1. `ZoteroClient.children(item_key)` is added to the thin wrapper over
   pyzotero (no dry-run guard — reads are always real, same as
   `items()`).
2. `stage_03_import._existing_has_pdf_attachment(client, item_key)`
   walks the children once and returns True iff any child is an
   attachment with `contentType` starting with `application/pdf`. HTML
   snapshots, notes, and non-PDF attachments do not count.
3. The Ruta A dedup branch in `_import_one`:
   - If `_existing_has_pdf_attachment` → do not call
     `attachment_simple`. Record `ImportRow.status = "deduped"`.
   - Else → call `attachment_simple(parent_key=existing_key)`. Record
     `ImportRow.status = "deduped_pdf_added"`.
4. Both statuses count toward `ImportResult.items_deduped` and toward
   `items_route_a`. The CSV surfaces the distinction in the `status`
   column so Stage 06 validation can break out "dedup no-op" vs
   "dedup filled-in".
5. Dry-run never calls `children()`; status stays `dry_run` as before.

## Consequences

### Positive

- **Respects pre-existing user state.** If the user already curated
  this paper in Zotero with a PDF, they see no visible change beyond a
  `state.db` row pointing at their item. The library does not grow
  silently-duplicated PDF children.
- **Still adds value when it can.** The common case of "the user
  imported a DOI via a BibTeX dump and never had the PDF" is precisely
  where our pipeline *should* add value. Those items get our
  (potentially OCR'd) PDF.
- **One extra read per dedup hit.** `pyzotero.children(item_key)` is a
  cheap local-API call. Dedups are a minority of the corpus (plan_01
  §3 Etapa 03 §126: A is 50-60%, dedup is a subset of A). Overhead is
  measured in hundreds of local HTTP calls for a 1000-paper run — not
  noticeable next to OpenAlex's rate limit floor.
- **Explicit in the CSV.** `status=deduped` vs `status=deduped_pdf_added`
  lets Stage 06's validation report break the number apart without a
  second log scan.
- **Easy to override later if needed.** The policy lives in one
  function (`_existing_has_pdf_attachment`) and one branch. Replacing
  it with a per-user flag or a "always attach" mode is a small diff.

### Negative

- **Does not detect duplicate PDFs by content.** Two different PDFs
  (say, a preprint and the published version) of the same DOI would
  be conflated: if the user had the preprint attached, we would not
  add the published PDF. In practice, Zotero's dedup semantics are
  DOI-based too, so this aligns with how the user already thinks of
  "the same paper"; Stage 04 enrichment has a chance to flag
  preprint-vs-published if the scenario comes up. Not load-bearing
  enough to chase.
- **Silently loses our (potentially OCR'd) PDF when user's copy is
  worse.** If the user's pre-existing PDF is a scanned image with no
  text layer and ours is post-Stage-02 OCR, we throw away the
  improvement. The cost here is real but uncommon (user usually
  imports PDFs with text); a `--force-attach-on-dedup` flag would
  cover the corner case if it ever matters.
- **Relies on Zotero's `contentType` metadata.** Zotero typically
  stamps PDF attachments with `application/pdf`; if a user's
  attachment has a missing or odd content type, we would attach
  redundantly. Unlikely in practice, and the safer failure direction
  (extra PDF > none).

### Neutral

- **Semantics converge with Zotero's own "Retrieve Metadata for PDFs"
  flow.** When Zotero's desktop recognizer finds a DOI match against
  an already-imported item, it links rather than creates a parallel
  attachment. This ADR brings our pipeline's behaviour in line with
  that UX expectation.

## Alternatives considered

**A. Always attach on dedup (status quo before this ADR).**
Rejected. Produces visibly duplicated PDF children for the realistic
case of a user who had the paper already. The "no creamos de nuevo"
half of the plan rule addressed the bibliographic item; this ADR
extends the same respect to the attachment.

**B. Never attach on dedup.**
Rejected. Loses the legitimate case of a DOI-only prior import (BibTeX
dump, manual drag of metadata). Our PDF is the value-add in exactly
that scenario; skipping it defeats the point of running Stage 03.

**C. Prompt the user interactively.**
Rejected. Stage 03 processes 1000 PDFs in a session; per-item prompts
would turn a batch pipeline into a manual task that violates the
"2-3h total of human time" budget (CLAUDE.md). The user may not even
be watching when the dedup hits.

**D. Detect PDF identity by SHA-256 and attach only if different.**
Rejected for v1. Zotero's API does not expose a child attachment's
hash without downloading the bytes, so the detection would add a
round trip *plus* a file download per dedup hit. Worse, "different
bytes for the same DOI" (preprint vs published) is not a reliable
signal of "the user wants both" — see the preprint/published note in
Consequences. Revisit if a real scenario makes this matter.

**E. Configurable policy: env var `STAGE_03_DEDUP_ATTACH_POLICY=
{skip_if_has_pdf,always,never}` defaulting to `skip_if_has_pdf`.**
Rejected as premature. The default from this ADR matches what a
reasonable single-investigator workflow expects. Adding knobs for
hypothetical users invites the "make everything configurable"
antipattern CLAUDE.md warns against. Revisit if two real users need
two different behaviours.

## References

- `docs/plan_01_subsystem1.md` §3 Etapa 03 "Edge cases" — the partial
  rule this ADR extends
- `docs/decisions/010-ruta-a-openalex-not-zotero-translator.md` — the
  Ruta A pipeline this lives inside
- `src/zotai/s1/stage_03_import.py` — `_existing_has_pdf_attachment`,
  the `deduped_pdf_added` literal, and the branched `_import_one`
  dedup path
- `src/zotai/api/zotero.py` — `ZoteroClient.children`
- `tests/test_s1/test_stage_03.py` — three new tests covering the
  two branches plus the HTML-snapshot-doesn't-count edge case
