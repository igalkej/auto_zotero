# ADR 005 — `gpt-4o-mini` as the LLM for tagging and extraction

**Status**: Accepted
**Date**: 2026-04-23
**Deciders**: project owner
**Supersedes**: —
**Superseded by**: —

---

## Context

Three tasks in the pipeline need an LLM:

- **Stage 01 classifier** (Rama 3 of the academic / non-academic gate,
  plan_01 §3.1). One JSON-mode call per ambiguous PDF; the prompt is a
  page-1 snippet and a page-count hint; the response is
  `{"is_academic", "confidence", "reason"}`.
- **Stage 04d — LLM extraction** (plan_01 §3 Etapa 04d). One JSON-mode
  call per item that fell through 04a/b/c; the prompt is the first two
  pages of the PDF; the response is a schema of
  `{title, authors, year, item_type, venue, doi, abstract}` that we
  validate against a Pydantic model and map to a Zotero payload.
- **Stage 05 — tagging** (plan_01 §3 Etapa 05, not yet implemented).
  One JSON-mode call per item; the prompt is the paper metadata + the
  taxonomy; the response is `{tema: [...], metodo: [...]}`.

All three share the same pattern: **short prompt, structured JSON
response, zero conversational context**. The quality ceiling is
"recover metadata that's already in the text" or "map an abstract to a
fixed taxonomy", not "reason from weak signals".

Three candidate models were considered:

- `gpt-4o-mini` — small, cheap, JSON mode, long context.
- `gpt-4o` — strongest model in the family; much higher cost.
- Open-weights local model (e.g. Llama-3.1-8B via Ollama) — zero
  marginal cost but substantial setup + maintenance burden, and
  the researcher's laptop is the expected deployment environment
  (not a GPU server).

## Decision

**Use `gpt-4o-mini` as the default for all three JSON-mode tasks.**

Encoded in `.env.example` as `OPENAI_MODEL_TAG=gpt-4o-mini` and
`OPENAI_MODEL_EXTRACT=gpt-4o-mini`, mirrored in
`OpenAISettings.model_tag` / `model_extract` with the same default.

Budget caps in `.env`:

- `MAX_COST_USD_STAGE_01=1.00` — the Stage 01 classifier.
- `MAX_COST_USD_STAGE_04=2.00` — only 04d uses it (04a/b/c are free).
  **LATAM-heavy corpora** bump this to `~4.00` per plan_01 §3 Etapa 04
  "Aviso — corpus LATAM-heavy".
- `MAX_COST_USD_STAGE_05=1.00` — per-paper tagging (Stage 05, future PR).

`BudgetExceededError` in `zotai.api.openai_client` is raised *before*
the call so no accidental overspend happens. For Stage 04d this error
is caught in the orchestrator: once tripped, the remaining items route
directly to 04e (quarantine) without further LLM calls.

## Consequences

### Positive

- **Cost is small enough to ignore for single-user corpora.** The price
  table in `zotai.api.openai_client._PRICING` (Jan 2026: $0.00015 input
  / $0.0006 output per 1K tokens) puts a 1000-paper S1 run at well
  under $2 for the LLM-using stages combined. Empirical observation
  from Stage 01's classifier on the project owner's own corpus (~1000
  PDFs): **~$0.12 for the LLM gate**, consistent with the pre-data
  estimate.
- **Same client surface across all three stages.** `OpenAIClient` has
  one `_check_budget` / `_charge` ledger that covers everything; the
  caller picks the method (`classify_document` / `extract_metadata` /
  `tag_paper`).
- **JSON mode removes most adversarial parsing.** Each caller still
  validates the response (Pydantic schema for 04d; allow-list check
  for 05's tag IDs), but the shape of the response is never "free-form
  prose the user has to prompt-engineer around".
- **Quality is enough for the job.** On 04d's task ("extract metadata
  already present in the first two pages") `gpt-4o-mini` converges to
  near the ceiling — the failure mode is almost always "the text
  isn't in the pages" (bad scan, missing front matter), not "the
  model couldn't read good text". On 01's classifier the task is
  trivially above `gpt-4o-mini`'s quality floor.

### Negative / Costs assumed

- **Vendor lock-in on the LLM path.** If OpenAI raises prices >2× or
  changes the JSON-mode contract, three stages need to migrate. We
  accept this by keeping `OpenAIClient` narrow (~200 lines) so a
  swap to an OpenAI-compatible provider (Together, Groq, local vLLM
  with OpenAI shim) is mechanical.
- **No use of `gpt-4o` for 04d's "hard" items.** When 04d fails we
  don't escalate to a bigger model — the item goes to quarantine (ADR
  008). Rationale: escalation doubles the per-item cost for a
  population of items where the bottleneck is PDF quality, not model
  quality. Revisit if empirical data shows a large fraction of
  quarantine items whose metadata is in the text but `gpt-4o-mini`
  misread it.
- **Quality drift on price updates.** OpenAI sometimes reprices models.
  `_PRICING` in `zotai/api/openai_client.py` hardcodes Jan 2026
  numbers; update with each release cycle (acknowledged in the module
  docstring).

## Alternatives considered and discarded

**A. `gpt-4o` as the default.** Discarded. ~16× the cost of
`gpt-4o-mini` for a quality improvement that is not measurable on
these three JSON tasks. If anything, the observed failure mode is
"the information was not in the text we sent" — a larger model
doesn't help.

**B. `gpt-3.5-turbo`.** Discarded. Cheaper than `gpt-4o-mini` in Jan
2025 pricing but OpenAI has effectively deprecated it for new
deployments and JSON mode on it is less reliable. The cost delta
against `gpt-4o-mini` is small enough to not matter for our volumes.

**C. Local open-weights (Llama-3.1-8B-Instruct via Ollama).** Discarded
**for v1**. Pros: zero marginal cost, full data control (relevant for
the paper-contents-in-prompt case in 04d). Cons: (a) the researcher's
deployment is their laptop, not a GPU server — CPU inference on an
8B model is slow enough to stretch Stage 04d's walk time from minutes
to hours; (b) Ollama installation + model download is additional
onboarding surface for a user who already has to set up Docker,
Zotero, and an OpenAI key; (c) JSON-mode reliability on 7-8B models
is not at the level `gpt-4o-mini` offers out of the box. Revisit if
(i) the owner's next laptop has a capable GPU and (ii) a future
release of Ollama / llama.cpp lands a comparable structured-output
mode.

**D. Separate models for 04d and the other stages.** Discarded. A
single default minimises configuration surface and reasoning about
cross-stage cost budgeting. If 04d later needs a stronger model, a
separate ADR can add that branch.

## Observed costs (reference)

Measured on the project owner's corpus after merging PR #48 (Stage
04a) and before the full Stage 04 cascade:

- Stage 01 classifier: **$0.12** on ~1000 PDFs (below the $1.00 cap).
- Stage 04d: not yet measured (lands with PR #50's successor).
  Pre-data estimate from plan_01: ~$0.40 anglo / ~$1.60 LATAM-heavy
  on a 1000-paper corpus. To be updated here once the first real run
  lands.

## Relation to other ADRs

- **ADR 004** (`text-embedding-3-large` for embeddings) — orthogonal:
  this ADR covers chat models, 004 covers embedding models. Both
  together are captured in `OpenAISettings`.
- **ADR 008** (Quarantine in S1) — complementary: 04d's "give up and
  quarantine" path is the reason we don't need a stronger / more
  expensive model to squeeze a few more items out of the cascade.
