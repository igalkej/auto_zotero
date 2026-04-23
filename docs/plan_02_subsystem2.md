# plan_02_subsystem2.md — Subsistema 2: Captura prospectiva

**Estado**: Spec cerrada, implementación tras S1 y S3.
**Estimación**: 2-3 semanas de desarrollo incremental (4 sprints).
**Complejidad relativa**: alta. Mezcla scheduled jobs, UI web, múltiples APIs externas, scoring compuesto.

---

## 1. Propósito del subsistema

Mantener la biblioteca Zotero sincronizada con el estado del arte de los temas del investigador, mediante:

- Ingesta automática de feeds RSS de journals elegidos por el usuario.
- Filtrado por múltiples criterios de relevancia (tags, similitud semántica, queries persistentes).
- Dashboard web para triage semanal: aceptar / descartar / posponer candidatos.
- Push automático a Zotero de los aceptados, con todo el procesamiento del S1 aplicado.

**Este es el corazón del producto.** S1 es el arranque, S3 es el acceso, S2 es lo que justifica el proyecto completo.

---

## 2. Criterios de éxito

- **Precision del filtro**: ≥50% (de los candidatos que muestra, al menos la mitad valen la pena mirar).
- **Volumen**: 10-30 candidatos/semana sostenidamente. Si excede 50/semana, recalibración obligatoria.
- **Tiempo humano**: 15-20 min/semana en triage.
- **A 90 días**: >5 papers/mes agregados vía S2 que no se hubieran encontrado por canales tradicionales.

---

## 3. Anti-objetivos

- **No es Google Scholar Alerts reemplazo**: no scrapeamos Google Scholar.
- **No es un feed reader general**: solo journals académicos con RSS estándar.
- **No tiene learning loop en v1**: no ajusta el filtro basándose en decisiones previas del usuario. Posponemos explícitamente por riesgo de cámara de eco.
- **No es social**: no comparte decisiones entre usuarios.
- **No auto-aprueba**: todo candidato pasa por ojo humano.

---

## 4. Arquitectura

```
┌───────────────────────────────────────────────────────────────┐
│ Worker (scheduled, corre c/ N horas)                          │
│  ┌──────────────────────────────────────────────────────┐     │
│  │ 0. Reconcile ChromaDB (add missing, remove orphans)  │     │
│  │    Mantiene el invariante: todo item no-cuarentenado │     │
│  │    en Zotero tiene entrada en ChromaDB. Ver ADR 015. │     │
│  │ 1. Fetch RSS de journals configurados                │     │
│  │ 2. Parsear, deduplicar vs candidatos ya vistos       │     │
│  │ 3. Para cada nuevo candidato:                        │     │
│  │    a. Enriquecer metadata (DOI → OpenAlex)           │     │
│  │    b. Calcular scores por criterio                   │     │
│  │    c. Score compuesto                                 │     │
│  │ 4. Persistir en candidates.db con status='pending'   │     │
│  └──────────────────────────────────────────────────────┘     │
└───────────────────────────────────────────────────────────────┘
                              │
                              ▼
                   ┌──────────────────────┐
                   │  candidates.db       │
                   │  (SQLite local)      │
                   └──────────────────────┘
                              │
                              ▼
┌───────────────────────────────────────────────────────────────┐
│ Dashboard (FastAPI + HTMX, localhost:8000)                    │
│  ┌──────────────────────────────────────────────────────┐     │
│  │ /inbox       — queue de pendientes, triage           │     │
│  │ /history     — decisiones pasadas                    │     │
│  │ /config      — journals, queries, tags, weights      │     │
│  │ /metrics     — precision observada, volumen          │     │
│  └──────────────────────────────────────────────────────┘     │
└───────────────────────────────────────────────────────────────┘
                              │
                              ▼ (user accepts N items)
                   ┌──────────────────────┐
                   │  Push module         │
                   │  → Zotero API        │
                   │  → PDF download      │
                   │  → Tag + collection  │
                   └──────────────────────┘
                              │
                              ▼
                         Biblioteca Zotero
                         del investigador
```

---

## 5. Modelo de datos

### candidates.db (SQLite, separada de state.db del S1)

