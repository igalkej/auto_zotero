# Economics

Costos esperados del pipeline por etapa, qué los controla, y cómo los
hard caps en `.env` los refuerzan. Números de enero 2026; actualizar
al cambio de precios de OpenAI.

---

## 1. Resumen — corpus típico (1000 PDFs, 70 % anglo / 30 % LATAM)

| Concepto | Estimado | Hard cap (default) | Env var |
|---|---|---|---|
| **Stage 01** — LLM gate (clasificador académico) | ~$0.12 | $1.00 | `MAX_COST_USD_STAGE_01` |
| **Stage 02** — OCR | $0 | — | — (tesseract local, sin costo marginal) |
| **Stage 03** — Import vía OpenAlex | $0 | — | — (API gratis) |
| **Stage 04a-c** — Cascade gratis | $0 | — | — (APIs gratis + rate limits) |
| **Stage 04d** — LLM extraction | ~$0.40 | $2.00 | `MAX_COST_USD_STAGE_04` |
| **Stage 05** — Tagging LLM | ~$0.40 | $1.00 | `MAX_COST_USD_STAGE_05` |
| **Stage 06** — Validation | $0 | — | — (read-only) |
| **S2 backfill inicial** — embeddings sobre la biblioteca completa | ~$1.50 | $3.00 | `S2_MAX_COST_USD_BACKFILL` |
| **S2 worker** — scoring mensual | ~$2.00/mes | $5.00/mes | `S2_MAX_COST_USD_MONTHLY` |
| **Total S1 típico** | **~$1.00** | $10.00 | `MAX_COST_USD_TOTAL` |

**S1 one-shot típico: menos de $1**. El cap duro del total (`MAX_COST_USD_TOTAL=10.00`)
es deliberadamente laxo para absorber corpora atípicos sin abortar; los
caps por etapa son los que efectivamente rigen.

---

## 2. Detalle por etapa

### Stage 01 — LLM gate

Sólo los PDFs **ambiguos** (no claramente académicos ni claramente
descarte) llegan al LLM. Las tres ramas del clasificador (plan_01
§3.1):

1. **Accept automático** (zero cost) — DOI / arXiv / ISBN / keywords
   académicos en páginas 1-3. Suele resolver 70-80 % del corpus.
2. **Reject automático** (zero cost) — ≤ 2 páginas + keywords de
   facturación / documento personal. Suele resolver 5-10 %.
3. **LLM gate** — el resto. `gpt-4o-mini` con prompt de ~500 chars
   + 1 retry. ~$0.0004 por PDF.

Para 1000 PDFs con ~20 % ambiguos: 200 × $0.0004 = **$0.08**. En
corpus LATAM-heavy (más variedad) hasta **$0.20**. Cap default:
$1.00.

### Stage 02 — OCR

Tesseract local vía `ocrmypdf`. Costo = tiempo CPU (≈ 10-30s/PDF
dependiendo del escaneo; paralelizable con `OCR_PARALLEL_PROCESSES`).
No hay costo de API.

### Stage 03 — Import

