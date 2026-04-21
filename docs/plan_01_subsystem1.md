# plan_01_subsystem1.md — Subsistema 1: Captura retroactiva

**Estado**: Spec cerrada, pendiente de implementación.
**Estimación**: ~5 días de desarrollo (1 desarrollador).
**Complejidad relativa**: media. La lógica es clara; la complejidad está en manejar la heterogeneidad del corpus real.

---

## 1. Propósito del subsistema

Pipeline CLI one-shot que lleva ~1000 PDFs dispersos en carpetas del usuario a su biblioteca Zotero personal, con:

- ≥90% metadata completa (título, autores, año, tipo de ítem, DOI/ID si aplica) en colección principal.
- ≤10% en colección `Quarantine` (accesibles pero marcados).
- 100% indexables por texto (OCR aplicado donde haga falta).
- Tagging automático según taxonomía definida (`plan_taxonomy.md`).

**Costo objetivo**: <$5 en APIs.
**Tiempo humano**: 2-3h total, en sesiones de ≤1h.

---

## 2. Anti-objetivos (no hacer)

- No intentar PRISMA compliance.
- No deduplicar automáticamente entre preprint y published — marcar ambos, usuario decide.
- No curar manualmente >5% del corpus — si excede, pipeline falló y hay que mejorarlo.
- No borrar los PDFs originales.
- No subir PDFs con licencia restrictiva a ningún servicio externo (solo metadata).

---

## 3. Pipeline: seis etapas

### Etapa 01 — Inventory

**Input**: rutas de carpetas fuente (desde `.env`: `PDF_SOURCE_FOLDERS`).
**Output**: filas en tabla `items` del `state.db`.

**Lógica**:
1. Recorrer recursivamente las carpetas.
2. Para cada PDF:
   - Calcular SHA-256 del archivo.
   - Si el hash ya existe en DB, marcar como duplicado, no crear item nuevo.
   - Extraer primeras 3 páginas como texto (via `pdfplumber`).
   - Detectar si el PDF tiene capa de texto (threshold: >100 chars extraíbles de página 1).
   - Buscar DOI por regex en texto extraíble.
   - Persistir: `id` (hash), `source_path`, `size_bytes`, `has_text`, `detected_doi`, `stage_completed=01`.
3. Generar reporte CSV con counts y anomalías.

**Edge cases**:
- PDFs corruptos: logear, marcar `last_error`, no detener pipeline.
- PDFs protegidos con password: idem.
- Archivos no-PDF con extensión `.pdf`: validar magic bytes.
- Duplicados por hash: reportar en CSV, solo uno va al resto del pipeline.

**Criterio de éxito etapa 01**: `stage_completed=01` en el 100% de PDFs válidos. Reporte `inventory_report.csv` generado.

**CLI**:
```bash
zotai s1 inventory [--folder PATH ...] [--dry-run]
```

---

### Etapa 02 — OCR

**Input**: items con `has_text=false AND stage_completed=01`.
**Output**: PDFs OCR-processed en `staging/`, `stage_completed=02`.

**Lógica**:
1. Para cada item sin texto, copiar a `staging/<hash>.pdf`.
2. Ejecutar `ocrmypdf --skip-text --language ${OCR_LANGUAGES} <staging>.pdf <staging>.pdf`.
3. Verificar que el output tiene texto extraíble post-OCR.
4. Si OCR falla (excepción, output vacío): marcar `last_error`, avanzar a `stage_completed=02` igual pero con flag `ocr_failed=true`.

**Paralelismo**: `multiprocessing.Pool` con workers = `OCR_PARALLEL_PROCESSES` (default 4).

**Edge cases**:
- Scan de muy baja calidad: OCR completa pero texto inutil. No detectable automáticamente; queda para filtrar en Etapa 04 si no hay match por título.
- PDFs ya con OCR previo malo: `--skip-text` los salta. Hay flag `--force-ocr` para reprocesar.
- Disk space: antes de arrancar, verificar que hay espacio >= sum(size) de items a procesar. Abortar con mensaje claro si no.

**Criterio de éxito etapa 02**: ≥95% de items con `has_text=true` (nativo o post-OCR).

**CLI**:
```bash
zotai s1 ocr [--force-ocr] [--parallel N]
```

---

### Etapa 03 — Import to Zotero

**Input**: items con `stage_completed=02` y `has_text=true`.
**Output**: items creados en Zotero, `zotero_item_key` persistido, `stage_completed=03`.

**Lógica por item**, en orden:

**Ruta A** (si `detected_doi is not null`):
1. Llamar Zotero API `POST /items` con `{itemType: 'journalArticle', DOI: detected_doi}` via translator chain → recupera metadata.
2. Si éxito, adjuntar PDF al item creado.
3. Persistir `zotero_item_key`, marcar `import_route='A'`.

