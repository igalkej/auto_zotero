# ADR 018 — Stage 04 cascade: add SciELO (04bs) and DOAJ (04bd) substages for LATAM and open-access coverage

**Status**: Accepted
**Date**: 2026-04-27
**Deciders**: project owner
**Supersedes**: —
**Superseded by**: —

---

## Context

Issue #46 surfaces a known gap in S1 Stage 04's enrichment cascade. The current cascade is:

```
04a (aggressive ID extraction) → 04b (OpenAlex fuzzy) → 04c (Semantic Scholar fuzzy) → 04d (LLM extraction) → 04e (quarantine)
```

For corpora dominated by anglophone journals indexed in Crossref, this cascade is sufficient: the vast majority of items resolve at 04a–c, well within the <10% quarantine target documented in `plan_01_subsystem1.md` §3 Etapa 04.

For the project's actual primary user — a CONICET researcher with a LATAM-heavy corpus (CEPAL Review, Desarrollo Económico, Estudios Económicos, Revista BCRA, BID/CAF/IADB working papers, plus most SciELO and RedALyC titles) — coverage of OpenAlex and Semantic Scholar drops to 20–50% per journal. Items that should resolve at 04b/04c instead fall through to 04d (LLM, $0.0004/item, with quarantine risk on malformed JSON) or 04e (quarantine, value loss). The plan_01 §3 Etapa 04 "Aviso — corpus LATAM-heavy" already calls this out; the existing target accepts up to <25% quarantine for LATAM corpora as an explicit weakness pending v1.1.

This ADR closes part of that gap by adding two free, public, fuzzy-title-searchable metadata sources to the cascade between 04b and 04c. It also documents which other candidate sources from issue #46 and from the user's evaluation request (DOAJ, ERIH PLUS, Scopus) are *not* viable as cascade substages and why.

### Sources evaluated

| Source | API surface | Verdict |
|---|---|---|
| **SciELO** | `search.scielo.org` Solr-backed search (`q=ti:"<title>"&format=json`), public, no auth, no documented rate limit. The optional two-step ArticleMeta (`articlemeta.scielo.org/api/v1/article`) is unnecessary — the Solr response already carries DOI + multilingual title + authors + journal + year + language for the records we need to map. | ✅ Include — substage `04bs` |
| **DOAJ** | `doaj.org/api/v3/search/articles/<query>`, Elasticsearch query syntax (`bibjson.title:"<title>"~`), public, no auth for read, **2 req/s with bursts up to 5**. Returns `bibjson` with title, author[], year, journal{title,country,language}, identifier[type=doi], abstract, keywords. | ✅ Include — substage `04bd` |
| **REDIB** | OAI-PMH only at `redib.org/oai-redib`. OAI-PMH is for incremental harvesting, not fuzzy lookup; the protocol does not support free-text title query. Building a local index from harvest is out of scope for v1.1. | ❌ Defer — issue #46 stays open |
| **RedALyC** | OAI-PMH (Dublin Core, throughput 20–200 records/min — insufficient for per-item lookup) plus a "Journal API" documented in a 2023 Zenodo PDF that is for journal-level metadata, not article search. | ❌ Defer — issue #46 stays open |
| **La Referencia** | OAI-PMH aggregator over LATAM national repositories. Same problem as RedALyC. | ❌ Defer — issue #46 stays open |
| **ERIH PLUS** | An *index of journals* (humanities and social sciences), not articles. Article-level data only via Dimensions (commercial). The public surface answers "is journal X indexed?" — not "what's the metadata for this article?". Does not fit the cascade's per-article fuzzy-lookup contract. | ❌ Reject as cascade source. Possible future use as a journal-quality gate downstream of 04d; tracked separately if it ever matters. |
| **Scopus** | Full REST API with TITLE() and DOI search, 20k queries/7d quota, **but requires Elsevier API key bound to an institutional token tied to a subscribing institution**. The project's distribution scenario α (CLAUDE.md) is one researcher per instance against their own library, including researchers without Scopus institutional access. Adding Scopus as a default cascade source would gate LATAM coverage behind a commercial subscription. | ❌ Reject as default. Possible future opt-in via `SCOPUS_API_KEY` env when the user explicitly has access; not in v1.1. |

## Decision

**Add two new substages to S1 Stage 04 between `04b` and `04c`, in order:**

