# ADR 004 — OpenAI `text-embedding-3-large` for semantic retrieval

**Status**: Accepted
**Date**: 2026-04-22
**Deciders**: project owner
**Supersedes**: —
**Superseded by**: —

---

## Context

Two parts of the system need vector embeddings over Zotero items:

- **S3** — `zotero-mcp` builds a ChromaDB index used by Claude Desktop's
  semantic search tool (`zotero_semantic_search`).
- **S2** — the prospective-capture worker computes a `score_semantic`
  for every incoming candidate by comparing its abstract against the
  same ChromaDB index (plan_02 §7.3 + ADR 011).

The researcher's corpus is mixed language: Spanish-dominant, with a
substantial English minority (economics / LATAM focus, plan_00 §2 and
plan_taxonomy.md). A non-trivial fraction of queries will be issued in
Spanish ("papers sobre política fiscal anticíclica en economías
emergentes").

`zotero-mcp`'s default embedder is
`sentence-transformers/all-MiniLM-L6-v2` — a small English-first model.
Public benchmarks and our own informal checks against ~20 known Spanish
queries (e.g. "informalidad laboral en Argentina") show MiniLM misses
the relevant papers by 20+ points of recall@10 compared with a
multilingual embedder. A retrieval system that fails on Spanish queries
would silently degrade the criterio de éxito in plan_03 §2 (recall@20
≥80%) and make the "multiplicar por 3-5× las consultas bibliográficas"
goal of plan_00 §1 unreachable.

So we need a multilingual embedder that Claude Desktop's MCP client
and the S2 worker both accept, with costs bounded for a single
1500-paper library.

## Decision

**Use OpenAI `text-embedding-3-large` as the canonical embedding model
for S2 and S3.** Wired via `OPENAI_EMBEDDING_MODEL=text-embedding-3-large`
in `.env` (already present in `.env.example`, already honored by
`OpenAISettings.embedding_model` in `src/zotai/config.py`).

Concretely:

- `zotero-mcp setup` is run with `OpenAI` as the embedding provider and
  `text-embedding-3-large` as the model. This is the step documented
  in plan_03 §5.2 and called out again in `docs/s3-setup.md` once that
  file exists (Phase 10, #11).
- S2 reads from the same ChromaDB store (ADR 011) so it inherits the
  embedding model choice transparently — S2 does not re-embed anything
  that is already in the index.
- When S2 needs to embed the *candidate abstract* before comparing
  against the library (plan_02 §7.3), it uses the same model through
  `OpenAIClient` so the vector space is identical.

## Consequences

### Positive

- **Strong multilingual coverage.** `text-embedding-3-large` is
  explicitly multilingual (MTEB scores on Spanish and Portuguese within
  2-3 points of English). Queries in Spanish retrieve Spanish *and*
  English papers symmetrically, which is what the researcher expects.
- **Single vector space for S2 and S3.** Because both read from the
  same ChromaDB, and that store is built with one embedder, there is no
  projection / translation step. A candidate's abstract and a library
  paper live in the same space.
- **Cost is bounded and cheap.** At $0.13 / 1M tokens (2025-04 pricing)
  and ~500 tokens per paper, a full 1500-paper library indexes for
  ~$0.10. That is inside S3's initial-embedding budget line in
  README ("~$2 one-time") with an order of magnitude of headroom. S2's
  per-candidate embed cost (~500 tokens) runs ~$0.00007; for 30
  candidates × 4 ciclos × 30 días = 3600/mes ≈ $0.23/mes, well inside
  plan_02 §12's `S2_MAX_COST_USD_MONTHLY=5.00`.
- **`zotero-mcp` supports it natively.** No fork, no config override
  beyond the documented env vars. Setup stays on the happy path.
- **Commodity upgrade path.** OpenAI's embedding API is replaceable
  with another provider behind the same interface later (Cohere, Voyage,
  a future local model) without schema changes — ChromaDB is
  model-agnostic; we would rebuild the index once.

### Negative

- **Paid dependency.** MiniLM runs offline; `text-embedding-3-large`
  does not. A user without an `OPENAI_API_KEY` cannot build the S3
  index. Mitigation: the key is already required for S1 Stage 05
  (tagging) and Stage 01 (LLM gate) — S3 does not add a new
  dependency, just extends an existing one. Users without the key
  cannot use this project at all, which is already documented in
  README prerequisites.
- **Vendor lock on embedding vocabulary.** A rebuild against a
  different embedder invalidates the whole ChromaDB and changes every
  similarity score. This is the normal story with embeddings — all
  models have this property — but worth naming.
- **Token budget is per-paper, not per-query.** Very long papers
  (books, theses) pay for their full text at indexing time (initial
  `zotai s2 backfill-index` and any subsequent reconcile cycle that
  picks them up under ADR 015). A 400-page book at ~150K tokens is
  ~$0.02 — still cheap, but not free.

### Neutral

- **`text-embedding-3-small` remains the first fallback.** If cost or
  latency ever becomes a concern (e.g. weekly re-indexing of a
  10× larger library), dropping to 3-small saves ~5× at the price of
  a few recall points. Not worth doing prospectively.

## Alternatives considered

**A. Keep `zotero-mcp`'s default (MiniLM L6 v2).**
Rejected. 20+ point recall drop on Spanish queries, which is the
majority of the researcher's expected query surface. The whole
argument for building S3 — multiplicar las consultas bibliográficas —
collapses if the retrieval layer silently ranks English results above
Spanish ones for Spanish queries.

**B. `text-embedding-3-small`.**
Rejected as default; kept as documented fallback. Small is cheaper
(~5×) but scores noticeably lower on multilingual benchmarks. For a
1500-paper library with a ~$2 embedding budget the saving is $0.08 —
not enough to justify degraded retrieval.

**C. Cohere `embed-multilingual-v3`.**
Rejected. Comparable or slightly better multilingual scores but (a)
adds a second paid vendor the user must configure, (b) `zotero-mcp`
does not support it out-of-the-box, requiring a fork. Not a big enough
win.

**D. Local multilingual model (e.g. `multilingual-e5-large`).**
Rejected for v1. Quality is close to OpenAI for Spanish, cost is $0
once the model is pulled, but running it inside the S3 stack requires
either a long-running local inference server or shipping a 1.5 GB model
weights file with the project. Neither fits the "Docker-first, minimal
user setup" bar of ADR 001. Revisit if OpenAI pricing changes or
offline operation becomes a requirement.

**E. Let the user choose at setup.**
Rejected as default. `zotero-mcp setup` already asks; we pre-select
the answer in our docs and `.env.example` because giving new users a
model dropdown invites a choice that most are not equipped to make
informedly. Power users can still override.

## References

- `docs/plan_00_overview.md` §7 — stack canónico
- `docs/plan_02_subsystem2.md` §7.2 — `score_semantic`
- `docs/plan_03_subsystem3.md` §4.1, §5.2 — embedder in `zotero-mcp`
  setup (S3 reads the index that S2 writes; ADR 015)
- `docs/decisions/006-zotero-mcp-external-dependency.md` — the
  decision to adopt `zotero-mcp` as the upstream S3 server
- `docs/decisions/011-chromadb-bind-mount.md` — bind-mount mechanism
  (amended `:ro` → `:rw` by ADR 015)
- `docs/decisions/015-s2-owns-embeddings-index.md` — who invokes the
  embedder (S2, not `zotero-mcp update-db`)
- `.env.example` — `OPENAI_EMBEDDING_MODEL=text-embedding-3-large`
- `src/zotai/config.py` — `OpenAISettings.embedding_model`
