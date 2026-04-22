# ADR 010 — Ruta A uses OpenAlex, not Zotero's translator chain

**Status**: Accepted
**Date**: 2026-04-22
**Deciders**: project owner
**Supersedes**: —
**Superseded by**: —

---

## Context

Stage 03 of Subsystem 1 (plan_01 §3 Etapa 03) imports PDFs into Zotero
via two routes: **Ruta A** when a DOI was detected in Stage 01, and
**Ruta C** (orphan attachment, enriched later in Stage 04) for
everything else. The purpose of Ruta A is to produce a fully-populated
bibliographic item in one shot — title, authors, year, venue, abstract
— without waiting for the enrichment cascade.

Earlier versions of the spec described Ruta A as:

> Llamar Zotero API `POST /items` con `{itemType: 'journalArticle',
> DOI: detected_doi}` via translator chain → recupera metadata.

This phrasing implies that posting an item with only a DOI to Zotero's
standard API will trigger Zotero's **translator** to fetch and fill in
the metadata. That implication is incorrect. Zotero's translator
machinery lives in two places:

- The Zotero Desktop UI ("Retrieve metadata for this item" / the
  magic-wand icon).
- The local connector endpoints at `http://localhost:23119/connector/*`
  that Zotero's browser extensions use when a user hits "Save to Zotero"
  on a journal page.

The standard data API (`/users/<id>/items`, which is what `pyzotero`
wraps) simply stores the fields you give it. A POST with only
`{itemType, DOI}` creates a barebones item whose only populated field is
the DOI — useless as a bibliographic record.

So the spec as written could not be implemented literally. We have to
pick an actual mechanism for DOI → metadata.

