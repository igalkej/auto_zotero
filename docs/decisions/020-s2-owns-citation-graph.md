# ADR 020 — S2 es owner del grafo de citas de la biblioteca

**Status**: Accepted
**Date**: 2026-04-28
**Deciders**: project owner
**Related**: ADR 015 (S2 owna ChromaDB), ADR 016 (RRF composite), ADR 017 (hybrid retrieval), plan_02 §5, §7, §11.
**Spawns**: ADR 021 (cascade de captura de refs), ADR 022 (metadata-only push).

---

## 1. Contexto

Cada paper en la biblioteca tiene información estructural que el sistema hoy descarta: su bibliografía. Esa información está disponible gratuitamente en OpenAlex (`referenced_works`) y, para casos no cubiertos, vía HTML scraping de la página del artículo (especialmente OJS / SciELO en LATAM — ver ADR 021). Ignorarla deja sobre la mesa tres ejes de valor:

- **Discovery de papers ausentes muy citados por el corpus**. Un DOI que aparece citado por k≥2 papers de mi biblioteca y no está en ella = un anchor del campo que no tengo. Hoy es invisible.
- **Scoring por overlap bibliográfico** para candidates de S2. Particularmente importante cuando la señal semántica es débil: papers paywalled con abstract genérico, papers en humanidades / LATAM con metadata escasa, o papers que aplican ideas viejas a problemas nuevos (taxonomía distinta, bibliografía compartida). Hoy `score_composite` depende de tags + semantic + queries; las tres degradan en esos casos.
- **Persistencia útil de items no-OA**. ADR 022 establece que items metadata-only son ciudadanos de primera clase. Sin grafo de citas son agujeros informativos; con grafo, contribuyen su bibliografía a la cobertura del usuario aun sin PDF.

La pregunta arquitectónica es quién owna ese grafo. Bajo el contrato de plan_00 §3 ("no hay DB compartida entre S1 y S2; comunicación sólo via Zotero"), refs son un derivado del corpus que cualquier subsistema podría capturar. Las opciones:

- **S1 captura refs en Etapa 04 enrichment, las escribe a `state.db`, S2 lee desde ahí**. Más eficiente (una sola llamada a OpenAlex). Viola el contrato.
- **DB nueva (`references.db`) con su propio módulo**. Mantiene cleanness pero suma una tercera SQLite y owner ambiguo.
- **S2 captura, persiste en `candidates.db`, mantiene el invariante por reconciliación** (mismo patrón ADR 015). Respeta el contrato. Cuesta una duplicación de calls a OpenAlex (gratis) que se asume con un comentario explícito.

Este ADR adopta la tercera opción.

## 2. Decisión

**S2 es owner del grafo de citas de la biblioteca, en `candidates.db`. S1 no captura refs.**

Concretamente:

- S2 mantiene el invariante: para todo paper en Zotero (no cuarentenado), existen refs persistidas en la tabla `Reference` indexadas por `zotero_item_key`.
- El invariante se preserva por **reconciliación por diff**, paralelo al de embeddings (ADR 015):

  ```
  zotero_keys := { items en Zotero, no cuarentenados }
  refs_keys   := { citing_zotero_key DISTINCT FROM Reference }

  to_fetch  := zotero_keys − refs_keys
  to_remove := refs_keys − zotero_keys

  Para cada key en to_fetch (limitado a S2_MAX_REFS_FETCH_PER_CYCLE):
      doi := zotero.get_doi(key)
      refs := refs_cascade.fetch(doi, landing_url)   # ADR 021
      Reference.upsert_many(citing_zotero_key=key, refs=refs)
      ExternalPaper.upsert_many(refs cuyo cited_doi ∉ zotero_dois)

  Si |to_remove| / |refs_keys| ≤ S2_SAFE_DELETE_RATIO:
      Reference.delete(citing_zotero_key in to_remove)
  Else:
      log WARNING, no borrar, requerir intervención
  ```

- El primer poblado se expone como comando explícito `zotai s2 backfill-references`, con su propio budget cap. No es un caso especial de código — es el mismo código de reconciliación, expuesto como comando para que el usuario lo dispare intencionalmente la primera vez.
- Reconcile de refs corre como **step 0.5** del worker, inmediatamente después del reconcile de embeddings (que es step 0 bajo ADR 015). Ambos reusan la misma enumeración de `zotero_keys`.