```python
class Candidate(SQLModel, table=True):
    id: str = Field(primary_key=True)  # hash del DOI o URL
    source_feed_id: str = Field(foreign_key="feed.id")
    doi: Optional[str] = None
    title: str
    authors_json: str  # JSON list
    abstract: Optional[str] = None
    venue: str
    published_at: datetime
    url: Optional[str] = None

    # Scoring
    score_tags: float = 0.0           # 0-1
    score_semantic: float = 0.0        # 0-1
    score_queries: float = 0.0         # 0-1
    score_composite: float = 0.0       # 0-1
    scoring_explanation: str  # JSON blob explicando por qué cada score

    # Triage
    status: str = "pending"  # pending, accepted, rejected, deferred
    decided_at: Optional[datetime] = None
    decided_by: Optional[str] = None  # usuario (futuro multi-user)
    decision_note: Optional[str] = None

    # Zotero integration
    zotero_item_key: Optional[str] = None  # si accepted y pushed
    pushed_at: Optional[datetime] = None

    created_at: datetime
    updated_at: datetime


class Feed(SQLModel, table=True):
    id: str = Field(primary_key=True)  # slug del journal
    name: str
    rss_url: str
    issn: Optional[str] = None
    active: bool = True
    last_fetched_at: Optional[datetime] = None
    last_fetch_status: Optional[str] = None
    items_fetched_total: int = 0


class PersistentQuery(SQLModel, table=True):
    id: int = Field(primary_key=True)
    query_text: str
    active: bool = True
    weight: float = 1.0  # para scoring compuesto
    created_at: datetime


class TriageMetric(SQLModel, table=True):
    id: int = Field(primary_key=True)
    week_start: date
    candidates_shown: int
    candidates_accepted: int
    candidates_rejected: int
    candidates_deferred: int
    precision_observed: float  # accepted / (accepted + rejected)
```

**Nota — ChromaDB no está en `candidates.db`**. Bajo ADR 015, S2 también
es el owner del índice de embeddings persistido en ChromaDB
(`/workspace/chroma_db`). Los embeddings de los items de la biblioteca
viven ahí, no en `candidates.db`. El campo `score_semantic` de
`Candidate` es solo el float derivado de la query del candidate contra
ese índice. La lógica de escritura (embedding + upsert) y de
mantenimiento del invariante vive en
`src/zotai/s2/indexing.py` (módulo dedicado, ver §11 Sprint 1) y se
documenta como contrato de schema en ADR 015 §6.

**Nota — tabla virtual FTS5 para hybrid query scoring** (ADR 017).
Además de las tablas listadas arriba, `candidates.db` contiene una
tabla virtual `candidate_fts(id, title, abstract)` construida con
`fts5(... tokenize='unicode61 remove_diacritics 2')` y mantenida en
sync vía triggers INSERT/UPDATE/DELETE sobre `Candidate`. Es la que
responde la mitad BM25 del score híbrido en §7.3. Zero mantenimiento
del usuario; los triggers se crean junto con el schema al
`init_s2()`.

---

## 6. Sub-módulo: Feed ingestion

**Archivo**: `src/zotai/s2/feeds.py`

**Responsabilidades**:
- Leer configuración de feeds desde `config/feeds.yaml`.
- Fetch RSS via `feedparser`.
- Parsear entries, extraer metadata (DOI, title, authors, abstract).
- Deduplicar contra candidatos ya vistos (por DOI si disponible, por URL si no).
- Enriquecer metadata vía OpenAlex (abstract si el RSS no lo trae).

**Formato de `config/feeds.yaml`**:

```yaml
feeds:
  - id: "aer"
    name: "American Economic Review"
    rss_url: "https://www.aeaweb.org/aer/rss"
    issn: "0002-8282"
    active: true

  - id: "jep"
    name: "Journal of Economic Perspectives"
    rss_url: "https://www.aeaweb.org/jep/rss"
    active: true

  - id: "qje"
    name: "Quarterly Journal of Economics"
    rss_url: "https://academic.oup.com/qje/rss"
    active: true

  # ...
```

