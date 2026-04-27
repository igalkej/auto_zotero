# ADR 019 — Substage 04bs implements via Crossref Member 530 (SciELO mirror), not search.scielo.org

**Status**: Accepted
**Date**: 2026-04-27
**Deciders**: project owner
**Supersedes**: —
**Superseded by**: —
**Amends**: ADR 018 (§"Sources evaluated" SciELO row; §Decision §1; §"Implementation artefacts" §1).

---

## Context

ADR 018 (PR #59, merged 2026-04-27) added two substages to S1 Stage 04: `04bs` (SciELO) and `04bd` (DOAJ). The Decision named `search.scielo.org`'s Solr-backed JSON endpoint as the implementation target for `04bs`, on the basis that it is publicly searchable and returns DOI + title + authors + journal in a single round-trip.

A pre-implementation spike (same day, before any code) showed that this assumption does not survive contact with reality:

- **`search.scielo.org/*` returns 403 Forbidden** to anonymous `httpx` clients on every variant tested — `format=json`, `output=json`, no format, alternative paths (`/api/v0/`, `/search/`), with and without `Referer: https://search.scielo.org/`, with and without `Accept: application/json`. The 403 comes from nginx (not Cloudflare); the endpoint is gated, not throttled.
- **ArticleMeta (`articlemeta.scielo.org`) is alive but is not a search engine.** `/api/v1/article/?code=<SciELO_PID>` returns full article metadata, but only when the caller already holds the SciELO PID. There is no documented endpoint that accepts a free-text title or a DOI and returns a record. `/api/v1/article/identifiers/` paginates over PIDs but exposes only `code/collection/processing_date` — no title, no abstract — making it useless for fuzzy lookup.
- **Internal/undocumented endpoints** (`search.scielo.org/api/v0/search`, `/search`, `api.scielo.br`) either 403 or do not resolve.

The official documentation at `scielo.readthedocs.io` confirms the public surface: ArticleMeta (by PID), CitedBy (citations to a known article), and `ref.scielo.org` (URL shortener). None supports the substage's contract.

The contract of substage `04bs` — *fuzzy-title-search → DOI-grade metadata* for a paper held only as a PDF orphan — is therefore not satisfiable against any documented public SciELO endpoint. The Decision in ADR 018 stands at the substage level; only the implementation target needs to pivot.

A second spike validated the chosen pivot:

- **Crossref REST API filtered to Member 530** (`api.crossref.org/works?query.title=<title>&filter=member:530`) returns 2,172 results for `informalidad laboral argentina` (a representative LATAM-Spanish query). Each result carries DOI, title (list), authors (with ORCID + affiliation), `published.date-parts`, `container-title` (journal), `publisher` (`FapUNIFESP (SciELO)` for SciELO-deposited records), `member` (`'530'`), `language` (sometimes), and `abstract` (often present, JATS-XML-wrapped). The polite pool (mailto in User-Agent, same pattern as 04b OpenAlex) applies.

Member 530 is Crossref's identifier for SciELO. Filtering Crossref by `member:530` returns *the SciELO catalog as visible to Crossref* — every SciELO-published paper that has a registered DOI. This is a strict subset of OpenAlex's underlying corpus (OpenAlex ingests Crossref), but the **filter changes the ranking surface**: a query like *"informalidad laboral"* against unfiltered OpenAlex ranks SciELO papers behind higher-citation anglophone matches; the same query against `member:530` ranks only SciELO papers, surfacing the relevant LATAM matches in top-5 instead of top-100.

That ranking effect is the substage's actual value-add. ADR 018's framing ("SciELO captures journals not in Crossref") was wrong about the source of the gap; the gap is **ranking-induced**, not coverage-induced, and Crossref-filter resolves it directly.

## Decision

**Substage `04bs` implements via Crossref REST API filtered to `member:530` (SciELO).**

Concretely, replacing ADR 018 §"Implementation artefacts" #1:

1. **`src/zotai/api/scielo.py`** (new). The filename and class name (`SciELoClient`) are preserved as the substage's abstraction — they refer to *the SciELO substage's HTTP adapter*, not a literal client of any SciELO-branded endpoint. Internally:

   - `SciELoClient.search_articles(title, *, per_page=5)` does a single GET to `https://api.crossref.org/works` with `params={"query.title": title, "filter": "member:530", "rows": per_page, "select": "DOI,title,author,published,container-title,abstract,type"}`. Returns the `message.items` array with defensive shape handling (returns `[]` and logs `log.warning("scielo.unexpected_shape", ...)` on shape mismatch).
   - `map_scielo_to_zotero(record)` produces a Zotero-ready dict from a Crossref `works` item. Quality gate: returns `None` if `title[0]` is missing or `author[]` is empty. Title selection: `record["title"][0]` (Crossref returns title as a list). Authors: `[{"given": a["given"], "family": a["family"]} for a in record.get("author", [])]`. Date: `record["published"]["date-parts"][0]` (year, optional month, optional day). DOI: `record["DOI"]` (always present in Crossref). Journal: `record["container-title"][0]` if non-empty. Abstract: strip JATS tags from `record["abstract"]` if present (Crossref returns `<jats:p>...</jats:p>` markup); otherwise omit. Item type: hardcode `journalArticle` (Crossref's `type` for SciELO records is uniformly `'journal-article'`).
   - User-Agent via `make_user_agent(mailto=user_email)` from `src/zotai/utils/http.py:35-39` — same polite-pool pattern as OpenAlex (Crossref documents 50 req/s in the polite pool vs lower without mailto).

The rest of ADR 018's implementation contract is unchanged:

- Cascade order, substage name (`04bs`), status label (`enriched_04bs`), `EnrichResult.items_enriched_04bs` counter, `BehaviorSettings.s1_enable_scielo`, env var `S1_ENABLE_SCIELO`, default `True`, CLI substage value `04bs`, resilience policy (HTTP 403/429/502/503 → `no_progress`; other exceptions → `failed`), and the position between `04b` and `04bd`.

- The spec-compliance contract in ADR 018 stands as written; only the underlying API target listed in §"Implementation artefacts" #1 changes.

ADR 018 §"Sources evaluated" SciELO row should be read with this amendment in mind: SciELO's *direct* APIs are not viable, but SciELO content **is** addressable through Crossref's `member:530` filter. ADR 018 §"Sources evaluated" REDIB / RedALyC / La Referencia rejections stand unchanged.

## Consequences

### Positive

- **Empirically working endpoint.** Crossref's `member:530` filter is documented, public, polite-pool-friendly, and returns clean structured JSON. Two independent spikes confirmed the request pattern works with our standard `make_async_client` + `with_retry` infrastructure.
- **Substages 04bs and 04bd retain their distinct identities.** The substage name, status label, settings field, env var, default, position, and counter — every identifier the code PR will need to assert against — are unchanged from ADR 018. The pivot is confined to "what does `SciELoClient.search_articles` actually call".
- **Ranking-induced LATAM coverage is preserved.** The filter forces the candidate list to SciELO-only items, surfacing the LATAM-Spanish matches in top-5 that unfiltered OpenAlex would rank dozens deep. This is the substage's actual value relative to 04b.
- **Abstracts more available than via Solr.** Crossref carries publisher-deposited abstracts for ~60-70% of SciELO records (publisher dependent). The original Solr endpoint exposed abstracts unevenly. Net positive.
- **Same polite-pool pattern as 04b.** Crossref and OpenAlex both honour the `mailto` in User-Agent; one User-Agent string covers both. No new HTTP infrastructure.

### Negative

- **The 04bs corpus is a strict subset of 04b's underlying corpus.** Every paper Crossref-with-`member:530` returns is also in OpenAlex's catalog. The substage is therefore not adding *new papers* to the cascade; it is improving the *ranking* of LATAM-Spanish papers inside the existing OpenAlex catalog by narrowing the search space. For a corpus where OpenAlex's top-5 already contains the right SciELO match (likely for English-language SciELO records with strong citation counts), 04bs will be redundant with 04b. The empirical answer to "how often does 04bs hit when 04b misses" is unknown until the user runs against a real LATAM corpus.
- **SciELO papers without Crossref-registered DOIs remain inaccessible.** SciELO indexes a tail of articles that were not deposited with Crossref. Those papers were also unreachable under ADR 018's original Solr-search assumption (no DOI = nothing for the cascade to pin to anyway), so this is not a regression — but it is a pre-existing gap that 04bs does not close.
- **Class name `SciELoClient` becomes mildly misleading.** The class hits Crossref, not SciELO. The naming is preserved for spec-compliance with ADR 018 and to keep the substage's identity stable; the ADR-019 `Update` lines in the docs explain the indirection.
- **Crossref's `query.title` is fuzzy-but-not-Solr.** Crossref ranks by a proprietary scoring; tokenisation differs from a Solr `ti:` query. `rapidfuzz.token_set_ratio` against the returned candidates still applies, so the substage's threshold logic (`_FUZZ_THRESHOLD = 85`) carries over unchanged. Empirical recall vs the (now-inaccessible) Solr endpoint is unmeasurable.

### Neutral

- **No code written under ADR 018 needs unwriting.** The code PR was not started (issues #60 and #61 had been filed but no implementation branch was opened). This ADR lands in PR-D2 *before* the implementation branch is cut, so the spec is consistent with reality from day one.
- **Issue #60's spec-compliance checklist is unchanged at the contract level.** Only the "API target" reference (informally noted in the issue body) is updated by this ADR; the checklist items remain identical.
- **No new env var, no new feature flag.** `S1_ENABLE_SCIELO` continues to control the substage's enable/disable.

## Alternatives considered

**A. Pursue a workaround for `search.scielo.org`'s 403.**
Rejected. Spikes tested polite UA, `Referer: https://search.scielo.org/`, alternative paths, alternative content-type negotiation. The 403 is consistent and policy-driven. Plausible workarounds (Selenium-driven browser, header spoofing, undocumented internal endpoints) are out of scope: fragile, against SciELO's evident access policy, and dependent on infrastructure (browser engines) the project deliberately does not ship.

**B. Use OpenAIRE Search Publications API instead.**
Considered. OpenAIRE aggregates LATAM repositories including LAReferencia and parts of REDIB; spike against `api.openaire.eu/search/publications?title=...` returned 30 hits for the test query. Rejected because the response shape is XML-in-JSON (with `$` keys for text content and `@` keys for attributes), which materially complicates the mapper. Crossref's flat shape produces a 30-line mapper; OpenAIRE's nested shape produces a 100+-line one with tag-stripping and recursive defaulting. The added engineering is not justified by the marginal coverage delta — for the LATAM-Spanish corpus, Crossref `member:530` already captures the SciELO subset, and other LAReferencia content tends to be preprints/grey-literature without DOIs (which the cascade can't act on anyway).

**C. Drop substage 04bs entirely; ship only 04bd (DOAJ).**
Considered. Would close issue #60 with a "spec violated reality" note; only 04bd would land. Rejected because the *ranking effect* of the filter is a real and measurable value-add even though the underlying corpus overlaps with OpenAlex. Dropping 04bs would also force a wider rewrite of ADR 018 (sections describing the `04bs → 04bd` ordering) and would close out the LATAM-specific story prematurely. Crossref-filter as the implementation lets 04bs ship with the same external contract as ADR 018 documented.

**D. Keep two-step ArticleMeta in case it helps.**
Rejected. ArticleMeta requires a SciELO PID, which the substage does not have at lookup time. The two-step `Crossref → DOI → guess SciELO PID via Crossref's `relation.has-version` chain → ArticleMeta` was tried in a spike: the relation chain is missing for most SciELO-Crossref records. ArticleMeta does not improve the substage's contract over Crossref alone.

**E. Rename the file and class to reflect the Crossref-via-member-530 implementation.**
Considered: rename `src/zotai/api/scielo.py` → `src/zotai/api/scielo_via_crossref.py`, rename `SciELoClient` → `CrossrefSciELoMirrorClient`. Rejected. The substage's identity (`04bs`, `enriched_04bs`, `s1_enable_scielo`, `S1_ENABLE_SCIELO`) is the user-facing abstraction, and ADR 018's spec-compliance contract names them. Keeping the file/class names aligned with the substage abstraction (rather than the underlying API) preserves the contract. A docstring on `SciELoClient` that points to ADR 019 is a 2-line documentation cost; a rename would force every spec-compliance reference in ADR 018 to be re-cross-checked.

## References

- ADR 018 §"Sources evaluated" SciELO row, §"Decision" §1, §"Implementation artefacts" §1 — the sections amended here.
- `docs/plan_01_subsystem1.md` §3 Etapa 04 — 04bs block updated in the same PR-D2 to reflect the Crossref endpoint.
- `docs/economics.md` §2 — 04bs bullet updated.
- Crossref REST API documentation: `https://api.crossref.org/swagger-ui/index.html` (filter syntax, `query.title`, `select`).
- Crossref Member 530 (SciELO): `https://api.crossref.org/members/530`.
- Issue #46 — stays open; REDIB / RedALyC / La Referencia continue to be tracked there pending real-corpus signal (no change from ADR 018).
- `src/zotai/api/openalex.py` — pattern reused (single-step GET against Crossref-feed-API + polite mailto).