### 2.1 Tablas nuevas en `candidates.db`

```python
class Reference(SQLModel, table=True):
    """Una arista del grafo de citas: paper_X cita paper_Y."""

    id: int | None = Field(default=None, primary_key=True)
    citing_zotero_key: str = Field(index=True)            # 8 chars, FK lógica a Zotero
    cited_doi: str | None = Field(default=None, index=True)
    cited_openalex_id: str | None = None
    cited_text: str | None = None                         # cita libre cuando no hay DOI
    source_api: str                                       # ver ADR 021 §2
    fetched_at: datetime = Field(sa_type=UTCDateTime)


class ExternalPaper(SQLModel, table=True):
    """Cache de metadata de DOIs ausentes en Zotero pero citados por papers en él.

    Habilita la bandeja /classics sin re-llamar a OpenAlex en cada render.
    """

    doi: str = Field(primary_key=True)
    openalex_id: str | None = None
    title: str
    authors_json: str
    year: int | None = None
    venue: str | None = None
    abstract: str | None = None
    cited_by_count: int = 0
    last_seen_at: datetime = Field(sa_type=UTCDateTime)
```

`Reference.cited_doi` y `cited_text` son ambos nullable: pueden coexistir (cita resuelta con texto crudo de respaldo) o sólo uno (cita sin DOI conocido se persiste como `cited_text` libre, ver §2.5). `cited_openalex_id` queda redundante con `cited_doi` para hits de OpenAlex pero permite resolver cited works que aún no tienen DOI registrado en OpenAlex.

### 2.2 Cambios en `Candidate`

`Candidate.source_feed_id` se vuelve **nullable** y se agrega `source_kind` enum:

- `'rss'` — captado por feed RSS (default actual; `source_feed_id` requerido).
- `'reference_mining'` — sugerido por el sistema desde el grafo de citas (escenario "discovery de classics ausentes"; `source_feed_id` null).

```python
class Candidate(SQLModel, table=True):
    # ... campos existentes ...
    source_feed_id: str | None = Field(default=None, foreign_key="feed.id")
    source_kind: str = "rss"   # 'rss' | 'reference_mining'

    score_refs: float = 0.0    # nuevo, ver §2.4
```

### 2.3 Comando explícito y reconcile en worker

- **`zotai s2 backfill-references`**: idempotente, recorre `zotero_keys − refs_keys`, llama la cascade de captura (ADR 021), persiste a `Reference` y `ExternalPaper`. Budget cap: `S2_MAX_COST_USD_BACKFILL_REFS` (default `0` — OpenAlex es gratis; reservado para que futuras fuentes con costo no requieran cambio de schema). Progress bar y mensajes al usuario equivalentes al `backfill-index` de ADR 015.
- **`zotai s2 reconcile-references`**: lo mismo, sin RSS fetch — para debugging.
- En el worker (`run_fetch_cycle()`): step 0 reconcile-embeddings (ADR 015), step 0.5 reconcile-references (este ADR), step 1+ resto del ciclo (RSS fetch + scoring + persist).

### 2.4 `score_refs` como cuarta señal del composite

Se agrega `score_refs ∈ [0,1]` a `Candidate`, computado al momento del scoring (post-fetch RSS / post-discovery):

```
score_refs(c) = |refs(c) ∩ zotero_dois| / |refs(c)|
```

donde `refs(c)` se obtiene aplicando la cascade ADR 021 al DOI del candidate, y `zotero_dois` es el conjunto de DOIs presentes en Zotero (cacheado al ciclo del worker).

**Si `|refs(c)| = 0`** (cobertura combinada cero), el score se **omite del RRF** (ADR 016) en lugar de penalizar. No podemos puntuar a falta de información; RRF naturalmente integra las tres señales restantes con la convención `rank = ∞ ⇒ contribution = 0`.

**Coupling intra-corpus** (similitud entre dos papers de Zotero por refs compartidas) queda implícito en el grafo y no se materializa en v1. Si se quiere usar para validación de tags o detección de duplicados temáticos, basta con queries SQL sobre `Reference`.

### 2.5 Refs sin DOI