**Edge cases**:
- RSS retorna 200 pero XML malformado: logear, dejar `last_fetch_status='malformed'`, intentar next run.
- DOI no extraíble del RSS: usar URL como fallback, hacer HEAD request al URL para ver si hay DOI en Link headers.
- Feed pseudo-RSS que trae ToC entero en un solo item: configuración especial por feed (patrón de parsing custom).

---

## 7. Sub-módulo: Scoring

**Archivo**: `src/zotai/s2/scoring.py`

Cada criterio produce un score `[0, 1]`. El score compuesto es combinación ponderada.

### 7.1 Score por tags (criterio más simple, primer sprint)

**Input**: candidate con `title + abstract`, usuario's tag vocabulary del S1.
**Output**: `score_tags ∈ [0, 1]`.

**Lógica**:
1. Extraer "tags candidatos" del paper usando `gpt-4o-mini` con la misma taxonomía del S1.
2. Calcular overlap con tags frecuentes en la biblioteca del usuario (top 30 tags por count).
3. `score = |overlap| / |extracted_tags|`, clippeado a [0, 1].

**Alternativa más barata (sin LLM)**: keyword matching sobre abstract contra un vocabulary derivado de tags existentes. Más rápido, menos preciso. Implementar ambas, dejar la elección configurable.

### 7.2 Score semántico (sprint 3)

**Input**: candidate con `abstract`.
**Output**: `score_semantic ∈ [0, 1]`.

**Lógica**:
1. Calcular embedding del `abstract` del candidate (OpenAI `text-embedding-3-large`, ~$0.00013/candidate).
2. Query contra ChromaDB con `top_k = semantic_scoring.top_k_corpus` (default 20).
3. `score = mean(similarity_scores de los top-k)`.
4. Normalizar: si la biblioteca tiene N papers y el candidate matchea fuerte con ≥10% de los papers temáticamente cercanos, score alto.

**Implementación**:
- Bajo ADR 015, ChromaDB es **mantenida por el propio S2** via reconciliación en cada ciclo del worker (paso 0 del diagrama §4). El path es `S2_CHROMA_PATH=/workspace/chroma_db`, montado read-write desde el host.
- Si ChromaDB tiene **menos de `semantic_scoring.min_corpus_size` documentos** (default 50, configurable en `config/scoring.yaml`), `score_semantic = semantic_scoring.neutral_fallback` (default 0.5) sin intentar la query k-NN. Este caso solo se da si S1 nunca corrió, o si el primer `zotai s2 backfill-index` todavía no se ejecutó. En régimen normal el invariante de reconciliación mantiene el índice poblado y este fallback no se activa.
- El threshold por count (en lugar de "está vacía") cubre el caso de bibliotecas chiquitas — un k-NN sobre 5 papers no es ruido informativo, mejor degradar.

### 7.3 Score por queries persistentes

**Input**: candidate, lista de `PersistentQuery`s activas.
**Output**: `score_queries ∈ [0, 1]`.

**Método: hybrid retrieval (BM25 + dense) por query** (ver ADR 017). Las queries persistentes suelen ser frases cortas (*"fiscal multipliers in emerging markets"*, *"informalidad laboral Argentina"*); en queries de ≤5 tokens la similitud puramente densa degrada — es un problema conocido de retrieval. La fusión convex de BM25 (lexical, exact-match friendly) + dense (semántico, paráfrasis friendly) mejora recall dramáticamente. SQLite tiene FTS5 built-in, así que no hace falta nueva dep.

**Lógica** (para cada query activa `q` → candidate `c`):

1. **BM25**: `SBM25(c, q) = bm25(candidate_fts, q)` sobre una tabla virtual `candidate_fts(id, title, abstract)` en `candidates.db` (FTS5). Normalizar a `[0,1]` con min-max sobre el batch del ciclo.
2. **Dense**: `Sdense(c, q) = cos(embedding(q), embedding(c.abstract))`. `embedding(q)` se cachea por query para no re-embebera en cada ciclo. Normalizar el coseno de `[-1, 1]` a `[0, 1]` con `(cos + 1) / 2`.
3. **Fusión**: `Squery(c, q) = α · SBM25 + (1 − α) · Sdense`, con `α = query_scoring.bm25_weight` (default 0.4, configurable en `config/scoring.yaml` y anulable per-run con `S2_QUERY_BM25_WEIGHT`).
4. **Agregación sobre múltiples queries**: `score_queries(c) = max(Squery(c, q) por query q activa) * PersistentQuery.weight_q` con default `weight_q=1.0`. El `max` preserva la semántica "basta con matchear *una* query para ser relevante" que el spec original ya preveía.
5. **Si no hay queries activas**: `score_queries = 0`.

