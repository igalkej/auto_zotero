# ADR 015 — S2 es owner del índice de embeddings; S3 es lector puro

**Status**: Accepted
**Date**: 2026-04-22
**Deciders**: project owner
**Supersedes**: parcialmente ADR 006 (zotero-mcp role) y ADR 009 (zotero-mcp usage boundary).
**Amends**: ADR 011 (ChromaDB bind mount — flag `:ro` → `:rw`).
**Related**: ADR 004 (OpenAI embeddings), plan_01 §10, plan_02 §7.2, plan_02 §10, plan_03 §4.3, plan_03 §7.1, plan_03 §8.

---

## 1. Contexto

El diseño original atribuía la escritura de ChromaDB a `zotero-mcp` (invocado
por S3) y la lectura a S2, bajo el principio de que `zotero-mcp` ya resuelve
indexación y el proyecto no debía duplicarla (ADR 006). La consecuencia:

- El subsistema que **consume** el índice para decisiones operacionales (S2,
  que scorea candidatos continuamente) depende de un proceso mantenido por
  otro subsistema (S3, cuyo ciclo natural es ad-hoc via Claude Desktop).
- La actualización del índice requiere un trigger externo a S2: cron en el
  host, ejecución manual, o invocación cross-boundary desde el container.
  Todas las variantes evaluadas tienen problemas operativos concretos: cron
  no existe uniformemente cross-platform (Task Scheduler en Windows);
  ejecución manual depende de disciplina humana; invocación cross-boundary
  viola el aislamiento host/container que el proyecto usa deliberadamente.
- Existe una ventana de **staleness no monitoreada**: entre la aceptación de
  un paper y la ejecución del siguiente `zotero-mcp update-db`, S2 scorea
  candidatos contra un snapshot obsoleto sin señal visible al usuario.
- La caída a `score_semantic = 0.5` (plan_02 §7.2) solo se activa cuando
  ChromaDB está vacía, no cuando está desactualizada. El degradamiento
  silencioso por índice viejo es indetectable desde la UI.

Estos problemas no son bugs individuales sino síntomas de que el ownership
del índice quedó en el subsistema equivocado. Este ADR invierte la decisión.

## 2. Decisión

**S2 es owner del índice de embeddings. S3 es lector puro.**

Concretamente:

- **S1 no toca ChromaDB**. Su output es una biblioteca Zotero poblada. No
  hay etapa de indexación en el pipeline de S1.
- **S2 mantiene el invariante**: al final de cada ciclo del worker, todo
  ítem no-cuarentenado en Zotero tiene entrada correspondiente en ChromaDB.
  El invariante se preserva por **reconciliación por diff**, no por
  trackeo de estado explícito.
- **S3 (`zotero-mcp serve`) nunca escribe**. Nunca se ejecuta
  `zotero-mcp update-db` como parte del flujo operativo del proyecto.
- **El primer backfill** (cuando S2 arranca con una biblioteca S1 ya
  poblada pero sin ChromaDB) se expone como comando explícito
  `zotai s2 backfill-index`, con budget propio, progress bar y mensajes
  al usuario. No es un caso especial de código — es el mismo código de
  reconciliación, expuesto como comando para que el usuario pueda
  dispararlo intencionalmente la primera vez.

### 2.1 Mecanismo de reconciliación

En cada ciclo del worker, antes del fetch de RSS y antes del scoring:

```
zotero_keys := { todos los item keys en Zotero, no cuarentenados }
chroma_keys := { todos los ids presentes en la collection de ChromaDB }

to_add    := zotero_keys − chroma_keys
to_remove := chroma_keys − zotero_keys

Para cada key en to_add (limitado a MAX_EMBED_PER_CYCLE):
    text := fulltext(pdf) si hay PDF adjunto legible, else abstract, else title
    embedding := OpenAI.embed(text)
    ChromaDB.upsert(id=key, embedding=embedding, metadata=...)

Si |to_remove| / |chroma_keys| ≤ SAFE_DELETE_RATIO:
    ChromaDB.delete(ids=to_remove)
Else:
    log WARNING, no borrar, requerir intervención
```

El limitador `MAX_EMBED_PER_CYCLE` existe para que el primer ciclo no
sature budget o tiempo; los ciclos siguientes procesan el residual hasta
converger. El safety de delete existe para evitar que un bug de lectura
de Zotero (ej. API devuelve lista vacía) vacíe ChromaDB por diff.