Cuando un parser identifica una cita pero no logra resolverla a DOI (libros viejos, working papers, tesis, fuentes primarias), se persiste con `cited_doi=null` y `cited_text` con el texto crudo de la cita. Estos no contribuyen a `score_refs` (no se intersecta contra `cited_text`), pero son auditables y pueden fuzz-matchearse contra `zotero_dois` por título en una pasada futura. Detalle de captura en ADR 021.

### 2.6 No persistimos `cited_by`

OpenAlex también expone `cited_by_api_url` (paginable). Se decide **no persistir** este lado del grafo en v1: los datos cambian rápido (un paper recién publicado puede ser citado en semanas), persistirlo invita a que se desactualice, y los casos de uso identificados (overlap, scoring, discovery de classics) no lo requieren — todos se resuelven con el lado saliente.

`cited_by` queda como consulta on-demand cuando aparezca una feature que lo justifique (por ejemplo: bandeja "papers que citan a tus papers").

## 3. Consecuencias

### 3.1 Positivas

- **Nueva señal ortogonal de scoring**. `score_refs` es estructural (un paper cita o no cita), no falseable por palabras. Particularmente fuerte donde el resto degrada: humanidades, LATAM, paywalled.
- **Bandeja /classics habilitada**. Discovery de papers altamente citados ausentes en Zotero queda como bandeja del dashboard (ver plan_02 §8.3 post este ADR). Misma máquina de triage que candidates RSS, distinta fuente.
- **Coherencia con ADR 015**. Mismo subsistema, mismo patrón de reconciliación, mismo modelo de auto-curación. S2 ahora maneja dos derivados del corpus (embeddings + refs).
- **Auto-curación uniforme**. Si `Reference` se borra, el siguiente ciclo la repuebla. Sin estado externo a Zotero.
- **Items metadata-only ganan utilidad**. La decisión de ADR 022 (aceptar items no-OA como ciudadanos de primera clase) sólo es útil si esos items aportan algo. Refs son lo que aportan.

### 3.2 Negativas / costos asumidos

- **Duplicación de calls a OpenAlex**. S1 Etapa 04 ya llama a OpenAlex por DOI para metadata. S2 backfill-references re-llama por los mismos DOIs para refs. Costo: ~5-10 min de calls extra para 1000 papers, $0 USD (OpenAlex es gratis, polite pool 10 req/s). El contrato S1/S2 disjuntas justifica la duplicación: no introducir DB compartida ni dependencia inter-subsistema. Mitigación cosmética (no operacional): cliente compartido `fetch_work_with_refs(doi)` en `api/openalex.py` reusable por ambos.
- **`candidates.db` crece**. Para 1000 papers × ~30 refs promedio = ~30k filas en `Reference`, ~5-10k DOIs únicos en `ExternalPaper`. Trivial para SQLite.
- **Captura no es uniforme**. OpenAlex cubre ~85-95% de journals modernos pero cae para LATAM/SciELO. ADR 021 cubre el gap con HTML scraping. Mitigación: experimento I1 (§5 de este ADR) antes de mergear el sprint que implementa.
- **`Candidate` ahora tiene un campo más** y `source_feed_id` se vuelve nullable. Migración de schema en `candidates.db` cuando aterrice Sprint 5.

### 3.3 Neutras

- No cambia el modelo de embeddings (ADR 004) ni el ownership de ChromaDB (ADR 015). Sólo agrega una cuarta dimensión al scoring.
- S3 (MCP) no se afecta. El servidor `zotero-mcp serve` no consulta refs; opera sobre Zotero + ChromaDB sólo.
- ADR 016 (RRF) no se modifica: la cuarta señal se integra naturalmente vía la convención de "rank infinito ⇒ contribución 0".

## 4. Alternativas consideradas y descartadas

**A. S1 captura refs en Etapa 04, las escribe a `state.db`, S2 lee desde ahí.**
Más eficiente (una sola call a OpenAlex). Descartada: viola el contrato plan_00 §3 sin una ganancia operacional clara. La duplicación de calls es ~5 min que se pagan una sola vez (backfill); los reconciles incrementales son <30 segundos.