OpenAlex es gratuito con `USER_EMAIL` en el User-Agent (el "polite
pool", 100 req/s vs 10). Zotero local API es gratis. Costo = $0.

### Stage 04a-c — Cascade gratis

- **04a**: regex + OpenAlex por DOI. Gratis.
- **04b**: OpenAlex por búsqueda de título. Gratis.
- **04c**: Semantic Scholar por búsqueda de título. Gratis (rate limit
  100 req / 5 min sin key, 1 req/s con key).

Estas tres ramas suelen resolver 80-90 % del fallthrough de Stage 03
en corpus anglo-dominantes. Para LATAM-heavy cae a 40-60 %, lo que
empuja más items a Stage 04d.

### Stage 04d — LLM extraction

Sólo los items que sobrevivieron 04a-c van al LLM. `gpt-4o-mini` con
primeras 2 páginas + prompt estructurado + 1 retry. ~$0.0004 por item.

**Estimación anglo-dominante**: 10-20 % del corpus × 1000 PDFs ×
$0.0004 = **$0.04-0.08**.

**Estimación LATAM-heavy** (plan_01 §3 Etapa 04 "Aviso"): 40 % del
corpus × 1000 × $0.0004 = **$0.16**, pero con ruido de retries
malformados y papers largos puede llegar a **$1.00**. Subí el cap a
`MAX_COST_USD_STAGE_04=4.00` si tu corpus es LATAM-pesado.

Cuando el cap trip, el orchestrator de `run-all` rutea los items
restantes directo a 04e (quarantine) — no vuelve a llamar al LLM.
Ver `docs/troubleshooting.md` §5 para recuperar items
quarantinados después de subir el cap.

### Stage 05 — Tagging

Un call al LLM por ítem elegible (con metadata + no quarantinado +
aún sin tags). ~$0.0004 por ítem.

**Estimación**: 1000 items × $0.0004 = **$0.40**. Cap default $1.00.

`--preview` mode escribe el CSV sin tocar Zotero ni cobrar por tags
que descartás. Buena práctica: `--preview` primero, revisar el CSV,
después `--apply`.

### Stage 06 — Validation

Read-only. No API calls. $0.

---

## 3. S2 (prospective capture — pendiente de implementación)

### Backfill inicial (`zotai s2 backfill-index`)

Un embedding por ítem de la biblioteca. `text-embedding-3-large` a
$0.00013 por 1K tokens. Un abstract medio es ~250 tokens ~
$0.000033 por ítem.

**Estimación**: 1500 items × $0.000033 = **$0.05**. Pero la ADR 015
cap default es `S2_MAX_COST_USD_BACKFILL=3.00` para absorber retries
+ re-embeds cuando el texto fulltext está disponible.

### Worker mensual

Cada candidate cuesta ~$0.0005 en embeddings + scoring. 30
candidates × 4 ciclos × 30 días = **~$1.80/mes**. Cap default
`S2_MAX_COST_USD_MONTHLY=5.00`.

---

## 4. Cómo se enforcen los caps

`zotai.api.openai_client.OpenAIClient` mantiene un ledger de `spent_usd`
que se incrementa después de cada call exitoso. Antes de cada call,
`_check_budget()` tira `BudgetExceededError` si `spent + projected >
budget`.

- **Stage 01**: el cap es un override del call-site: el clasificador
  construye su propio `OpenAIClient(budget_usd=max_cost or MAX_COST_USD_STAGE_01)`.
- **Stage 04d**: idem.
- **Stage 05**: idem.

Los caps son por-stage, no acumulativos entre stages. El cap total
`MAX_COST_USD_TOTAL` no se enforza automáticamente en v1 — es más
que nada un budget mental. Si querés enforce real del total, bajá
los caps individuales.

---

## 5. Claude Desktop (S3)

Claude Pro es $20/mes — requerido para usar el MCP server. No hay
costo marginal por query (Anthropic lo incluye en la suscripción).

---

## 6. Optimizar costos

- Correr Stage 01 con `--skip-llm-gate` si tenés confianza en que tu
  corpus está limpio de no-académicos (ahorra los ~$0.12 del gate).
- Correr Stage 04d con `--substage 04d` aislado después de 04a-c
  cuando querés monitorear el gasto del LLM específicamente.
- `--preview` antes de `--apply` en Stage 05 — el costo es el mismo
  (un call por ítem), pero preview no compromete las tags a Zotero
  si no te gustan.
- Usar `text-embedding-3-small` en lugar de `-large` para S2 corta los
  costos ~6× pero degrada el recall multilingüe ~10 pts (ver ADR
  004). Probablemente no vale la pena.

---

## 7. Referencias

- `.env.example` — todos los caps con comentario explicativo.
- `docs/plan_01_subsystem1.md` §3 — detalles por etapa, budgets.
- `docs/decisions/005-gpt-4o-mini-tagging-extraction.md` — por qué
  `gpt-4o-mini` como default.
- `docs/decisions/004-openai-text-embedding-3-large.md` — por qué
  `text-embedding-3-large`.
- `src/zotai/api/openai_client.py` — pricing table + ledger.