### 2.2 Invariantes garantizados

- **Auto-curación**: si ChromaDB se corrompe o se borra, el siguiente
  ciclo de S2 la reconstruye. Sin comando especial.
- **Convergencia**: el estado post-ciclo satisface
  `zotero_keys ⊆ chroma_keys` (salvo lo residual por
  `MAX_EMBED_PER_CYCLE`), y tras suficientes ciclos la igualdad es
  estricta.
- **Frescura**: no existe ventana de staleness entre modificaciones de
  Zotero y reflejo en ChromaDB mayor que `S2_FETCH_INTERVAL_HOURS`.

## 3. Consecuencias

### 3.1 Positivas

- **Coherencia arquitectónica**: el subsistema que modifica el corpus
  (S2 pushea papers aceptados) es el mismo que mantiene el índice. El
  productor es el owner; el consumer (S3) solo lee.
- **Eliminación del trigger externo**: desaparecen cron, ejecución
  manual, triggers on-use, y cualquier coreografía entre host y
  container relacionada a mantenimiento del índice.
- **Eliminación de la ventana de staleness**: el invariante garantiza
  frescura acotada por la frecuencia del worker.
- **Cross-platform sin fricción**: el código corre dentro del container
  de S2. No hay dependencias de cron/Task Scheduler/shell del host.
- **Idempotencia uniforme**: "agregar un paper" y "hacer backfill de
  1000 papers" son el mismo código, con el mismo patrón de
  reconciliación por diff.
- **Observabilidad**: el estado del índice es inspectable desde el
  dashboard de S2 (último ciclo de reconciliación, pendientes,
  distribución fulltext vs abstract).

### 3.2 Negativas / Costos asumidos

- **S2 gana responsabilidad**. El módulo `src/zotai/s2/` absorbe la
  lógica de extracción de texto, embedding, y escritura a ChromaDB. El
  módulo crece; se mitiga confinando la nueva lógica a
  `src/zotai/s2/indexing.py` como sub-módulo dedicado.
- **Dependencia del schema de `zotero-mcp`**. S2 escribe a una ChromaDB
  que `zotero-mcp serve` lee. Si el schema esperado por `zotero-mcp`
  cambia entre versiones, las lecturas pueden romperse. Mitigaciones:
  (a) pinear versión exacta de `zotero-mcp` en la guía de setup de S3;
  (b) al setup de S3, validar compatibilidad con una query de prueba;
  (c) documentar el schema escrito por S2 en este mismo ADR §6.
- **Responsabilidad de extracción de fulltext en S2**. S1 ya usa
  `pdfplumber` para la etapa 01; S2 reusa ese módulo. No hay nueva
  dependencia, pero sí nueva responsabilidad: S2 debe leer archivos PDF
  del storage de Zotero. Se documenta en plan_02.
- **Justificación de `zotero-mcp` se reduce**. Bajo este ADR,
  `zotero-mcp` provee solo el servidor MCP, no el indexador. La
  dependencia sigue siendo razonable (escribir un MCP server propio no
  está justificado hoy), pero si en el futuro el overhead de
  mantenimiento crece, revisar en un ADR sucesor.

### 3.3 Neutras

- El ADR no cambia los criterios de scoring (pesos, fórmulas del
  plan_02 §7). Solo cambia quién mantiene el índice sobre el que se
  hacen las queries de similitud.
- El ADR no cambia el modelo de embeddings (ADR 004 sigue vigente:
  `text-embedding-3-large`).

## 4. Alternativas consideradas y descartadas

**A. Mantener diseño original + cron de `zotero-mcp update-db`**.
Descartado por problemas cross-platform (cron no existe uniformemente
en Windows), dependencia de disciplina humana, y staleness silenciosa
entre ejecuciones.

**B. Mantener diseño original + trigger "on-use" (`update-db` se dispara
al abrir el dashboard)**. Descartado porque requiere cruzar frontera
host/container para invocar un binario del host desde código del
container; las variantes (archivo marker + watcher en host,
endpoint HTTP en host) agregan componentes operativos sin resolver
cleanly la responsabilidad.

**C. S2 escribe solo al momento del push (idempotencia por paper), sin
reconciliación global**. Descartado porque no cubre papers agregados
manualmente a Zotero ni el backfill inicial. La reconciliación por diff
es estrictamente superior: cubre A + backfill + auto-curación, con el
mismo código.