**B. Cliente HTTP compartido `fetch_work_with_refs(doi)` que ambos subsistemas usan; cada uno persiste a su DB.**
Refactor cosmético: las HTTP calls siguen duplicadas porque OpenAlex no tiene cache compartida. Adoptado en términos de buena práctica de código (un único cliente OpenAlex en `api/openalex.py`) pero no resuelve el "costo" — sólo limpia ergonomía.

**C. Refs como blob JSON en `metadata_json` de `Item` o `Candidate`.**
Imposible queries de overlap eficientes (overlap en SQL requiere relación normalizada). Descartada.

**D. Reference graph como tercera DB (`references.db`).**
Ortogonal al diseño plan_02 §5. Descartada por complejidad: una tabla en `candidates.db` alcanza, owner es S2, no hace falta un tercer SQLite con su propio engine y migrations.

**E. Captura on-demand cuando aparece un candidate (sin backfill).**
Imposible computar overlap sin tener refs del corpus completo (`zotero_dois` es el lado lento del intersect). Descartada.

**F. Persistir también `cited_by` (citas hacia atrás).**
Descartada para v1 por motivos de §2.6.

## 5. Validación empírica antes de cerrar implementación

Antes de mergear el Sprint 5 que implementa este ADR, ejecutar el experimento **I1** (cobertura combinada de fuentes):

1. Tomar 50-100 DOIs random de la biblioteca Zotero del usuario (post-S1 corrida real).
2. Para cada DOI: llamar OpenAlex; si `|referenced_works| = 0`, intentar HTML scraping (cascade ADR 021).
3. Reportar:
   - % de papers con refs ≥1 (cobertura combinada).
   - Distribución de `len(refs)`.
   - % de refs intra-corpus (papers cuyas refs caen en otros DOIs de la misma biblioteca — proxy de `score_refs` no degenerado).
   - Breakdown por venue / publisher (para identificar cuál fuente hace el peso).

**Gate**:
- Si cobertura combinada **≥ 80%** y % intra-corpus **≥ 5%**, este ADR queda firme.
- Si cobertura combinada **< 60%**, reabrir decisión sobre PDF parsing en ADR 021.
- Si **60% ≤ cobertura < 80%**, este ADR sigue en pie pero `score_refs` se considera señal complementaria, no central; el composite RRF lo absorbe naturalmente.

Mientras el experimento no se haya corrido, este ADR procede con el **supuesto** explícito de cobertura aceptable basado en la prevalencia de OpenAlex en journals modernos. El supuesto se verifica antes del primer merge de código, no del merge de docs.

## 6. Cambios requeridos en documentos existentes

- `docs/plan_02_subsystem2.md`:
  - §5 (modelo de datos): agregar tablas `Reference`, `ExternalPaper`. Agregar campos `source_kind`, `score_refs` en `Candidate`.
  - §7 (scoring): nueva subsección §7.4 "Score por refs (4ta señal del RRF)".
  - §8 (dashboard): nueva subsección §8.3 "Bandeja /classics".
  - §9 (worker): agregar reconcile-references como step 0.5.
  - §11 (sprints): agregar Sprint 5 "Citation graph + bandeja classics + metadata-only push".
- `docs/plan_00_overview.md`:
  - §3 (diagrama): S2 ahora maneja dos stores derivados (ChromaDB + Reference graph).
  - §5 (tabla ADRs): agregar filas 020, 021, 022.
- `docs/plan_01_subsystem1.md`:
  - §10 (fuera de alcance): explicitar "captura de refs no es responsabilidad de S1; S2 lo maneja (ver ADR 020)".
- `docs/plan_03_subsystem3.md`:
  - §X (donde corresponda): confirmación explícita de que items metadata-only se indexan via la cascade existente de ADR 015 (`s2_abstract` / `s2_title_only`); no requiere cambios al schema ChromaDB.
- `docs/plan_glossary.md`: entradas para "citation graph", "anchor papers", "reference mining", "bandeja /classics".
- `CLAUDE.md` §"Contratos entre subsistemas": agregar segundo store derivado owned por S2.
- `README.md`:
  - §"Cómo queda tu biblioteca Zotero": mencionar tags `metadata-only`, `discovered-via-refs` (definidos en ADR 022).
  - §"Estado del proyecto": agregar Sprint 5 a la tabla de S2.