**Ruta B** (si A falla o no hay DOI):
1. Subir PDF como attachment huérfano via Zotero API.
2. Invocar endpoint "Retrieve Metadata for PDFs" de Zotero (request al servicio de recognizer).
3. Si recupera metadata, crear parent item, asociar PDF.
4. Persistir `zotero_item_key`, marcar `import_route='B'`.

**Ruta C** (fallback):
1. Subir PDF como attachment huérfano sin parent.
2. Marcar `import_route='C'`, item queda pendiente de enrichment en Etapa 04.

**Rate limiting**: Zotero API permite ~100 req/sec local. Configurar cliente con `httpx` + `tenacity` retry con exponential backoff.

**Batching**: lotes de 50 items. Pausa 30s entre lotes.

**Edge cases**:
- Zotero desktop no abierto: error claro al usuario antes de arrancar, no a mitad.
- Item ya existe en Zotero (detectable por DOI duplicado): no crear de nuevo, asociar nuestro `state.db` con el `item_key` existente.
- PDF >20MB: Zotero API tiene límites. Subirlo via WebDAV / file storage directo.

**Tasa esperada**:
- Ruta A: 50-60% del corpus (los que tienen DOI)
- Ruta B: 15-20% adicional
- Ruta C: 20-35% (residual que va a Etapa 04)

**Criterio de éxito etapa 03**: 100% de items tienen `zotero_item_key`. Distribución de `import_route` razonable.

**CLI**:
```bash
zotai s1 import [--batch-size 50] [--dry-run]
```

---

### Etapa 04 — Enrichment

**La etapa crítica. Transforma rutas-C en rutas-A virtuales.**

**Input**: items con `import_route='C' AND stage_completed=03`.
**Output**: items con metadata parcial/completa, `stage_completed=04`, posible flag `in_quarantine=true`.

**Lógica en cascada. Un item baja al siguiente sub-paso solo si el anterior falló.**

**04a — Extracción agresiva de identificadores**:
- Regex sobre texto extraído de páginas 1-3:
  - DOI: `10\.\d{4,9}/[-._;()/:A-Z0-9]+`
  - arXiv: `arXiv:\d{4}\.\d{4,5}` o `arxiv\.org/abs/...`
  - ISBN-10/13 en libros
  - Handle.net URLs
  - REPEC format strings
- Si encuentra un ID nuevo no detectado en Etapa 01, reintentar Ruta A/B de Etapa 03.

**04b — Match fuzzy contra OpenAlex**:
- Extraer título probable:
  - Heurística: línea más grande (mayor font-size) en primera página, via `pdfplumber` con layout analysis.
  - Fallback: primera línea no vacía de página 1 si >20 chars.
- Llamar `GET https://api.openalex.org/works?search=<title>&per-page=5`.
- Para cada candidato, calcular `rapidfuzz.fuzz.token_set_ratio(extracted_title, candidate.title)`.
- Si score >= 85:
  - Hidratar item con metadata de OpenAlex (DOI, authors, year, type, abstract).
  - Actualizar en Zotero via PATCH.
- Rate limit: OpenAlex permite 10 req/sec sin autenticación, 100 con email en header. Setear `User-Agent: zotai/{version} (mailto:<user-email>)`.

**04c — Match fuzzy contra Semantic Scholar**:
- Solo para items que fallaron 04b.
- `GET https://api.semanticscholar.org/graph/v1/paper/search?query=<title>&limit=5`.
- Mismo criterio fuzzy match.
- Rate limit: 100 req/5min sin key. Si tenés `SEMANTIC_SCHOLAR_API_KEY`, 1 req/sec.

**04d — LLM extraction (gpt-4o-mini)**:
- Solo para items que fallaron 04c.
- Enviar primeras 2 páginas + prompt estructurado:
```
Extract bibliographic metadata from this document. Return JSON with fields:
  title, authors (list of {first, last}), year, item_type (one of:
  journalArticle, book, bookSection, thesis, report, preprint, conferencePaper),
  venue, doi (null if not present), abstract.
If a field cannot be determined with reasonable confidence, return null.
Do NOT invent information.
```
- Response format: `json_object`.
- Validar JSON contra pydantic model.
- Push a Zotero via API.

**04e — Cuarentena**:
- Items que fallaron todas las sub-etapas.
- Taggear con `needs-manual-review`.
- Mover a colección `Quarantine` en Zotero.
- Persistir en CSV `quarantine_report.csv` con path original, primeras 200 chars del texto, razón de fracaso.

**Presupuesto por sub-etapa**:
- 04a-c: $0 (APIs gratuitas, solo rate limits).
- 04d: max $2 configurable. Si excede, pausar y pedir confirmación.

