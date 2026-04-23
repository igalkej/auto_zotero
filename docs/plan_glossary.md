# plan_glossary.md — Glosario

**Propósito**: uniformar el vocabulario del proyecto. Cuando Claude Code (o cualquier dev) vea uno de estos términos, debe entender exactamente lo mismo cada vez.

---

## Términos del producto

**Corpus**
El conjunto completo de documentos que el investigador tiene a disposición. Subdividido en *corpus activo* (biblioteca principal de Zotero, usado para búsquedas) y *corpus en cuarentena* (items con metadata insuficiente, accesibles pero no indexados).

**Biblioteca principal**
La colección raíz de Zotero del investigador. Excluye explícitamente `Quarantine` e `Inbox S2`.

**Cuarentena (Quarantine)**
Colección especial en Zotero donde viven items que el pipeline del S1 no pudo procesar con calidad suficiente. Están presentes pero no contaminan las búsquedas del S3.

**Inbox S2**
Colección especial en Zotero a la cual el S2 pushea los candidates aceptados. Diferenciada del inbox del dashboard (que es una UI, no una colección).

**Item**
Entrada bibliográfica en Zotero. Puede ser `journalArticle`, `book`, `bookSection`, `thesis`, `report`, `preprint`, `conferencePaper`. Tiene (o debería tener) metadata + adjunto PDF.

**Candidate** (S2)
Paper sugerido por el worker del S2 como potencialmente relevante, pendiente de triage. No es un item de Zotero todavía; vive en `candidates.db`.

**Triage**
Acción humana de decidir sobre un candidate: accept, reject, defer.

**Push** (S2)
Acción de convertir un candidate `accepted` en un item de Zotero real, con metadata completa y (si posible) PDF adjunto.

---

## Términos técnicos

**Pipeline** (S1)
Secuencia de 6 etapas (inventory, OCR, import, enrich, tag, validate) que procesa el corpus inicial. Cada etapa tiene estado persistente.

**Etapa / Stage**
Una unidad indivisible del pipeline S1, identificada por número (01-06). Tiene input bien definido, output bien definido, y un criterio de éxito.

**Sub-etapa / Substage** (S1 Etapa 04)
Las cinco sub-etapas de enrichment: 04a (identifiers), 04b (OpenAlex), 04c (Semantic Scholar), 04d (LLM), 04e (cuarentena).

**Worker** (S2)
Proceso scheduled que corre cada N horas, fetcheando feeds y procesando candidates. Vive en el mismo container que el dashboard pero es un proceso separado (APScheduler).

**Scoring** (S2)
Cálculo de tres sub-scores (`tags`, `semantic`, `queries`) y su combinación en `score_composite`. Bajo ADR 016 la combinación default es Reciprocal Rank Fusion (no promedio ponderado); bajo ADR 017 el sub-score `queries` es a su vez un hybrid de BM25 (SQLite FTS5, lexical) + dense cosine.

**Hybrid retrieval** (S2)
Combinación convex de BM25 lexical + dense semántico para queries persistentes cortas: `α·BM25 + (1-α)·cos`, default `α=0.4`. BM25 corre sobre una tabla virtual FTS5 `candidate_fts` en `candidates.db`; el componente dense reusa los embeddings de ChromaDB. Cierra el gap de recall de ~5-15 puntos que tiene dense-only en queries de 3-7 tokens. Ver ADR 017.

**Reciprocal Rank Fusion (RRF)** (S2)
Método default para `score_composite`: `sum(1/(k+rank_c(d)))` sobre los tres criterios (tags / semantic / queries), con k=60. Robusto a distribuciones distintas por criterio, sin pesos pre-datos, favorece candidates que rankean alto en cualquier criterio individual. Ver ADR 016.

**State DB / state.db** (S1)
SQLite con el estado del pipeline S1. Ubicación: `/workspace/state.db` dentro del container.

**Candidates DB / candidates.db** (S2)
SQLite con candidates, feeds, queries. Separado del `state.db`. Ubicación: `/workspace/candidates.db`.