1. **`04bs` — Match fuzzy contra SciELO**, single-step against `search.scielo.org`'s Solr-backed JSON endpoint. Default ON via `BehaviorSettings.s1_enable_scielo: bool = True` (env `S1_ENABLE_SCIELO`).
2. **`04bd` — Match fuzzy contra DOAJ**, single-step against `doaj.org/api/v3/search/articles/<query>`. Default ON via `BehaviorSettings.s1_enable_doaj: bool = True` (env `S1_ENABLE_DOAJ`).

The new cascade is:

```
04a → 04b (OpenAlex) → 04bs (SciELO) → 04bd (DOAJ) → 04c (Semantic Scholar) → 04d (LLM) → 04e (Quarantine)
```

### Order rationale

- **04bs before 04bd**: SciELO has higher *per-request specificity* for LATAM-Spanish corpora (~1.7k iberoamerican journals, locally curated). DOAJ has higher *horizontal recall* (~20k open-access journals globally) but lower specificity per LATAM lookup. Trying the higher-specificity source first; the broader source picks up what the first misses.
- **Both before 04c**: Semantic Scholar has stricter rate limits (100 req/5min without key) and weaker LATAM coverage than either SciELO or DOAJ for the project's target corpus. Putting the LATAM-stronger sources earlier in the cascade reduces load on 04c and the chance of it being the chokepoint.
- **Both before 04d**: free vs paid. Both new substages are gratis; 04d costs ~$0.0004/item.

### Implementation artefacts

1. **`src/zotai/api/scielo.py`** (new). `SciELoClient.search_articles(title, *, per_page=5)` does a single GET to `search.scielo.org` with `format=json`, parses `payload["diaServerResponse"][0]["response"]["docs"]` with defensive shape handling (returns `[]` and logs `log.warning("scielo.unexpected_shape", ...)` on shape mismatch). `map_scielo_to_zotero(doc)` produces a Zotero-ready dict; quality gate identical to the Semantic Scholar mapper (returns `None` if title or authors missing). Multilingual title selection prefers the entry matching `doc["la"]`, falling back to the first non-empty title.

2. **`src/zotai/api/doaj.py`** (new). `DOAJClient.search_articles(title, *, per_page=5)` does a single GET to `https://doaj.org/api/v3/search/articles/<URL-encoded query>` with `pageSize=per_page&page=1`. Query syntax: `bibjson.title:"<title>"~` (Elasticsearch fuzzy). Parses `payload["results"]`. `map_doaj_to_zotero(record)` extracts from `record["bibjson"]`: title, author[].name (split via `_split_name`), year, identifier[type=doi], journal.title, journal.language[0], abstract. Same quality gate.

3. **`src/zotai/s1/stage_04_enrich.py`**. Two new helper functions, `_enrich_04bs_one()` and `_enrich_04bd_one()`, structurally cloned from `_enrich_04b_one()`. Both inserted between 04b and 04c in `_run_per_item_cascade()`, gated by their respective `*_client is not None` check (which is itself gated by the feature flag at client construction time). The `EnrichSubstage` Literal, the `EnrichStatus` Literal (`enriched_04bs`, `enriched_04bd`), and `_ENRICHED_STATUSES` all extend to include the new statuses. The `EnrichResult` dataclass gains `items_enriched_04bs` and `items_enriched_04bd` counters.

4. **`src/zotai/config.py`**. `BehaviorSettings` gains `s1_enable_scielo: bool = True` and `s1_enable_doaj: bool = True`. No new settings classes — both sources are open and parameterless.

5. **`src/zotai/cli.py`**. The `--substage` flag accepts `04bs` and `04bd`; output reports `enriched_04bs` and `enriched_04bd` counts.

6. **No `utils/http.py` changes**. `make_async_client()` + `make_user_agent(mailto=...)` + `with_retry()` cover both clients.

7. **No Alembic migration**. The Zotero-ready payload produced by both new mappers is structurally identical to the existing OpenAlex/SS payload (`itemType`, `title`, `creators`, `date`, `abstractNote`, `DOI?`, `publicationTitle?`). The `Item.metadata_json` column already accepts it.

### Resilience policy

The cascade orchestrator must not be brittle to a misbehaving external service. Both new substages, on `httpx.HTTPStatusError`:

- **403 / 429 / 502 / 503** → return `no_progress` with error label `<source>_unavailable:<status>`. The cascade flows to the next substage. These statuses are operationally transient or service-side and should never single-handedly fail an item.
- **Any other exception** → return `failed`. This is a genuine bug worth investigating in the CSV report.