**D. S2 mantiene su propio índice sobre `candidates.db` separado del
usado por S3**. Descartado por duplicación de costo de embeddings (~$2
extra por usuario inicial) y por romper la promesa de S3 de poder
buscar semánticamente en el corpus completo via Claude Desktop.

**E. Reemplazar `zotero-mcp` por un MCP server propio**. Descartado por
ahora: escribir el servidor MCP (~500-1000 líneas + mantenimiento) no
se justifica cuando `zotero-mcp serve` cubre el caso. Reconsiderable en
un ADR futuro si aparecen features necesarias que `zotero-mcp` no
cubre.

## 5. Validación empírica requerida antes de implementar

Antes de escribir código de producción, validar que `zotero-mcp serve`
puede leer correctamente una ChromaDB escrita exclusivamente por código
propio (sin que `zotero-mcp update-db` haya corrido nunca). Prueba:

1. Borrar ChromaDB existente: `rm -rf ~/.config/zotero-mcp/chroma_db/`.
2. Desde un script Python standalone, crear una collection con el
   nombre y schema que `zotero-mcp` espera, y agregar 3-5 items con
   ids que coincidan con keys reales de Zotero.
3. Ejecutar `zotero-mcp serve`.
4. Desde Claude Desktop, ejecutar `zotero_semantic_search` con una
   query relevante a los items cargados.
5. Verificar que los resultados son correctos y que `zotero_item_details`
   sobre los mismos keys funciona.
6. Verificar que `zotero-mcp serve` tolera el campo extra
   `source_subsystem` en metadata sin pisarlo, ignorarlo de forma
   silenciosa, ni crashear.

Si la prueba falla, documentar qué convención de schema requiere
`zotero-mcp` y ajustar el código de S2 en consecuencia antes de cerrar
la implementación. Si falla de forma irreconciliable (ej.
`zotero-mcp` tiene assumptions no documentadas imposibles de replicar),
reabrir este ADR.

## 6. Schema de ChromaDB escrito por S2

Contrato explícito para que futuras evoluciones no lo rompan sin
reabrir el ADR:

```
collection name: zotero_library   (verificar contra el default de zotero-mcp)

document id: {zotero_item_key}    (string de 8 chars alfanuméricos,
                                   exactamente como lo devuelve
                                   pyzotero — sin prefijo, sin library id)

embedding:   vector de 3072 floats (OpenAI text-embedding-3-large)

metadata:    {
    "title": str,
    "year": int | null,
    "item_type": str,          # journalArticle, book, etc.
    "doi": str | null,
    "source": str,             # "s2_fulltext" | "s2_abstract" | "s2_title_only"
    "indexed_at": ISO8601 str,
    "source_subsystem": "s2",  # marker de ownership
}
```