**Edge cases**:
- API down (OpenAlex, Semantic Scholar): retry con backoff; tras 3 fallos, saltar item y continuar.
- Título extraído es genérico ("Chapter 1", "Introduction"): detectable por longitud <5 palabras o coincidencia con blacklist. Saltar a siguiente sub-etapa directamente.
- LLM retorna JSON malformado: reintentar 1 vez con mensaje corregir. Si falla, cuarentena.

**Criterio de éxito etapa 04**: <10% del corpus original en cuarentena.

**CLI**:
```bash
zotai s1 enrich [--substage {04a,04b,04c,04d,04e}] [--max-cost N]
```

---

### Etapa 05 — Tagging

**Input**: items con metadata completa, NOT in_quarantine, `stage_completed=04`.
**Output**: tags aplicados en Zotero, `stage_completed=05`.

**Lógica**:
1. Cargar taxonomía desde `plan_taxonomy.md` (parseada a YAML/JSON en `config/taxonomy.yaml`).
2. Para cada item, componer prompt:
```
You are tagging an academic paper for a researcher's bibliographic database.
The researcher works in economics and social sciences, with focus on Latin
America.

Paper metadata:
  Title: {title}
  Abstract: {abstract}
  Authors: {authors}
  Year: {year}
  Type: {item_type}

Choose 2-4 tags from the TEMA taxonomy and 1-2 tags from the METODO taxonomy.
Only use tags from the provided lists. If nothing fits well, use fewer tags
rather than forcing.

TEMA taxonomy:
  {tema_list}

METODO taxonomy:
  {metodo_list}

Return JSON: {tema: [tags], metodo: [tags]}
```
3. Invocar `gpt-4o-mini`.
4. Validar que tags retornados están en la taxonomía (strict).
5. En modo `--preview`: generar CSV con propuestas, NO aplicar.
6. En modo `--apply`: PATCH a Zotero con tags.

**Presupuesto**: $1 configurable. Para 1000 papers a ~$0.0004/paper → $0.40.

**Edge cases**:
- Items sin abstract: tag solo con título, más conservador (menos tags).
- LLM inventa tag que no está en taxonomía: descartar esa tag, no fallar item.
- LLM retorna JSON malformado: reintento 1 vez, si falla dejar item sin tags (usuario revisa).

**Criterio de éxito etapa 05**: ≥80% de items tienen al menos 1 tag de TEMA y 1 de METODO.

**CLI**:
```bash
zotai s1 tag [--preview|--apply] [--max-cost N]
```

---

### Etapa 06 — Validation

**Input**: todo el estado actual.
**Output**: reporte HTML + CSV en `reports/s1_validation_{timestamp}.{html,csv}`.

**Chequeos**:
1. **Completitud**: % con metadata completa, % con tags, % con fulltext extraíble.
2. **Distribución de tags**: tag counts, tags huérfanos (usados <3 veces), tags dominantes (>30% del corpus).
3. **Consistencia**: items con `year` fuera de rango razonable [1900, current_year+1], items con 0 autores, items sin título.
4. **Duplicados potenciales**: pares con `fuzz.ratio(title) > 90 AND same year`.
5. **Costos**: total gastado, breakdown por etapa y servicio.
6. **Tiempo**: por etapa, total wall-clock.

**Output HTML**: navegable, con links a Zotero para cada item flagged.

**CLI**:
```bash
zotai s1 validate [--open-report]
```

---

## 4. Estado compartido: `state.db`

Archivo SQLite en volumen persistente Docker (`/workspace/state.db`).

### Schema

```python
# Usar sqlmodel (SQLAlchemy + pydantic)

class Item(SQLModel, table=True):
    id: str = Field(primary_key=True)  # SHA-256 hex
    source_path: str
    size_bytes: int
    has_text: bool = False
    detected_doi: Optional[str] = None
    ocr_failed: bool = False
    zotero_item_key: Optional[str] = None
    import_route: Optional[str] = None  # 'A', 'B', 'C'
    stage_completed: int = 0
    in_quarantine: bool = False
    last_error: Optional[str] = None
    metadata_json: Optional[str] = None  # JSON blob
    tags_json: Optional[str] = None
    created_at: datetime
    updated_at: datetime

class Run(SQLModel, table=True):
    id: int = Field(primary_key=True)
    stage: int
    started_at: datetime
    finished_at: Optional[datetime] = None
    items_processed: int = 0
    items_failed: int = 0
    cost_usd: float = 0.0
    status: str  # 'running', 'succeeded', 'failed', 'aborted'

class ApiCall(SQLModel, table=True):
    id: int = Field(primary_key=True)
    run_id: int = Field(foreign_key="run.id")
    service: str  # 'openalex', 'semantic_scholar', 'openai', 'zotero'
    cost_usd: float = 0.0
    duration_ms: int
    status: str  # 'success', 'error', 'rate_limited'
    item_id: Optional[str] = Field(foreign_key="item.id", default=None)
    timestamp: datetime
```

