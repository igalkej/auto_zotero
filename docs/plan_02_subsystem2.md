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

### 7.2 Score semántico (sprint 3, requiere S3 operativo)

**Input**: candidate con `abstract`.
**Output**: `score_semantic ∈ [0, 1]`.

**Lógica**:
1. Calcular embedding del `abstract` del candidate (OpenAI `text-embedding-3-large`, ~$0.00013/candidate).
2. Query contra ChromaDB del S3 con `top_k=20`.
3. `score = mean(similarity_scores de los top-k)`.
4. Normalizar: si la biblioteca tiene N papers y el candidate matchea fuerte con ≥10% de los papers temáticamente cercanos, score alto.

**Implementación**:
- Reutilizar la instancia de ChromaDB que construye `zotero-mcp` (path configurable).
- Si ChromaDB no está poblada (S3 nunca corrió), score=0.5 (neutral).

### 7.3 Score por queries persistentes

**Input**: candidate, lista de `PersistentQuery`s activas.
**Output**: `score_queries ∈ [0, 1]`.

**Lógica**:
1. Para cada query, computar match semántico: embedding(query) · embedding(candidate.abstract).
2. Score final: `max(similarity_por_query)` o promedio ponderado por query weight.
3. Si no hay queries activas, score=0.

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

**Tecnología**: APScheduler (in-process) o cron externo invocando `zotai s2 fetch`.

**Decisión preliminar**: APScheduler, porque corre en el mismo container que el dashboard, simplifica deploy. Alternativa cron si el dashboard no está 24/7.

**Frecuencia default**: cada 6 horas. Configurable.

**Lógica del job**:
```python
async def run_fetch_cycle():
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

**Budget**: cada candidato cuesta ~$0.0005 en embeddings + scoring. Para 30 candidates/ciclo × 4 ciclos/día × 30 días = 3600/mes → ~$2/mes.

---

## 10. Sub-módulo: Push a Zotero

**Archivo**: `src/zotai/s2/push.py`

Se dispara cuando un candidate se marca `accepted`.

**Lógica**:
1. Crear item en Zotero via API con metadata del candidate.
2. Si el candidate tiene DOI o URL con PDF descargable, intentar fetch del PDF:
   - Priorizar: OpenAccess URL de OpenAlex → DOI resolver → Sci-Hub (NO, illegal) → URL del RSS.
   - Si conseguimos PDF, adjuntar al item Zotero.
   - Si no, item queda sin PDF, tag `needs-pdf`.
3. Aplicar tags derivados del scoring (los que mejor matchearon).
4. Mover a colección "Inbox S2" en Zotero (configurable).
5. Update del candidate: `zotero_item_key`, `pushed_at`.

**Edge cases**:
- Item ya existe en Zotero (mismo DOI): no duplicar, solo marcar `zotero_item_key` al existente.
- Push falla (API error, red): retry con backoff, eventualmente marcar `push_failed`, mostrar en dashboard.

---

## 11. Roadmap por sprints

### Sprint 1 (3-5 días): Captura bruta

**Objetivo**: el worker captura de feeds, persiste en DB, el dashboard los muestra sin scoring.

- [ ] `config/feeds.yaml` inicial con 5-10 journals.
- [ ] `src/zotai/s2/feeds.py` con RSS parsing.
- [ ] `candidates.db` con schema.
- [ ] `src/zotai/s2/worker.py` con fetch cycle básico.
- [ ] Dashboard minimal: `/inbox` muestra lista sin scoring ni triage.
- [ ] CLI: `zotai s2 fetch-once` para testing manual.

**Deliverable**: ver candidatos fluir al inbox tras ejecutar fetch.

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

- [ ] Integración con ChromaDB del S3 (read-only).
- [ ] `score_semantic` en scoring.py.
- [ ] Breakdown visual de scores en cada card.
- [ ] Keyboard shortcuts en inbox.
- [ ] Bulk actions.
- [ ] `/metrics` con precision observada.
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
S2_CHROMA_PATH=/workspace/chroma_db   # compartido con S3
S2_ZOTERO_INBOX_COLLECTION=Inbox S2   # nombre de la colección destino

# ──────────── S2 Dashboard ────────────
S2_DASHBOARD_HOST=127.0.0.1
S2_DASHBOARD_PORT=8000

# ──────────── S2 Budgets ────────────
S2_MAX_COST_USD_DAILY=0.50
S2_MAX_COST_USD_MONTHLY=10.00
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

- **S1** debe haber corrido: necesitamos biblioteca poblada para que el scoring funcione.
- **S3** debe estar operativo: necesitamos ChromaDB para el score semántico.
- **Zotero abierto** con API local (igual que S1).
- **Worker y dashboard corriendo**: típicamente en el mismo Docker container (misma imagen), separado por service en docker-compose.