This is exactly the same situation we faced with **Ruta B** earlier in
the project (see `plan_01_subsystem1.md` §3 Etapa 03 "Nota — ausencia
de Ruta B" and the PR #22 discussion): a route that depends on Zotero's
connector/recognizer endpoints is depending on a non-public,
version-fragile surface. Ruta B was removed for that reason; the same
reasoning applies here to any "use Zotero's translator" implementation
of Ruta A.

## Decision

**Ruta A resolves DOI → metadata via OpenAlex's public API**
(`GET https://api.openalex.org/works/doi:<doi>`), then writes the full
bibliographic record to Zotero using `pyzotero.create_items([{...}])`.

Concretely:

1. `zotai.api.openalex.OpenAlexClient.work_by_doi(doi)` → returns an
   OpenAlex `Work` object (JSON) or `None` on 404.
2. A small mapper translates OpenAlex fields to Zotero's item schema
   (title, authors as `[{creatorType: 'author', firstName, lastName}]`,
   DOI, year from `publication_year`, venue from
   `primary_location.source.display_name`, abstract from
   `abstract_inverted_index` reconstructed, item_type from OpenAlex's
   `type` field mapped onto Zotero's `itemType` enum).
3. Quality gate: if the mapped record lacks a non-empty `title` or has
   zero authors, the item is *not* created via Ruta A — it falls through
   to Ruta C (orphan attachment) and Stage 04 takes over.
4. On a valid record: `pyzotero.create_items([record])` → Zotero returns
   the new `item_key` → we attach the PDF with
   `pyzotero.attachment_simple([pdf_path], parent_key=item_key)`.

Ruta C absorbs the failure modes: DOI not in OpenAlex, OpenAlex returns
a record without title/authors, network error after retries.

## Consequences

### Positive

- **Reliable interface.** OpenAlex's REST API is documented, versioned,
  and rate-limited predictably. No dependency on Zotero Desktop
  internals.
- **Reuses existing code.** `OpenAlexClient.work_by_doi` was implemented
  in Phase 1 as part of the shared infrastructure (`#2`). Stage 03 does
  not need a new HTTP client or schema.
- **Symmetric with Stage 04b.** Stage 04b also queries OpenAlex (by
  title instead of DOI) when Ruta C items need enrichment. Items
  processed via Ruta A and items recovered via Stage 04b end up with
  metadata from the same source — uniform quality and vocabulary across
  the library.
- **Covers the target audience.** OpenAlex ingests CrossRef, DataCite,
  arXiv, PMC, and PubMed. Coverage of DOIs for journal articles,
  preprints, and theses in the researcher's corpus is expected to
  exceed 98%. The remaining ~2% (monographs with publisher-specific
  DOIs, very recent DOIs not yet propagated) fall to Ruta C → Stage 04
  cascade, which tries multiple fallbacks.
- **Consistent failure semantics.** When Ruta A fails, Ruta C creates
  an orphan attachment that Stage 04's five-stage cascade handles. The
  user never ends up with a Zotero item that has a DOI but no other
  metadata.
- **Clear blast radius.** A Zotero library built by this pipeline is
  traceable to two external sources (OpenAlex + OpenAI) and two local
  ones (pyzotero + pdfplumber). No hidden dependency on Zotero's
  in-process translator state.

### Negative

- **Single upstream source for Ruta A's metadata.** OpenAlex is free
  and reliable today, but it is one organisation. If it becomes
  unavailable, Ruta A breaks. Mitigation: all failures cascade to Ruta
  C → Stage 04, which includes Semantic Scholar and LLM extraction as
  alternative sources. The pipeline still produces a Zotero library;
  just slower.
- **Metadata diverges from what Zotero's translator would have
  produced.** Zotero's translators are maintained per-publisher and can
  extract publisher-specific details that OpenAlex might miss (e.g.,
  corrigendum status, specific editor for book chapters). For the
  researcher's core use case (economics / LATAM papers), this
  difference is marginal.
- **No "book chapter retrieved via DOI" path.** OpenAlex has weaker
  coverage for book chapters than journal articles. Chapters with DOIs
  may be missed by Ruta A and fall to Ruta C. Stage 04d (LLM extracting
  from the PDF text) handles this — a chapter's first two pages usually
  contain its own front matter.

### Neutral

- This ADR does not preclude a future **CrossRef fallback**. If
  empirical use shows OpenAlex misses a meaningful fraction of real
  DOIs, adding `habanero.Crossref().works(ids=doi)` as a secondary try
  between "OpenAlex 404" and "fall to Ruta C" is a small change. Not
  scoped for v1 because the expected gap (~1-2% over OpenAlex's
  coverage) does not currently justify the added client.

## Alternatives considered

**A. Zotero connector endpoints (`/connector/savePageViaDOI` or
equivalent).**
Rejected. Same rationale as Ruta B's removal: non-public API, version-
fragile, undocumented across Zotero Desktop releases. Worth revisiting
only if Zotero publishes a stable contract for these endpoints.

**B. CrossRef directly as Ruta A's primary source.**
Rejected as the default. CrossRef is the authoritative registry for
DOIs but OpenAlex covers >98% of what CrossRef has *and* covers arXiv
preprints that CrossRef does not. Adding CrossRef as a v1.1 fallback
stays open (see "Neutral" above).

**C. LLM lookup by DOI (give an LLM the DOI, ask for the paper
metadata).**
Rejected. LLMs hallucinate bibliographic citations at rates documented
at 15-30% depending on model and corpus ([Dahl et al. 2024 on
hallucinated legal citations](https://hai.stanford.edu/news/hallucinating-law-legal-mistakes-large-language-models-are-pervasive);
similar rates observed in scholarly citation benchmarks). Because the
LLM is not grounded in any verifiable source when given only a DOI,
wrong metadata is indistinguishable from right metadata downstream —
exactly the "trabajo silencioso" antipattern that
`plan_glossary.md` names as prohibited. Contrast with Stage 04d, where
the LLM extracts metadata **from the PDF's own first pages**; there the
LLM is bounded by what the document says about itself and the risk is
manageable.

**D. Barebones item with only DOI, enrich later in Stage 04.**
Rejected because it collapses the distinction between Ruta A and Ruta
C, losing the point of having a "fast, high-quality path" when a DOI
is available. Stage 04 is a costly cascade (API calls, LLM
extraction); Ruta A exists to bypass most of it. Posting DOI-only items
would also pollute Zotero's library during the window between Stage 03
and Stage 04 — items would appear with no title, no authors, no venue,
which is jarring for a user who inspects Zotero mid-pipeline.

**E. User triggers Zotero Desktop's "Retrieve metadata" manually per
item.**
Rejected. Violates the pipeline's automation goal and the CLAUDE.md
budget of "2-3h total of human time". Also does not scale to 1000
PDFs.

## References

- `docs/plan_01_subsystem1.md` §3 Etapa 03 (this ADR's direct consumer)
- `docs/plan_glossary.md` — "Ruta A/C"
- `docs/decisions/002-sqlite-for-state.md` — the same "depend only on
  stable, documented surfaces" principle applied there
- PR #22 — removal of Ruta B (`docs(s1): drop Ruta B from Etapa 03,
  consolidate into Etapa 04 cascade`). The analogous rationale.
- `src/zotai/api/openalex.py` — the `OpenAlexClient.work_by_doi`
  implementation shipped in Phase 1 (`#2`)