### Migraciones

Usar `alembic`. Cualquier cambio al schema post-release requiere migration.

---

## 5. Configuración via .env

```bash
# ──────────── Zotero ────────────
ZOTERO_API_KEY=                  # de zotero.org/settings/keys
ZOTERO_LIBRARY_ID=               # userID
ZOTERO_LIBRARY_TYPE=user
ZOTERO_LOCAL_API=true            # usar API local (requiere Zotero abierto)

# ──────────── OpenAI ────────────
OPENAI_API_KEY=
OPENAI_MODEL_TAG=gpt-4o-mini
OPENAI_MODEL_EXTRACT=gpt-4o-mini

# ──────────── Semantic Scholar (opcional) ────────────
SEMANTIC_SCHOLAR_API_KEY=        # opcional, para mejor rate limit

# ──────────── Paths ────────────
PDF_SOURCE_FOLDERS=/data/folder1,/data/folder2
STAGING_FOLDER=/workspace/staging
STATE_DB=/workspace/state.db
REPORTS_FOLDER=/workspace/reports

# ──────────── Budgets ────────────
MAX_COST_USD_TOTAL=10.00
MAX_COST_USD_STAGE_04=2.00
MAX_COST_USD_STAGE_05=1.00

# ──────────── OCR ────────────
OCR_LANGUAGES=spa+eng
OCR_PARALLEL_PROCESSES=4

# ──────────── Behavior ────────────
DRY_RUN=false
LOG_LEVEL=INFO
USER_EMAIL=                      # para User-Agent de OpenAlex
```

---

## 6. Orquestación: comando `run-all`

```bash
zotai s1 run-all
```

Ejecuta etapas 01-06 en orden, con prompts interactivos entre etapas:

```
[01/06] Inventory complete. 1024 PDFs found, 23 duplicates, 312 need OCR.
        Continue? [Y/n]

[02/06] OCR in progress...
        Done. 308 successful, 4 failed (see state.db).
        Continue? [Y/n]

... etc
```

Modo `--yes` skippea confirmaciones (para CI o usuarios experimentados).

---

## 7. Manejo de errores

**Principio**: un item que falla NUNCA detiene el pipeline. Se persiste el error y se continúa.

**Implementación**:
- Decorador `@stage_item_handler` que wraps cada procesamiento de item individual.
- Captura excepciones, persiste en `last_error`, incrementa counter de fallos en `Run`.
- Al final de cada etapa: si `items_failed / items_processed > 0.30`, abortar con mensaje claro y instrucciones de diagnóstico.

**Errores que SÍ detienen**:
- Credenciales inválidas (Zotero, OpenAI).
- Disco lleno.
- Zotero desktop no accesible (si `ZOTERO_LOCAL_API=true`).
- Budget excedido.

---

## 8. Testing

**Cobertura mínima**: 60% en `src/zotai/s1/*`.

**Fixtures**:
- 20 PDFs de test variados (con DOI, sin DOI, escaneados, mal OCR, no-PDF con extensión .pdf).
- Mock de Zotero API via `respx`.
- Mock de OpenAI / OpenAlex / Semantic Scholar.

**Tests críticos**:
- Cada etapa es idempotente: correr dos veces produce mismo resultado.
- Pipeline se reanuda desde stage_completed correcta tras interrupción.
- `--dry-run` no modifica ni Zotero ni `state.db`.
- Budget enforcement: mockear costos altos, verificar abort.

---

## 9. Deliverables de la implementación de S1

- [ ] `src/zotai/s1/stage_*.py` (6 archivos, uno por etapa).
- [ ] `src/zotai/state.py` con modelos SQLModel.
- [ ] `src/zotai/api/{zotero,openalex,semantic_scholar,openai_client}.py`.
- [ ] `src/zotai/cli.py` con comandos `s1 {inventory,ocr,import,enrich,tag,validate,run-all,status}`.
- [ ] `config/taxonomy.yaml` poblado desde `plan_taxonomy.md`.
- [ ] `Dockerfile` funcional.
- [ ] `docker-compose.yml` con volúmenes correctos.
- [ ] Tests con cobertura ≥60%.
- [ ] `docs/setup-windows.md`, `docs/setup-linux.md`.
- [ ] `docs/decisions/001-009` ADRs (see `plan_00_overview.md` §5).
- [ ] README.md con quickstart.
- [ ] CHANGELOG.md con entry para v0.1.0.

---

## 10. Fuera de alcance del S1

Explícitamente pospuesto:
- Indexación semántica con ChromaDB → parte del S3.
- Dashboard web → parte del S2.
- Integración con Better BibTeX export → post-v1.0.
- Detección de duplicados entre preprint/published → v1.1 si hace falta.
