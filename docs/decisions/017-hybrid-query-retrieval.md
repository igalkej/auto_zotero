# ADR 017 — Hybrid retrieval (BM25 + dense) for S2 persistent queries

**Status**: Accepted
**Date**: 2026-04-23
**Deciders**: project owner
**Supersedes**: —
**Superseded by**: —

---

## Context

S2's `score_queries` (plan_02 §7.3) evaluates every incoming candidate against the user's active `PersistentQuery` list. The original spec defined this as a pure dense similarity: `cos(embedding(query), embedding(candidate.abstract))`, max-pooled across queries.

Two empirical problems with pure-dense retrieval at this scale:

1. **Short queries are where dense embeddings underperform.** The researcher's persistent queries are typically 3-7 tokens (*"fiscal multipliers in emerging markets"*, *"informalidad laboral Argentina"*, *"political economy of inflation"*). A well-documented behaviour of dense retrievers on short queries is that the embedding collapses to a generic topical centroid and loses the discriminative power of exact terms. BM25, by contrast, rewards exact / near-exact term matches — precisely the signal a short query carries. Combining the two covers both regimes.

2. **Lexical anchors that matter.** Queries with proper nouns or rare technical terms (*"Argentina 2001 crisis"*, *"Calvo pricing"*, *"Kaldor facts"*) need the retrieval to honour the exact term, not paraphrase it. Pure dense ranks "Mexico 1995 crisis" similarly to "Argentina 2001 crisis" for a query about "Argentina 2001" — close topically but wrong. BM25 anchors on the exact term and disambiguates.

This is not a novel problem. Hybrid retrieval (BM25 + dense, fused) is the de facto industry default now: Pyserini (Castor Lab, UWaterloo), Elastic's `rrf` and `rank_feature` queries, Vespa's hybrid ranker, LangChain's `EnsembleRetriever`, Weaviate's hybrid mode, and OpenAI's own retrieval benchmarks all ship with hybrid as the recommended configuration. The improvement over pure dense is measured at 5-15 recall points depending on corpus and query distribution.

The question is *whether to adopt it for our specific case*, and *how to do it given our existing stack*.

## Decision

**`score_queries` computes a convex combination of BM25 (lexical) and dense cosine (semantic) per query, with `α = 0.4` as default**, and aggregates across queries with `max × PersistentQuery.weight`. SQLite's built-in FTS5 virtual table backs BM25; no new dependency.

Formally, for candidate `c` and query `q`:

$$
S_{\text{query}}(c, q) = \alpha \cdot S_{\text{BM25}}(c, q) + (1 - \alpha) \cdot \cos(\vec{e}_q, \vec{e}_c)
$$

with `α = query_scoring.bm25_weight` (default `0.4`, overridable via `S2_QUERY_BM25_WEIGHT`). The BM25 component is normalised to `[0, 1]` with min-max over the batch; the cosine is normalised from `[-1, 1]` to `[0, 1]` with `(cos + 1) / 2`.

Aggregation across multiple queries stays as the spec originally required: `max(S_query(c, q) × q.weight)` over active queries. Per-query weight stays in `PersistentQuery.weight` for users who want a topic to count more than others; default is `1.0`.

### Implementation artefacts

1. **`candidates.db` schema**. Add an FTS5 virtual table mirroring `Candidate`:

   ```sql
   CREATE VIRTUAL TABLE candidate_fts USING fts5(
       id UNINDEXED,
       title,
       abstract,
       tokenize = 'unicode61 remove_diacritics 2'
   );
   -- plus INSERT/UPDATE/DELETE triggers on Candidate to keep fts in sync.
   ```

   `remove_diacritics 2` ensures that "política" matches "politica" — critical for a Spanish-dominant corpus with inconsistent accent handling.

2. **Query embedding cache**. `embedding(q)` is expensive (OpenAI call, ~$0.0001 per query). Cache per `PersistentQuery.id`, invalidate when `q.query_text` changes. Avoids re-embedding the same query every cycle.

3. **Config surface**. `config/scoring.yaml` gains a `query_scoring:` block with `bm25_weight: 0.4`. `.env.example` exposes `S2_QUERY_BM25_WEIGHT` for per-run override (env takes priority over YAML).

### Calibration path

Same pattern as ADR 016 for the composite score: `α = 0.4` is a literature default, not a calibrated value. Once the `candidates.db` accumulates ≥100 triage decisions per query, a successor ADR can grid-search `α ∈ {0.2, 0.3, 0.4, 0.5, 0.6}` against observed precision and pick the point that maximises it. Until then, 0.4 is the universally-defensible default.

## Consequences

### Positive

- **Recall on short queries.** The literature gap (5-15 recall points on short / lexical-anchor queries) is the single biggest gain available to S2 scoring at this stage. For a researcher whose persistent queries are typically 3-7 tokens and frequently include proper nouns (country names, event years, author surnames), hybrid is strictly better than dense alone.
- **Zero new dependency.** SQLite 3.9+ (2015) has FTS5 built-in. The CPython 3.11 bundled sqlite is at least 3.40, so every target platform already has it. Nothing to `uv add`, nothing to document as "install separately".
- **Cheap compute.** FTS5 BM25 is ~μs per query per document on SQLite, vs ~ms for a network-round-tripped embedding. The BM25 side effectively free; the dense side is the existing embedding cost.
- **Diacritic-insensitive.** `remove_diacritics 2` is a free perk for Spanish. Queries like "politica fiscal" match "política fiscal" in stored abstracts and vice versa — no manual normalisation.
- **Same aggregation as before.** The `max` over active queries + per-query `weight` stays intact. Existing spec semantics are preserved; only the per-query score is upgraded.
- **Composable with ADR 016 RRF.** `score_queries` is one of the three criteria RRF ranks. Upgrading `score_queries` to be better-calibrated benefits RRF's fusion directly. No interaction risk — the two ADRs are orthogonal.