The defensive parsing inside `search_articles()` itself (unexpected response shape) returns `[]`, logs `log.warning("<source>.unexpected_shape", ...)`, and lets the cascade move on — same as 04b/04c behaviour.

### Spec-compliance contract

The code PR that implements this ADR must satisfy these literal names and defaults:

| Concept | Canonical name |
|---|---|
| Substage names | `04bs`, `04bd` |
| Status labels | `enriched_04bs`, `enriched_04bd` |
| Settings fields | `BehaviorSettings.s1_enable_scielo`, `BehaviorSettings.s1_enable_doaj` |
| Env vars | `S1_ENABLE_SCIELO`, `S1_ENABLE_DOAJ` |
| Defaults | both `True` |
| CLI substage values | `04a, 04b, 04bs, 04bd, 04c, 04d, 04e, all` |
| Cascade order | `04a → 04b → 04bs → 04bd → 04c → 04d → 04e` |
| Counter fields | `EnrichResult.items_enriched_04bs`, `EnrichResult.items_enriched_04bd` |

Spec-compliance assertions belong in the test suite of the code PR (e.g. one test that the cascade visits substages in this exact order, one test that defaults are ON, one test that env var names match).

## Consequences

### Positive

- **Closes the LATAM coverage gap as far as freely available APIs allow.** SciELO is the canonical Iberoamerican index; DOAJ is the canonical open-access index. Together they capture the bulk of LATAM economics and social-science journals not in Crossref/OpenAlex.
- **Zero marginal cost.** Both substages are gratis. Using them ahead of 04d (LLM, $0.0004/item) reduces 04d's actual workload and therefore the realised cost on LATAM-heavy corpora.
- **Reuses every existing pattern.** Both clients reuse `make_async_client` + `with_retry`. Both substages clone `_enrich_04b_one` literally with cosmetic substitutions. Same fuzzy threshold (`_FUZZ_THRESHOLD = 85`), same dedup-on-attach policy (ADR 014), same reparenting flow.
- **Cleanly opt-out-able.** Anglo-only corpora can disable both via env vars. Default ON keeps the LATAM use case (the actual primary user) unblocked without surprising other users — the LATAM extension is the project's own primary use case, not an exotic add-on.
- **Documents the rejected sources explicitly.** Future contributors don't need to re-evaluate REDIB / RedALyC / La Referencia / ERIH PLUS / Scopus from scratch. The conditions under which they'd become viable are written down (real corpus signal, harvest-and-index local store for OAI-only sources, opt-in env for Scopus).

### Negative

- **Two extra round trips per cascade hit beyond 04b.** For an item that ultimately falls to 04d, the cascade now does up to two extra calls (`search.scielo.org` + DOAJ). Latency: ~300–800 ms each, network-dependent. Mitigated by the resilience policy (transient errors fall through fast as `no_progress` without retry storms) and by the fact that fuzzy matches that resolve early in the cascade *save* time and money downstream.
- **Cloudflare in front of `search.scielo.org`.** The endpoint is behind Cloudflare's WAF. Default `httpx` UAs sometimes 403; a polite UA (`zotai/<version> (mailto:<email>)`) typically passes. The implementation must validate empirically; if a future Cloudflare rule update breaks the polite-UA path, the substage degrades to `no_progress` per the resilience policy and the cascade still produces output, but coverage drops until the headers are adjusted.
- **DOAJ rate limit (2 req/s).** In the cascade context (one request per item per substage, throughput dominated by OCR/LLM elsewhere), 2 req/s is holgado. Future batch reconcile or backfill-style flows would need a token bucket; out of scope for v1.1.
- **Two new feature flags.** `S1_ENABLE_SCIELO` and `S1_ENABLE_DOAJ` enlarge the configuration surface by two booleans. Justified by the explicit per-source opt-out need (a researcher with a known anglo-only corpus may want the cleaner cascade); kept simple — booleans, no per-source settings classes.

### Neutral

- **Reversible.** Setting both flags to `false` recovers the pre-ADR cascade behaviour with zero code differences in the hot path (the `if scielo_client is not None` and `if doaj_client is not None` guards short-circuit cheaply).
- **Doesn't affect 04a, 04b, 04c, 04d, or 04e semantics.** The new substages slot in without changing existing ones. Tests for existing substages stay green.
- **Doesn't change the success target for LATAM corpora yet.** plan_01 §3 keeps the ≤25% LATAM quarantine target as the *worst-case* tolerance until empirical data from a real CONICET corpus run shows the post-ADR-018 number. The expectation is that 04bs+04bd absorbs 15–30% of items that previously fell through to 04d/04e, but this is not a contractual commitment.

