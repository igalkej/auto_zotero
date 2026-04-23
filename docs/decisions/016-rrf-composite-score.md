# ADR 016 — Reciprocal Rank Fusion for S2 composite score

**Status**: Accepted
**Date**: 2026-04-23
**Deciders**: project owner
**Supersedes**: —
**Superseded by**: —

---

## Context

S2 computes three independent scores for every candidate paper (plan_02 §7):

- `score_tags ∈ [0, 1]` — overlap between the paper's LLM-extracted tags and the user's library tag vocabulary (§7.1).
- `score_semantic ∈ [0, 1]` — mean cosine of top-k nearest library papers in ChromaDB (§7.2).
- `score_queries ∈ [0, 1]` — max cosine against the user's persistent queries (§7.3).

These feed into a single `score_composite` that drives ranking in `/inbox`.

The original spec defined composite as a **linear weighted mean**:

```
composite = (w_t·S_tags + w_s·S_sem + w_q·S_queries) / (w_t + w_s + w_q)
with w_t=1, w_s=2, w_q=2 by default.
```

Two problems with this baseline:

1. **Pesos arbitrarios.** `w_s = w_q = 2` and `w_t = 1` are pulled out of the air. There's no data to calibrate them before S2 has seen a hundred or so triage decisions, and weighted-mean behaviour is sensitive to those choices. The spec explicitly defers a "learning loop" to v1.1 (plan_02 §3) to avoid echo chamber — but there's a difference between *calibrating scales* (adjusting how much each signal counts) and *biasing by past decisions* (training a model to reproduce the user's taste). Calibration is not echo chamber.

2. **Promedio ponderado destruye señales ortogonales.** A paper with `score_tags=0.1, score_semantic=0.1, score_queries=0.9` — exactly the "out-of-coverage but matches a specific query" case that the whole `PersistentQuery` feature exists to surface — produces `composite = (1·0.1 + 2·0.1 + 2·0.9) / 5 = 0.42`, which is middling and gets buried. The user then never sees the one paper they specifically asked for. The linear combination penalises exactly the kind of off-diagonal match that S2 is supposed to celebrate.

We need a composite that (a) does not depend on arbitrary pre-data weights, (b) preserves the signal of "ranks high on any one criterion", and (c) has room to evolve into a calibrated model once we have decision data.

## Decision

**Reciprocal Rank Fusion (RRF, Cormack / Clarke / Büttcher 2009) is the default method for `score_composite`. The legacy weighted-mean is documented as an opt-in fallback.**

Formally, for each candidate `d` and each criterion `c ∈ {tags, semantic, queries}`:

$$
S_{\text{RRF}}(d) = \sum_{c} \frac{1}{k + \text{rank}_c(d)}
$$

with `k = 60` (the paper-standard constant) and `rank_c(d)` the 1-indexed position of `d` after sorting the candidate pool by `score_c` descending. `score_composite` is then normalised to `[0, 1]` by dividing by `max(S_RRF)` in the pool.

Concretely:

1. **`config/scoring.yaml`** gains a `composite_score:` block:
   ```yaml
   composite_score:
     method: rrf   # rrf | weighted_mean
     rrf_k: 60
   ```
   The legacy `weights:` block remains in the file but is only consulted when `method: weighted_mean`.
2. **`plan_02` §7.4** is rewritten with the RRF pseudocode and the calibration-deferred note.
3. **Dashboard `/inbox`** adds per-criterion sort controls. `score_composite` is still the default sort, but a user who wants to see "top by queries" independently can flip the sort without losing the RRF-ranked view.
4. **The `scoring_explanation` JSON** stored per candidate (plan_02 §5) gains a `rrf_ranks` field recording the rank of that candidate in each criterion at scoring time. This is what Sprint 3's UI breakdown visualisation reads; it is also the input a future calibration ADR will feed into logistic regression.
5. **Calibration path**. Once `candidates.db` has ≥100 decisions with both `accepted`/`rejected` outcomes and the ranked-per-criterion data stored above, a successor ADR can compare RRF to a weighted-mean calibrated via logistic regression on `{score_tags, score_semantic, score_queries} → outcome`. Until then, RRF is the default and the weighted-mean option exists only for users who want to A/B compare.

## Consequences

### Positive

- **No pre-data weight tuning.** RRF eliminates the `w_t, w_s, w_q` pre-spec guesswork. The only knob is `k`, and the paper-standard `k = 60` is appropriate across wildly different retrieval corpora; the user rarely needs to touch it.
- **Ortogonales surgen.** A paper that ranks #1 on `queries` and #500 on both `tags` and `semantic` still gets `1/61 + 1/560 + 1/560 ≈ 0.0200`, which is ~40% of the score of a paper that ranks #1 on all three (`3/61 ≈ 0.0492`). That's visible in the inbox rather than buried. The user's persistent query is not punished for being out-of-distribution from their existing library.
- **Robusto a distribuciones distintas.** RRF uses ranks, not raw scores, so wild differences in scale across criteria (tags might max out at 0.6, semantic at 0.95, queries at 0.85) don't silently bias the ranking. Weighted-mean requires per-criterion normalisation to avoid this; RRF gets it for free.
- **Estandar de la industria.** Hybrid retrieval systems (learned-sparse + dense in modern search; BM25 + dense in pyserini, Vespa, Elastic) use RRF as the canonical fusion method. We gain a well-understood default instead of inventing one.
- **Camino claro a calibración.** "No weights" is a real selling point *today* (pre-data); "weighted with calibrated coefficients" is the eventual story *when the data exists*. Keeping both methods in the config makes the transition an ADR update and an A/B run, not a rewrite.

### Negative

- **Rank-based ≠ score-based.** RRF throws away the actual score values — a paper with `score_semantic=0.95` and one with `score_semantic=0.85` rank adjacent, and their difference in `score_composite` is a single slot of rank, not the 0.10 gap in the underlying similarity. When the underlying score *is* well-calibrated and discriminative, weighted-mean might eke out marginally better rankings. For our case (three scores of uncertain calibration, especially at start), this is a feature, not a bug — but it's worth naming.
- **Needs the full candidate pool at scoring time.** Ranks are relative to the pool, so you cannot compute `score_composite` one candidate at a time. The worker already processes a batch per cycle (`run_fetch_cycle` accumulates all new candidates before scoring), so this matches the existing flow — but a future streaming scoring design would need to revisit it.
- **UI needs to explain RRF.** "Composite score" is intuitive as a weighted-mean; "a number derived from ranks across three criteria" takes one extra sentence in the `/inbox` onboarding note. The breakdown-per-criterion visualisation (planned for Sprint 3) does most of the explaining visually.

### Neutral

- **Weighted-mean stays in the config**. Not as the default, but available. A user who wants to tune weights manually during a calibration experiment can flip `method: weighted_mean` and set `weights.tags / semantic / queries` directly. No code removal; the implementation branches on `method`.
- **`k = 60` is not load-bearing.** The whole point of RRF is that `k` absorbs noise; `k ∈ [10, 100]` typically produce indistinguishable rankings for small-to-medium pools. We lock to 60 for reproducibility.

## Alternatives considered

**A. Keep the weighted-mean and pick better weights.**
Rejected. There are no "better weights" without decision data. Picking weights pre-data is guessing, and any guess has the ortogonal-signals-destruction problem — the only remedy is a weight vector so lopsided that one criterion effectively dominates, which defeats the point of having three.

**B. Use `max()` instead of a weighted mean.**
Considered seriously. `max(S_tags, S_sem, S_q)` has the same "any criterion wins" property as RRF without the rank machinery. Rejected because `max` does not distinguish between "ranks #1 on one criterion" and "ranks #1 on three criteria" — the latter is strictly stronger evidence and should rank higher. RRF preserves that ordering. Cheap enough to have both.

**C. Learn a ranker from the first 100 decisions, then switch.**
Rejected as the initial default — this IS the learning-loop path that plan_02 §3 defers to v1.1, and it does not solve the "what to do before 100 decisions exist" problem. RRF is the *pre-learning* default; a calibrated ranker is the *post-data* successor.

**D. CombSUM / CombMNZ (other fusion methods).**
Rejected. CombSUM sums raw scores (suffers the same calibration problem as weighted-mean); CombMNZ multiplies by the number of criteria that matched, which is similar in spirit to RRF but less standard and less forgiving of zero scores. RRF is the industry default for a reason.

**E. Just use `score_semantic` alone and drop the other two.**
Rejected. `score_semantic` degrades to `neutral_fallback=0.5` when ChromaDB is below `min_corpus_size` (ADR 015, plan_02 §7.2). Pre-backfill, the system would rank everything identically. The three-criterion design exists so S2 has signal even before ChromaDB is populated.

## References

- `docs/plan_02_subsystem2.md` §7 — the three per-criterion scores
- `docs/plan_02_subsystem2.md` §7.4 — composite score (rewritten to use RRF)
- `docs/plan_02_subsystem2.md` §3 — anti-objective "no learning loop en v1"; this ADR explains why RRF is *not* learning loop
- `config/scoring.yaml` — `composite_score.method` and `rrf_k`
- Cormack, G. V.; Clarke, C. L. A.; Büttcher, S. (2009). "Reciprocal rank fusion outperforms Condorcet and individual rank learning methods." SIGIR 2009. Canonical reference for RRF with `k = 60`.
- [Elastic Search: Reciprocal rank fusion](https://www.elastic.co/guide/en/elasticsearch/reference/current/rrf.html) — a modern industry implementation using the same formula.