### Negative

- **BM25 needs a tokenizer choice.** `unicode61 remove_diacritics 2` is sensible for es/en mixed corpora but suboptimal for other languages (CJK in particular). Out of scope for v1 — the researcher's corpus is es/en. Documented as a future-ADR trigger if the language mix changes.
- **FTS5 table is extra storage.** The virtual table roughly doubles the storage of the `title + abstract` columns. For a 1500-paper library that's a few MB — negligible.
- **Two normalisations to reason about.** BM25 and cosine have different scales; the `[0, 1]` normalisation (min-max for BM25, affine for cosine) is standard but adds a small reasoning surface. The config surface keeps them opaque behind one `bm25_weight` knob.
- **α is still a knob.** We traded "no learning loop" (ADR 016) for "one knob that defaults to 0.4". That knob has a defensible default from the literature, but it *is* a knob that will eventually want calibration. Same path as ADR 016's successor.

### Neutral

- **Doesn't affect `score_tags` or `score_semantic`.** Only `score_queries` changes. The three-criterion decomposition in plan_02 §7 stays.
- **Reversible.** Setting `query_scoring.bm25_weight: 0.0` recovers pure dense behaviour. Setting `1.0` recovers pure BM25. A/B testing post-calibration is trivial.

## Alternatives considered

**A. Keep pure dense retrieval (the status quo spec).**
Rejected. 5-15 recall points on the shape of queries the researcher actually writes is a large, known-fix gap. Leaving it on the table to "simplify" is penny-wise, pound-foolish — the first quarter of S2 in production would show the gap immediately.

**B. Pure BM25.**
Rejected. Pure lexical loses paraphrase matching entirely: "fiscal stimulus" in a query would miss a paper abstract that only uses "government spending multiplier". Dense catches those; hybrid catches both.

**C. Per-query α.**
Rejected. Different queries might benefit from different α (a query with rare proper nouns wants higher α; a long conceptual query wants lower α). Technically sound but adds a per-query config field that most users won't set. Keep global α for v1; revisit if per-query sees real demand.

**D. RRF instead of convex combination, for this layer too.**
Considered. RRF fuses ranked lists; here we have exactly two rankers, and a scalar α knob is more natural for fine-tuning on one slider. RRF would also introduce a second `k` constant (60 for composite_score per ADR 016, a separate k here). Convex combination is simpler at the per-query layer and consistent with the broader hybrid-retrieval literature (RRF is typically the *meta-fusion* across *different retrieval strategies*; a single BM25+dense hybrid is usually done with convex combination or tuned weights).

**E. Elasticsearch / OpenSearch as the retrieval backend.**
Rejected. A heavy extra service for a use case SQLite FTS5 handles natively at our scale (1500 papers, a handful of queries per cycle). ADR 002 picked SQLite as state store precisely to avoid this kind of creep; same argument applies here.

**F. Weaviate / Qdrant for hybrid in one system.**
Rejected. Same reason as E plus: we already committed to ChromaDB for semantic (ADR 015), and adding another vector store for queries would split responsibility weirdly. SQLite FTS5 + existing ChromaDB is the least new infrastructure.

**G. OpenAI Embeddings with larger context for the query.**
Rejected — wrong axis. The problem isn't that the query is *too short to embed*; OpenAI embeds short queries fine. The problem is that *dense similarity* of short-query embeddings against long-document embeddings systematically underweights exact-term matches. Bigger context on the query doesn't help. Hybrid does.

## References

- `docs/plan_02_subsystem2.md` §7.3 — rewritten to use hybrid retrieval
- `docs/plan_02_subsystem2.md` §5 — note added about the FTS5 virtual table in `candidates.db`
- `docs/plan_02_subsystem2.md` §12 — new env var `S2_QUERY_BM25_WEIGHT`
- `docs/plan_02_subsystem2.md` §15 — SQLite ≥ 3.9 with FTS5 as a dependency
- `docs/decisions/016-rrf-composite-score.md` — the sibling ADR; this one upgrades `score_queries`, that one fuses the three criteria
- `config/scoring.yaml` — `query_scoring.bm25_weight`
- SQLite documentation: [FTS5 extension](https://www.sqlite.org/fts5.html). BM25 is the default ranking function (§7).
- Karpukhin et al. "Dense Passage Retrieval" (EMNLP 2020) — the canonical paper establishing dense retrieval's strengths on longer queries and weaknesses on short ones.
- [Gao, Xiong. "Complementing Lexical Retrieval with Semantic Residual Embedding" (ECIR 2021)](https://arxiv.org/abs/2004.13969) — documents the recall gap we close here.
- Castorini / Pyserini default configurations for hybrid retrieval (α ≈ 0.4 across TREC collections).