## Alternatives considered

**A. Parallelise SciELO + DOAJ alongside 04b (race-to-first-match).**
Rejected. Adds race-resolution complexity (which match wins if two come back over threshold?) without a clear gain over sequential. The existing cascade contract — "first match wins, in the order written" — is a strong invariant; breaking it for two sources is not justified by the latency saving.

**B. Place both new substages after 04c, not before.**
Rejected. Loses the rate-limit-saving benefit (Semantic Scholar is the cascade's tightest free rate limit). For a LATAM-heavy corpus, this also means the cascade hits SS's coverage hole *before* trying the LATAM-specific sources, increasing the chance of a no-progress slog through 04c that 04bs would have resolved cleanly.

**C. Two-step SciELO (Solr search → ArticleMeta fetch by SciELO ID).**
Rejected. The Solr response already carries DOI + title + authors + journal + year for the records we need to map. Two-step adds latency, complexity, and a partial-failure mode (search succeeds, ArticleMeta down). The single-step path is sufficient for the cascade's quality gate; if a future need for richer metadata emerges, ArticleMeta can be added behind a flag.

**D. One umbrella `04L` substage that internally fans out to both SciELO and DOAJ.**
Rejected. The status labels (`enriched_04bs` vs `enriched_04bd`) carry diagnostic value: when reading the CSV or running validation reports, knowing *which* source matched lets the user spot patterns ("DOAJ is doing all the work, SciELO matches nothing — maybe the SciELO endpoint is misconfigured"). Lumping them together hides that signal.

**E. Dedicated `SciELoSettings` / `DOAJSettings` classes in `config.py`.**
Rejected. Each source has exactly one configurable knob: the enable bool. Two booleans on `BehaviorSettings` is far simpler than two new pydantic classes that contain one field each. If a future ADR adds API keys, rate-limit overrides, or endpoint switches, the classes can be promoted then; YAGNI for v1.1.

**F. Adopt Scopus as a default cascade source (gated by API key presence).**
Rejected. The project's distribution scenario α explicitly assumes per-researcher instances; many target users (CONICET fellows without Elsevier institutional contracts) cannot use Scopus. Making it part of the default cascade would silently degrade their experience to "yet another item in quarantine because Scopus auth failed". Future opt-in via `SCOPUS_API_KEY` is a separate decision that requires its own ADR if and when a user requests it.

**G. Adopt ERIH PLUS as a substage.**
Rejected on factual grounds: ERIH PLUS does not expose article-level metadata. There is no fuzzy-title-to-article query for it to answer. ERIH PLUS could plausibly be a *journal-quality validator* downstream of 04d (verifying the LLM-derived journal name is real), but that's a different shape of integration and not in this ADR's scope.

**H. Build a local OAI-PMH harvest of REDIB / RedALyC / La Referencia and lookup against the local index.**
Rejected for v1.1. Bootstrapping a local copy of three OAI-PMH corpora is a significant project on its own (storage, refresh policy, indexing layer, query layer). The decision to do this should follow real-corpus data: if 04bs + 04bd together still leave >20–25% in quarantine on the user's actual corpus, the harvest path becomes worth the engineering. Until then, issue #46 stays open as the marker.

## References

- `docs/plan_01_subsystem1.md` §3 Etapa 04 — the cascade contract this ADR extends.
- `docs/decisions/010-ruta-a-openalex-not-zotero-translator.md` — the upstream choice (OpenAlex over Zotero translator) for 04b that this ADR mirrors at 04bs and 04bd.
- `docs/decisions/014-stage-03-dedup-skip-attach-if-pdf-exists.md` — dedup/attach policy reused unchanged in the new substages' reparenting flow.
- Issue #46 — stays open; tracks the remaining LATAM sources (REDIB, RedALyC, La Referencia) plus the harvest-and-index strategy that would make them viable.
- SciELO ArticleMeta API: `https://articlemeta.scielo.org/api/v1/article/` (referenced for completeness; not used in v1.1).
- DOAJ Public API v3: `https://doaj.org/api/v3/docs` — the Elasticsearch query syntax and `bibjson` schema this ADR maps to Zotero fields.
- ERIH PLUS: `https://kanalen.hkdir.no/publiseringskanaler/erihplus` — referenced for the rejection rationale.
- Scopus API: `https://dev.elsevier.com/sc_apis.html` — referenced for the rejection rationale (institutional token requirement).