**Chroma DB**
Base vectorial para embeddings de los items de la biblioteca Zotero del usuario. Ubicación canónica: `~/.config/zotero-mcp/chroma_db/` en el host (path por default de `zotero-mcp setup`); montada al container de S2 como `/workspace/chroma_db:rw`. **S2 la escribe** (owner, ver ADR 015) via `reconcile_embeddings()` por ciclo del worker + el comando one-shot `zotai s2 backfill-index`. **S3 (`zotero-mcp serve`) la lee** para responder queries MCP desde Claude Desktop. No se ejecuta `zotero-mcp update-db` en ningún flujo del proyecto.

**Reconciliación de embeddings** (S2)
Proceso que corre en cada ciclo del worker como paso 0, antes del fetch de RSS y del scoring. Compara el conjunto de keys en Zotero con el conjunto de ids en ChromaDB; embebe lo faltante (limitado por `S2_MAX_EMBED_PER_CYCLE`) y borra huérfanos cuando el ratio está bajo `S2_SAFE_DELETE_RATIO` (safety contra bugs de lectura de Zotero que vaciarían el store). Implementa el invariante "todo item no-cuarentenado en Zotero está indexado en ChromaDB" del ADR 015. Idempotente: correr dos veces seguidas con el mismo estado produce el mismo resultado. Disparable manualmente con `zotai s2 reconcile` o como parte de `zotai s2 fetch-once`.

**Backfill de índice** (S2)
Comando `zotai s2 backfill-index`: misma lógica de reconciliación pero con `max_per_cycle` efectivamente sin límite, progress bar, y cap de costo separado (`S2_MAX_COST_USD_BACKFILL`, default 3.00). Es el primer comando que el usuario corre tras completar S1 + setup de S3 — pobla ChromaDB inicialmente para que `score_semantic` arranque con datos. Idempotente; re-correrlo es seguro y barato.

**Clasificador académico / no-académico** (S1 Etapa 01)
Filtro upstream del pipeline S1. Decide, para cada PDF encontrado bajo `PDF_SOURCE_FOLDERS`, si es material bibliográfico o material de descarte (facturas, DNIs, tickets, manuales, etc.). Estrategia híbrida en 3 ramas: (1) **accept** automático por heurística positiva — DOI / arXiv / ISBN / keywords académicos en páginas 1-3; (2) **reject** automático por heurística negativa — ≤2 páginas + ausencia de texto o keywords de facturación en primera página; (3) **ambiguos** resueltos por `gpt-4o-mini` con prompt corto. Ver `plan_01_subsystem1.md` §3 Etapa 01 y §3.1.

**Excluded report**
CSV en `reports/excluded_report_<ts>.csv` que lista los PDFs rechazados por el clasificador con su razón. Estos PDFs **no entran a `state.db`** y no consumen OCR/API de stages posteriores. Archivo paralelo al `inventory_report.csv`.

**Needs review**
Flag booleano en `Item.needs_review`. True cuando el clasificador Stage 01 tuvo que decidir con incertidumbre (LLM respondió con `confidence=low`, o tras error transitorio). El item sigue al resto del pipeline como académico, pero se lo surfacea explícitamente en el reporte de Etapa 06 para que el usuario lo revise manualmente.

**Ruta A/C** (S1 Etapa 03)
Las dos estrategias de import a Zotero:
- **A**: import con metadata via OpenAlex. Resolvemos el DOI detectado en Etapa 01 contra `api.openalex.org/works/doi:<doi>`, recibimos metadata completa (título, autores, año, venue, abstract), y creamos el item en Zotero con `pyzotero.create_items([...])` + attachment del PDF como hijo. Las versiones previas del plan describían esto como "via translator chain" de Zotero; ver ADR 010 para por qué usamos OpenAlex en su lugar.
- **C**: import como attachment huérfano sin parent. Absorbe todo lo que no cae por A (items sin DOI detectado, o items donde OpenAlex no tiene el DOI o devuelve metadata sin título/autores). La recuperación de metadata para estos items sucede después, en la cascada de Etapa 04 (enrichment).