El campo `source` permite distinguir qué texto se usó para generar el
embedding, sin confundir con el campo `source_subsystem` que identifica
al escritor. Esto es auditabilidad básica que habilita análisis futuros
(por ejemplo, "los papers indexados con `title_only` tienen peor recall,
re-embeberlos cuando aparezca PDF").

## 7. Cambios requeridos en documentos existentes

Este ADR obliga a editar los siguientes documentos para mantener
consistencia. Los cambios concretos están en la orden de trabajo que
acompaña a este ADR (documento separado) y se implementan en PRs
subsiguientes (Fase 1 — docs alineados; Fase 2 — validación
empírica; Fase 3 — código).

- `docs/plan_00_overview.md` §5 (tabla de decisiones): agregar fila ADR 015.
- `docs/plan_01_subsystem1.md` §10 (fuera de alcance): la línea
  "Indexación semántica con ChromaDB → parte del S3" pasa a "Indexación
  semántica con ChromaDB → responsabilidad de S2 (ver ADR 015)".
- `docs/plan_02_subsystem2.md` §4 (arquitectura), §7.2 (score semántico),
  §10 (push), §11 (roadmap): agregar etapa de reconciliación al worker,
  reformular el fallback de `score_semantic`, agregar el ownership del
  índice a las responsabilidades listadas.
- `docs/plan_03_subsystem3.md` §4.3, §5.2 (setup), §7.1 (mantenimiento),
  §8 (integración S2/S3): invertir dirección ("S3 solo lee; S2 escribe").
  Remover el paso "Build del índice inicial" del setup de S3.
- `docs/plan_glossary.md`: la entrada "Chroma DB" actualmente dice "S3
  la escribe, S2 la lee". Invertir.
- `CLAUDE.md` §"Contratos entre subsistemas": el diagrama actualmente
  no menciona ChromaDB. Agregar que ChromaDB es estado owned por S2,
  leído por S3.
- `.env.example`: el comentario en `S2_CHROMA_PATH` que dice "compartido
  con S3, read-only para S2" debe invertirse ("owned por S2, read-only
  para S3").
- `docker-compose.yml`: el mount `:ro` que ADR 011 había anticipado
  para Phase 9 aterriza como `:rw` bajo este ADR (ver amendment en
  ADR 011).

## 8. Presupuesto y métricas

### Impacto en budgets (USD)

- **Pre-ADR**: S3 gastaba ~$2 una vez en el initial indexing; S2 no
  indexaba.
- **Post-ADR**: S2 gasta ~$2 en el primer backfill + ~$0.01-0.05 por
  ciclo en reconciliación incremental. S3 no gasta en indexing.

El gasto total no cambia; cambia de subsistema. La variable
`S2_MAX_COST_USD_DAILY` del `.env.example` puede quedar en 0.50 porque
el backfill se dispara manualmente con `zotai s2 backfill-index` que
tiene su propio límite (`S2_MAX_COST_USD_BACKFILL=3.00`, agregado en
`.env.example` como parte de Fase 1).

### Métricas a exponer en el dashboard

- `chroma_docs_count`: tamaño de la collection.
- `chroma_last_reconcile_at`: timestamp del último ciclo de
  reconciliación exitoso.
- `chroma_pending_embeddings`: tamaño de `zotero_keys − chroma_keys` al
  momento de la consulta.
- `chroma_orphans`: tamaño de `chroma_keys − zotero_keys`.
- Breakdown de `source` en la metadata (cuántos fulltext vs abstract vs
  title_only).

## 9. Follow-ups

- Si tras 3 meses de uso el ratio `abstract`/`fulltext` es alto, evaluar
  si vale la pena un re-embedding asincrónico que convierta
  `abstract_only` a `fulltext` cuando el PDF aparece. Este ADR no lo
  requiere; el estado `abstract_only` es un degradamiento aceptable,
  no un error.
- Si a escala (>10k papers) el diff por enumeración de keys se vuelve
  costoso, migrar a reconciliación incremental con tabla de pendientes
  en `candidates.db`. Documentar en ADR sucesor.
- Revisar la compatibilidad de schema con `zotero-mcp` en cada upgrade
  manual de la dependencia, y registrar el resultado en el CHANGELOG.

## 10. Relación con ADRs previos

- **ADR 006** (adopción de `zotero-mcp` como servidor MCP para S3) —
  parcialmente superseded: la parte de la justificación que apelaba a
  la maturity de `zotero-mcp`'s indexer (`update-db`, `--fulltext`) ya
  no es load-bearing para este proyecto, porque dejamos de usar esa
  capacidad. El resto (MCP transport, tool schema, annotations) sigue
  vigente y es el motivo por el que no forkeamos ni reemplazamos.
- **ADR 009** (S1/S2 usan `pyzotero`; S3 usa `zotero-mcp`) — parcialmente
  superseded: el principio "MCP no es el transporte de pipelines batch"
  sigue vigente. Lo que cambia es el corolario sobre ChromaDB: S2 ahora
  accede al store directamente como writer, no solo como reader
  incidental vía bind mount. El boundary pyzotero (writes a Zotero) /
  ChromaDB directo (writes al índice) / zotero-mcp (solo consumidor
  MCP para Claude Desktop) sigue siendo limpio.
- **ADR 011** (ChromaDB bind mount) — amended: el mecanismo del bind
  mount y la convención de paths son idénticos, pero el flag del mount
  pasa de `:ro` a `:rw`. Ver la sección "Amendment" agregada a ADR 011.
- **ADR 004** (OpenAI `text-embedding-3-large`) — sin cambios. El
  embedder que elige ADR 004 ahora lo invoca S2 (el writer) en vez de
  `zotero-mcp`.
- **ADR 012** (APScheduler in-process + cron alternativa) — sin cambios.
  La reconciliación corre dentro del `run_fetch_cycle()` que ambos
  paths (APScheduler y cron via `zotai s2 fetch-once`) invocan; el
  comando adicional `zotai s2 reconcile` existe para disparos manuales
  de debug sin el RSS fetch.