**Calibración de α**. La literatura (Pyserini, Elastic hybrid, LangChain EnsembleRetriever) sugiere `α ∈ [0.3, 0.5]` como punto de partida; 0.4 es razonable pre-datos. Igual que RRF (ADR 016), calibrar α con regresión logística queda para un ADR sucesor una vez que `candidates.db` acumule ≥100 decisiones con breakdowns por componente.

**FTS5 setup** (detalle de implementación, se persiste en `candidates.db`):

```sql
CREATE VIRTUAL TABLE candidate_fts USING fts5(
    id UNINDEXED,
    title,
    abstract,
    tokenize = 'unicode61 remove_diacritics 2'  -- útil para español
);

-- Triggers para mantenerla sincronizada con la tabla Candidate.
CREATE TRIGGER candidate_fts_insert AFTER INSERT ON candidate BEGIN
    INSERT INTO candidate_fts(id, title, abstract) VALUES (new.id, new.title, new.abstract);
END;
-- (update + delete triggers análogos).
```

`remove_diacritics 2` hace que "política" matchee "politica" — útil para corpus mixto es/en donde los acentos son inconsistentes.

### 7.4 Score compuesto

```python
def composite_score(c: Candidate, weights: Weights) -> float:
    return (
        weights.tags * c.score_tags
        + weights.semantic * c.score_semantic
        + weights.queries * c.score_queries
    ) / (weights.tags + weights.semantic + weights.queries)
```

**Weights default** (configurable en `config/scoring.yaml`):
- `tags=1.0`
- `semantic=2.0` (más importante, más discriminador)
- `queries=2.0`

---

## 8. Sub-módulo: Dashboard

**Tecnología**: FastAPI + Jinja2 templates + HTMX + Tailwind (via CDN, cero build step).

**Decisión**: HTMX sobre React/Vue. Razones:
- Un solo developer mantiene esto.
- El estado real vive en SQLite, no en el browser.
- Cero build step, cero bundle, cero npm.
- Interacciones son CRUD simples, no SPA.

### 8.1 Rutas

| Ruta | Método | Propósito |
|---|---|---|
| `/` | GET | Redirect a `/inbox` |
| `/inbox` | GET | Lista de candidates `status=pending`, ordenados por `score_composite DESC` |
| `/inbox/item/{id}` | GET | Detalle: abstract, scoring_explanation, PDF preview si disponible |
| `/inbox/item/{id}/accept` | POST | Marca accepted + dispara push a Zotero (async) |
| `/inbox/item/{id}/reject` | POST | Marca rejected |
| `/inbox/item/{id}/defer` | POST | Marca deferred |
| `/history` | GET | Historial de decisiones, filtrable |
| `/config/feeds` | GET/POST | Ver/editar lista de feeds |
| `/config/queries` | GET/POST | Ver/editar persistent queries |
| `/config/weights` | GET/POST | Ajustar pesos del scoring compuesto |
| `/metrics` | GET | Dashboard de precision, volumen, tendencia |
| `/worker/run-now` | POST | Dispara un fetch manual (para testing) |
| `/healthz` | GET | Health check |

### 8.2 UX del inbox

**Layout**:
- Lista vertical de cards.
- Cada card muestra:
  - Título (click abre detalle)
  - Autores, venue, año
  - Score compuesto (barra visual) + breakdown por criterio (mini barras)
  - Razón principal del match (ej. "Matches query 'fiscal multiplier'")
  - Botones: ✓ Accept / ✗ Reject / ⏸ Defer
  - Link externo al paper
- Keyboard shortcuts: `a`/`r`/`d` sobre el card focused.
- Bulk actions: checkboxes + acción masiva en toolbar.