La Ruta B (recognizer de Zotero Desktop sobre PDF huérfano) existió en versiones previas del plan y fue eliminada. Ver `plan_01_subsystem1.md` §3 Etapa 03 "Nota — ausencia de Ruta B" para el rationale.

**Idempotencia**
Propiedad de que una operación puede ejecutarse múltiples veces con el mismo efecto final que una sola ejecución. Crítico en todo el pipeline S1.

**Dry-run**
Modo de ejecución donde el sistema reporta qué haría sin ejecutar ni modificar estado externo (Zotero, ChromaDB) ni DB interna.

---

## Términos de Zotero

**Library ID**
Identificador numérico único del usuario en Zotero. Visible en `zotero.org/settings/keys`.

**Item Key**
Identificador del item dentro de una biblioteca Zotero. String de 8 caracteres alfanuméricos.

**Collection**
Carpeta lógica en Zotero. Un item puede estar en múltiples colecciones simultáneamente.

**Local API**
API HTTP expuesta por Zotero Desktop cuando está corriendo, en `http://localhost:23119/api`. Requiere `Settings → Advanced → Allow other applications...` habilitado.

**Web API**
API de zotero.org, usada cuando Zotero Desktop no está corriendo. Más lenta, con rate limits.

**Translator**
Componente de Zotero que convierte metadata de una fuente (DOI, arXiv, etc.) en un item estructurado. Invocable via la API local.

---

## Términos de MCP

**MCP (Model Context Protocol)**
Protocolo de Anthropic que permite a Claude (u otros LLMs) usar herramientas externas (tools). Implementado sobre JSON-RPC.

**MCP Server**
Proceso que expone tools vía MCP. En este proyecto: `zotero-mcp`.

**MCP Tool**
Una operación invocable por Claude. En este proyecto: `zotero_search`, `zotero_semantic_search`, `zotero_fulltext`, etc.

**Tool call**
Invocación de una tool por parte de Claude durante una sesión.

---

## Términos de costos

**Budget** (presupuesto)
Límite configurable de gasto en dólares por etapa, sub-etapa, o total. Definido en `.env`.

**Presupuesto duro**
Límite absoluto. Si se excede, el sistema aborta la operación.

**Presupuesto blando**
Warning threshold. Se excede, se muestra advertencia pero se continúa.

---

## Antipatrones (nombrarlos explícitamente)

**Cámara de eco**
Situación donde el sistema de captura prospectiva (S2) solo sugiere papers muy similares a los que ya tenés, reduciendo diversidad. Riesgo inherente a "similitud semántica" como criterio. Mitigación: mantener `score_queries` como señal alternativa, no solo `score_semantic`.

**Ilusión de cobertura** (S3)
Situación donde Claude sintetiza sobre el subset de papers recuperados por RAG, y el usuario cree que la síntesis cubre "toda" su biblioteca. Mitigación: pedir siempre los IDs recuperados antes de la síntesis, verificar cobertura manualmente.

**Deriva de taxonomía**
Tendencia a agregar tags nuevos incrementalmente sin consolidar, llevando a vocabulario inflado (>100 tags), cada uno usado pocas veces. Mitigación: review trimestral obligatorio.

**Trabajo silencioso**
Operaciones automatizadas que fallan silenciosamente, degradando calidad sin que el usuario lo note. Prohibido. Todo error se loguea y aparece en algún reporte.

---

## Jerarquía de confianza (importante)

De más a menos confiable, para decisiones de cita:

1. **Lectura manual del paper completo** (siempre gold standard).
2. **Lectura manual de passage específico recuperado por `zotero_fulltext`**.
3. **Abstract del paper en Zotero** (metadata validada).
4. **Síntesis de Claude sobre passages recuperados** (con citas explícitas, verificable).
5. **Sugerencia de cita por `zotero_semantic_search`** (requiere verificación manual).
6. **Tag aplicado automáticamente por S1 Etapa 05** (solo orientativo).

El usuario **nunca** debe citar basándose en el nivel 5 o 6 sin pasar por 1-2.
