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
Cálculo de tres sub-scores (`tags`, `semantic`, `queries`) y su combinación ponderada en `score_composite`.

**State DB / state.db** (S1)
SQLite con el estado del pipeline S1. Ubicación: `/workspace/state.db` dentro del container.

**Candidates DB / candidates.db** (S2)
SQLite con candidates, feeds, queries. Separado del `state.db`. Ubicación: `/workspace/candidates.db`.

**Chroma DB**
Base vectorial gestionada por `zotero-mcp` para embeddings. Ubicación canónica: `~/.config/zotero-mcp/chroma_db/`. S3 la escribe, S2 la lee.

**Ruta A/B/C** (S1 Etapa 03)
Las tres estrategias de import:
- **A**: import por DOI directo.
- **B**: import por PDF → Zotero recognizer extrae DOI.
- **C**: import como huérfano, bajar a enrichment.

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