- `.env.example`: agregar `S2_MAX_COST_USD_BACKFILL_REFS`, `S2_MAX_REFS_FETCH_PER_CYCLE`, `S2_SAFE_DELETE_RATIO_REFS`, `S2_REFS_FETCH_TIMEOUT_SECONDS`. (Las variables específicas de la cascade van en ADR 021.)

Estos cambios se aplican en un PR derivado, separado de este (regla CLAUDE.md "Si Claude Code propone un cambio al plan, hacerlo en PR separado de la implementación").

## 7. Presupuesto y métricas

### 7.1 Impacto en budgets

- **Backfill inicial** (`zotai s2 backfill-references`): ~1000 calls OpenAlex (gratis, polite pool) ≈ 5-10 min wall-clock. Si HTML scraping se activa para los misses: ~50-150 calls HTTP adicionales, mismo orden de magnitud, igual gratis.
- **Reconcile incremental** (paso 0.5 del worker): ~10-30 calls por ciclo (papers nuevos en Zotero desde el ciclo anterior), <30 segundos.
- **PDF parsing**: opt-in detrás de flag (ADR 021 §2). Default off; cuando se active, costo en CPU/tiempo no en USD.

`S2_MAX_COST_USD_BACKFILL_REFS` se introduce con default `0.00`. Existe como slot por si una fuente futura agrega costo (ej. Crossref+ con tier pago, scrapers con rotating proxy).

### 7.2 Métricas a exponer en `/metrics`

- `refs_total_edges`: tamaño de `Reference`.
- `refs_papers_indexed`: `COUNT(DISTINCT citing_zotero_key)`.
- `refs_external_papers_cached`: tamaño de `ExternalPaper`.
- `refs_coverage_ratio`: `refs_papers_indexed / |zotero_keys|`.
- `refs_pending`: `|zotero_keys − refs_keys|`.
- `refs_orphans`: `|refs_keys − zotero_keys|`.
- `refs_last_reconcile_at`.
- Breakdown de `source_api` (cuántas refs vienen de openalex / ojs_html / scielo_html / generic_html / pdf).

## 8. Follow-ups

- Si I1 muestra cobertura <60%, reabrir decisión PDF parsing (ADR 021 §3 / §8).
- Si después de 3 meses se observa que `score_refs` domina injustamente sobre las otras señales (RRF se vuelve casi-determinístico por refs), recalibrar pesos en `config/scoring.yaml` o agregar normalización por `cited_by_count_globally` para descontar la importancia bruta del paper citado.
- Coupling intra-corpus (sim A↔B por refs compartidas) queda implícito. Si más adelante se quiere herramienta de "audit de taxonomía" o "detección de duplicados temáticos", materializar tabla `BibliographicCoupling` en una versión sucesora.
- Considerar persistir `cited_by` cuando una bandeja "papers que citan a tus papers" se vuelva una feature deseada (forward reference network, complementaria a la backward que este ADR habilita).
- Si la bandeja /classics produce demasiado ruido en uso real (k=2 default genera N>1000 entries), subir k o agregar normalización por subárea (clusters por embeddings o por venue).

## 9. Relación con ADRs previos

- **ADR 015** (S2 owna ChromaDB): este ADR es el análogo para refs. Mismo subsistema, mismo patrón, mismo modelo de reconciliación. Conceptualmente, "S2 owna los derivados estructurales del corpus" cubre ambos.
- **ADR 016** (RRF default para `score_composite`): `score_refs` se integra naturalmente como cuarta dimensión. Ningún cambio al ADR 016.
- **ADR 017** (hybrid BM25+dense para `score_queries`): independiente. Persistent queries no se afectan.
- **ADR 018/019** (SciELO + DOAJ substages en S1 Stage 04): SciELO queda como **fuente potencial de refs vía HTML scraping** en ADR 021, no como cliente de metadata bibliográfica adicional. Sin conflicto: el cliente Crossref Member 530 de ADR 019 sigue siendo el path para metadata; el parser HTML de ADR 021 es path para refs.
- **ADR 014** (skip attach if existing PDF): independiente. Refs vienen de OpenAlex/HTML, no del PDF en Zotero.
- **ADR 022** (metadata-only push): este ADR provee la motivación operacional para 022 (items metadata-only sólo son útiles si aportan refs).