**Filtros**:
- Por score range.
- Por feed de origen.
- Por semana (`published_at`).

### 8.3 Autenticación

**Para v1**: ninguna. Localhost-only, el dashboard bind a `127.0.0.1:8000`.

**Si en el futuro se expone a red**: Basic Auth con password en `.env`, forzar HTTPS.

---

## 9. Sub-módulo: Worker

**Archivo**: `src/zotai/s2/worker.py`

**Tecnología**: APScheduler in-process es el default; cron / Task Scheduler es la alternativa para usuarios que no mantienen el dashboard corriendo 24/7. Ambos caminos invocan la misma función `run_fetch_cycle()`. Ver **ADR 012** (`docs/decisions/012-apscheduler-default-cron-alternative.md`) para el detalle de la decisión y la receta de cron.

- **APScheduler default**: `docker compose up dashboard` arranca el scheduler en el mismo proceso que FastAPI. No hay pasos adicionales.
- **Cron alternativo**: setear `S2_WORKER_DISABLED=true` en `.env` y configurar un job del SO que ejecute `docker compose run --rm onboarding zotai s2 fetch-once`. Recetas por OS en `docs/setup-linux.md` y `docs/setup-windows.md` (Phase 9 / #10).
- **Dashboard `/worker/run-now`** dispara un fetch inmediato por background task, independiente del scheduler (plan §8.1).

**Frecuencia default**: cada 6 horas. Configurable via `S2_FETCH_INTERVAL_HOURS`.

**Lógica del job**:
```python
async def run_fetch_cycle():
    # Step 0 — reconcile ChromaDB so scoring queries hit a fresh index.
    # The reconcile is bounded (S2_MAX_EMBED_PER_CYCLE) and safe-guarded
    # for deletes (S2_SAFE_DELETE_RATIO). Errors are logged but do not
    # abort the cycle — score_semantic degrades to neutral_fallback if
    # the corpus_size threshold isn't met after reconcile. See ADR 015.
    reconcile_embeddings(zot_client, chroma_collection, openai_client,
                         max_per_cycle=settings.s2.max_embed_per_cycle,
                         safe_delete_ratio=settings.s2.safe_delete_ratio)

    for feed in get_active_feeds():
        try:
            entries = fetch_and_parse(feed)
            new_candidates = dedup_and_filter(entries)
            for c in new_candidates:
                c.enrich_metadata()
                c.compute_scores()
                c.save()
            feed.last_fetched_at = now()
            feed.last_fetch_status = 'ok'
        except Exception as e:
            feed.last_fetch_status = f'error: {e}'
            log.exception("feed_fetch_failed", feed=feed.id)
        finally:
            feed.save()
```

**Budget**: cada candidato cuesta ~$0.0005 en embeddings + scoring. Para 30 candidates/ciclo × 4 ciclos/día × 30 días = 3600/mes → ~$2/mes. Sumar ~$0.01-0.05/ciclo de reconciliación incremental sobre la biblioteca (típicamente 0-3 items nuevos por ciclo en régimen). El backfill inicial (`zotai s2 backfill-index`) tiene su propio cap `S2_MAX_COST_USD_BACKFILL=3.00`. Ver ADR 015 §8.

**Comando manual `zotai s2 reconcile`**: dispara un solo ciclo de reconciliación sin fetch de RSS — útil para debug, para forzar la propagación de un push reciente, o para usuarios que prefieren disparar el reconcile desde un cron externo independiente del worker. Usa los mismos defaults de `.env` que el ciclo del worker.

---

## 10. Sub-módulo: Push a Zotero

**Archivo**: `src/zotai/s2/push.py`

Se dispara cuando un candidate se marca `accepted`.

**Lógica**:
1. Crear item en Zotero via API con metadata del candidate.
2. Si el candidate tiene DOI o URL con PDF descargable, intentar fetch del PDF.
   Priorizar en orden (detener en primer éxito):
   1. OpenAccess URL de OpenAlex (`best_oa_location.pdf_url`)
   2. DOI resolver (seguir redirect, verificar content-type PDF)
   3. Anna's Archive (búsqueda por DOI/ISBN)
   4. Library Genesis (búsqueda por DOI/título)
   5. Sci-Hub (búsqueda por DOI)
   6. URL del RSS (si sirve PDF directo)

   Cada fuente es configurable via `.env` (`S2_PDF_SOURCES`). **Knobs de control**:
   - `S2_PDF_FETCH_MAX_ATTEMPTS_PER_CANDIDATE` (default 6) — tope de fuentes que se prueban antes de declarar el candidate como `needs-pdf`.
   - `S2_PDF_FETCH_TIMEOUT_SECONDS` (default 30) — timeout por fuente; una fuente lenta no bloquea al worker.
   - `S2_PDF_FETCH_MAX_MINUTES_WEEKLY` (default 20) — budget global wall-clock por semana para fetch de PDFs. Al excederlo, el worker salta el fetch y etiqueta `needs-pdf`; evita que un outage prolongado de Sci-Hub/LibGen queme tiempo del usuario. Se resetea el lunes 00:00 local.

   Si ninguna fuente entrega PDF dentro del budget, el item mantiene el tag `needs-pdf` y queda visible en el dashboard para retry manual.
3. Aplicar tags derivados del scoring (los que mejor matchearon).
4. Mover a colección "Inbox S2" en Zotero. **El push la crea on-demand e idempotentemente** — si no existe, S2 la crea; si ya existe, la usa. El nombre viene de `S2_ZOTERO_INBOX_COLLECTION` (default `Inbox S2`). No se le pide al usuario que la cree a mano.
5. Update del candidate: `zotero_item_key`, `pushed_at`.

**Edge cases**:
- Item ya existe en Zotero (mismo DOI): no duplicar, solo marcar `zotero_item_key` al existente.
- Push falla (API error, red): retry con backoff, eventualmente marcar `push_failed`, mostrar en dashboard.
- Colección `Inbox S2` renombrada por el usuario entre runs: la próxima corrida crea una nueva con el nombre canónico — no buscamos por nombre viejo. Si el usuario quiere renombrar persistentemente, también cambia `S2_ZOTERO_INBOX_COLLECTION` en `.env`.

**Nota — el push no escribe a ChromaDB directamente**. La escritura a ChromaDB ocurre en el siguiente ciclo de reconciliación del worker (paso 0 del diagrama §4 / pseudocódigo §9), que detecta el nuevo item en Zotero y lo embebe. Esto mantiene un único path de escritura a ChromaDB, simple de testear y auditar (ver ADR 015). El precio: hay una ventana de hasta `S2_FETCH_INTERVAL_HOURS` entre el push y la disponibilidad del item en queries semánticas — aceptable para el flujo de S3 (Claude Desktop), donde el usuario raramente consulta sobre papers que aceptó hace minutos.

---

## 11. Roadmap por sprints

### Sprint 1 (3-5 días): Captura bruta + indexación

**Objetivo**: el worker captura de feeds, persiste en DB, el dashboard los muestra sin scoring. **El módulo de indexación (ADR 015) aterriza en este sprint** porque Sprint 2 lo necesita para `score_semantic` ya en su cuna.

- [ ] `config/feeds.yaml` inicial con 5-10 journals.
- [ ] `src/zotai/s2/feeds.py` con RSS parsing.
- [ ] `candidates.db` con schema.
- [ ] `src/zotai/s2/indexing.py` con `reconcile_embeddings()`. Schema de ChromaDB según ADR 015 §6. Tests con ChromaDB temporal.
- [ ] `src/zotai/s2/worker.py` con fetch cycle básico, llamando a `reconcile_embeddings()` en el paso 0 antes del fetch.
- [ ] CLI: `zotai s2 fetch-once`, `zotai s2 backfill-index`, `zotai s2 reconcile`.
- [ ] Dashboard minimal: `/inbox` muestra lista sin scoring ni triage. `/metrics` ya muestra `chroma_docs_count`, `chroma_last_reconcile_at`, `chroma_pending_embeddings`.

**Deliverable**: ver candidatos fluir al inbox tras ejecutar fetch, y la biblioteca poblada en ChromaDB tras `backfill-index`.

### Sprint 2 (3-5 días): Scoring básico + triage

**Objetivo**: scoring por tags + persistent queries funciona, usuario puede aceptar/rechazar.

- [ ] `src/zotai/s2/scoring.py` con scores `tags` y `queries`.
- [ ] Botones accept/reject/defer funcionales.
- [ ] Push a Zotero de aceptados (sin PDF por ahora).
- [ ] `/config/queries` para CRUD de persistent queries.
- [ ] Tests.

**Deliverable**: workflow E2E: fetch → score → triage → push.

### Sprint 3 (3-5 días): Scoring semántico + UX polish

**Objetivo**: criterio de similitud semántica funciona, dashboard usable semanal.

- [ ] `score_semantic` en scoring.py — query contra la ChromaDB que el indexing module mantiene desde Sprint 1 (ADR 015). Ya no hace falta "integración con S3"; la ChromaDB es local a S2.
- [ ] Breakdown visual de scores en cada card.
- [ ] Keyboard shortcuts en inbox.
- [ ] Bulk actions.
- [ ] `/metrics` con precision observada + breakdown por `source` de los embeddings (fulltext / abstract / title_only).
- [ ] PDF download en push (best-effort).

**Deliverable**: sistema usable semanalmente, métricas visibles.

### Sprint 4 (2-3 días): Scheduling + hardening

**Objetivo**: corre solo, con logging, error handling, recovery.

- [ ] APScheduler integrado con el dashboard.
- [ ] Structured logging completo.
- [ ] Error handling robusto en todas las rutas.
- [ ] Docker Compose con service healthcheck.
- [ ] Documentación de uso: `docs/s2-user-guide.md`.
- [ ] Tests end-to-end.

**Deliverable**: S2 listo para uso productivo del usuario.

---

## 12. Configuración via .env (adicional al S1)

```bash
# ──────────── S2 Worker ────────────
S2_FETCH_INTERVAL_HOURS=6
S2_CANDIDATES_DB=/workspace/candidates.db
# Container-side path donde S2 escribe ChromaDB (owner per ADR 015).
# Compose bind-mounts el path del host (ZOTERO_MCP_CHROMA_HOST_PATH) acá
# con flag :rw. Read-only para `zotero-mcp serve` que corre en el host.
S2_CHROMA_PATH=/workspace/chroma_db
# Host-side source para el bind mount. Default coincide con el path que
# `zotero-mcp setup` espera leer. Cambiarlo en .env también requiere
# coordinar con la config de zotero-mcp.
ZOTERO_MCP_CHROMA_HOST_PATH=${HOME}/.config/zotero-mcp/chroma_db
# Cambiar a `true` para deshabilitar APScheduler in-process y usar cron/Task
# Scheduler externo. Ver ADR 012.
S2_WORKER_DISABLED=false
S2_ZOTERO_INBOX_COLLECTION=Inbox S2   # S2 crea la colección on-demand

# ──────────── S2 Index reconciliation (ADR 015) ────────────
# Max embeddings calculados por ciclo del worker. Limita costo y picos de
# latencia; el residual converge en ciclos siguientes.
S2_MAX_EMBED_PER_CYCLE=50
# Safety threshold: si orphans/total > este ratio, NO borrar y requerir
# intervención manual. Protege contra bugs de lectura de Zotero (ej. API
# devuelve lista vacía erróneamente) que vaciarían ChromaDB por diff.
S2_SAFE_DELETE_RATIO=0.10
# Budget cap para el comando one-shot `zotai s2 backfill-index`. Independiente
# del cap diario/mensual del worker.
S2_MAX_COST_USD_BACKFILL=3.00

# ──────────── S2 Query scoring (ADR 017) ────────────
# Peso de BM25 en la fusión convex con dense: α·BM25 + (1−α)·cos. Default 0.4
# (punto estándar de la literatura para queries cortas). También configurable
# en config/scoring.yaml como `query_scoring.bm25_weight`; esta env var tiene
# prioridad para experimentación per-run. Rango útil: [0.2, 0.6].
S2_QUERY_BM25_WEIGHT=0.4

# ──────────── S2 PDF fetch cascade ────────────
S2_PDF_SOURCES=openaccess,doi,annas,libgen,scihub,rss
S2_PDF_FETCH_MAX_ATTEMPTS_PER_CANDIDATE=6
S2_PDF_FETCH_TIMEOUT_SECONDS=30
S2_PDF_FETCH_MAX_MINUTES_WEEKLY=20

# ──────────── S2 Dashboard ────────────
S2_DASHBOARD_HOST=127.0.0.1
S2_DASHBOARD_PORT=8000

# ──────────── S2 Budgets ────────────
S2_MAX_COST_USD_DAILY=0.50
S2_MAX_COST_USD_MONTHLY=5.00
```

---

## 13. Testing

**Cobertura mínima**: 60%. UI no se testea unitariamente, solo E2E básico.

**Tests críticos**:
- Dedup funciona (mismo DOI no duplica).
- Scoring determinista dado input idéntico.
- Worker se recupera de feed con XML malformado.
- Push a Zotero idempotente.
- Dashboard rutas responden correctamente.

**E2E con Playwright** (opcional, sprint 4): flujo completo de triage.

---

## 14. Fuera de alcance del S2

Posponer explícitamente:
- Learning loop (ajustar weights basado en decisiones históricas).
- Fuentes no-RSS (OpenAlex temático, Semantic Scholar alerts).
- Autores a seguir.
- Notificaciones push (email, Slack).
- Multi-user.
- Dashboard público / deployed en cloud.

Cada una es un ticket para v1.1+.

---

## 15. Dependencias del S2

- **S1** debe haber corrido: necesitamos biblioteca poblada para que el scoring funcione y para que el primer `backfill-index` tenga material que embebera.
- **S2 es owner de ChromaDB** (ADR 015). El primer backfill se dispara con `zotai s2 backfill-index`. Los ciclos siguientes del worker mantienen el invariante "todo item no-cuarentenado en Zotero está indexado" via reconciliación por diff. El bind mount es `:rw` (`/workspace/chroma_db` dentro del container ← `${ZOTERO_MCP_CHROMA_HOST_PATH}` en el host). Ver ADR 011 (mecanismo del mount, amended) + ADR 015 (ownership). Si el corpus indexado tiene <`semantic_scoring.min_corpus_size` documentos, `score_semantic=neutral_fallback` (default 0.5) y el dashboard sigue funcionando.
- **S3 NO es prerequisito de S2** bajo ADR 015. S3 (`zotero-mcp serve`) es lector puro del mismo store. Si el usuario nunca configuró S3, S2 funciona igual; lo único que pierde el usuario es la consulta conversacional desde Claude Desktop.
- **Zotero abierto** con API local (igual que S1). S2 crea la colección `Inbox S2` (o el nombre en `S2_ZOTERO_INBOX_COLLECTION`) on-demand e idempotentemente en el primer push.
- **Worker y dashboard corriendo**: por default APScheduler in-process en el mismo container del dashboard (ADR 012). Usuarios que no mantienen el dashboard 24/7 pueden setear `S2_WORKER_DISABLED=true` y usar cron / Task Scheduler externos invocando `zotai s2 fetch-once` (receta en `docs/setup-{linux,windows}.md`); el `fetch-once` ejecuta el reconcile como paso 0 igual que el worker, así que el invariante de ChromaDB se mantiene en ambos paths.
- **`pdfplumber`**: ya es dependencia de S1 (Etapa 01); S2 la reusa para extraer fulltext de los PDFs adjuntos a los items de Zotero al momento de embebera (ADR 015 §3.2 / §6).
- **Schema de `zotero-mcp`**: S2 escribe a una ChromaDB que `zotero-mcp serve` (host) lee. La compatibilidad de schema fue validada empíricamente (Fase 2 del plan de implementación de ADR 015) y se documenta como contrato en ADR 015 §6. Cualquier upgrade futuro de `zotero-mcp` debe re-validarse.
- **SQLite ≥ 3.9 con FTS5** (ADR 017): las queries persistentes usan una tabla virtual FTS5 `candidate_fts` en `candidates.db` para el score BM25. SQLite 3.9 liberó FTS5 en 2015; cualquier Python 3.11 trae uno muy superior, así que no hay acción requerida del usuario — se documenta solo para cubrir el caso "builds exotics de SQLite sin soporte FTS5".
