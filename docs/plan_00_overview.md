# plan_00_overview.md — Arquitectura general

**Propósito de este documento**: dar contexto suficiente a Claude Code (y a cualquier desarrollador nuevo) para entender qué construye el proyecto, por qué, y cómo se organiza. Los detalles de implementación están en `plan_01`, `plan_02`, `plan_03`.

---

## 1. Problema que resuelve

Investigador tiene ~1000 PDFs académicos sueltos, en carpetas, sin gestor de referencias. Quiere:

1. **Migrar** todo a una biblioteca estructurada (Zotero) con metadata correcta.
2. **Mantenerla actualizada** automáticamente con publicaciones nuevas de journals que sigue.
3. **Consultarla** vía Claude Desktop durante el trabajo de investigación/escritura.

Objetivo de más alto nivel: **multiplicar por 3-5x la cantidad de consultas bibliográficas** que hace el investigador, ampliando la cobertura del corpus que efectivamente usa.

---

## 2. Principio rector

**El corazón del producto es la captura prospectiva (Subsistema 2).** La captura retroactiva (S1) es el arranque. El acceso (S3) es la superficie de uso. Cualquier decisión de diseño se evalúa contra si mejora o degrada la cobertura útil del corpus.

**Cobertura útil** = papers relevantes que están en la biblioteca Y son recuperables cuando se necesitan.

---

## 3. Arquitectura de tres subsistemas

```
┌─────────────────────────────────────────────────────────────┐
│  ZOTERO (fuente de verdad única, bibliotecas personales)    │
│  • Colección principal                                       │
│  • Colección "Quarantine" (items con metadata parcial S1)    │
│  • Colección "Inbox"      (candidatos S2 pendientes triage)  │
└─────────────────────────────────────────────────────────────┘
                 ▲                    ▲                ▲
                 │ write              │ write          │ read
                 │                    │                │
┌────────────────┴──────┐  ┌─────────┴────────┐  ┌────┴─────┐
│  SUBSISTEMA 1 (S1)    │  │  SUBSISTEMA 2    │  │  S3      │
│  Retrospective        │  │  Prospective     │  │  MCP     │
│                       │  │                  │  │  access  │
│  One-shot CLI pipe    │  │  Dashboard +     │  │          │
│  • OCR                │  │  scheduled worker│  │ zotero-  │
│  • Ingesta            │  │  • RSS feeds     │  │   mcp    │
│  • Enrichment         │  │  • Scoring       │  │          │
│  • Tagging            │  │  • Triage UI     │  │ Claude   │
│  • Validation         │  │  • Push to Z.    │  │ Desktop  │
│                       │  │                  │  │          │
│  Corre ~1 vez         │  │  Corre semanal   │  │ Ad hoc   │
└───────────────────────┘  └──────────────────┘  └──────────┘
```

**Comunicación entre subsistemas**: solo a través de Zotero. No hay DB compartida ni API interna. Esto es deliberado: loose coupling, Zotero ya resuelve persistencia, permisos, y sync.

---

## 4. Orden de implementación

**S1 → S2 → S3** (orden de valor entregado)

Bajo ADR 015 (S2 es owner del índice de embeddings; S3 es lector puro), hasta que S2 corra `zotai s2 backfill-index` el ChromaDB está vacío y las queries del MCP en Claude Desktop devuelven resultados vacíos. Por eso el orden lineal es S1 → S2 → S3.

Razones por hito:

- **S1** produce la biblioteca sobre la que opera todo lo demás. Bloqueante para S2 y S3.
- **S2** llena ChromaDB con su primer `zotai s2 backfill-index` (Sprint 1). Bajo ADR 015 absorbe la responsabilidad del indexador de embeddings (`src/zotai/s2/indexing.py`) que mantiene el invariante "todo item no-cuarentenado en Zotero está en ChromaDB" via reconciliación por diff en cada ciclo del worker. Es el más complejo (~2-3 semanas incrementales); los sprints 2/3/4 agregan triage UI, scoring, push y scheduling sobre la base de Sprint 1.
- **S3** en esta fase significa **setup del servidor MCP** para Claude Desktop: instalar `zotero-mcp`, configurar `claude_desktop_config.json`, y dejarlo corriendo. **No** se ejecuta `zotero-mcp update-db` — bajo ADR 015 S2 es el owner del índice y `update-db` no se usa nunca en el flujo operativo. Setup barato (~0.5d). El primer producto funcional para descubrimiento y cita es S1 + S2 Sprint 1 + `backfill-index` + S3.

**Dependencias técnicas vs orden de valor**: las issues #11 (S3) y #12 (S2 Sprint 1) declaran que ninguna depende de la otra a nivel de código — S2 no necesita el MCP server configurado, y S3 sólo necesita la biblioteca de S1 poblada. El setup de S3 (docs + config + scripts) puede empaquetarse en paralelo con S2 Sprint 1 si conviene operativamente. Pero el orden de valor entregado al usuario sigue siendo S1 → S2 → S3.

---

## 5. Decisiones arquitectónicas clave

Cada una con ADR correspondiente en `docs/decisions/`.

| # | Decisión | Racional |
|---|---|---|
| 001 | Docker como medio de distribución | Repo compartido, cross-platform. Reduce onboarding de minutos a horas en users no-técnicos. |
| 002 | SQLite para estado del pipeline | Zero-setup, inspectable, idempotencia trivial. |
| 003 | Escenario α (bibliotecas personales) | Discutido con usuario; β/γ requieren coordinación que no es necesaria inicialmente. |
| 004 | OpenAI text-embedding-3-large | Corpus mix es/en requiere embedder multilingual. Default de zotero-mcp (MiniLM) degrada 20+ puntos en queries en español. |
| 005 | gpt-4o-mini para tagging/extracción | Calidad suficiente, $0.00042/paper. $2 para toda la biblioteca. |
| 006 | zotero-mcp (54yyyu) para S3 | Existe, estable, cubre los 3 modos out-of-the-box. Build propio no justificado. |
| 007 | FastAPI + HTMX para dashboard S2 _(pendiente — ver [#12](https://github.com/igalkej/auto_zotero/issues/12))_ | HTMX evita SPA, renderizado server-side, más simple de mantener. Un único investigador hace cambios. ADR escrito como entregable de S2 Sprint 1. |
| 008 | Cuarentena en S1 en vez de "todo o nada" | Resuelve tensión completitud vs calidad. Lo dudoso queda accesible pero marcado. |
| 009 | zotero-mcp usado por S3 **pero no por S1/S2** | S1/S2 usan la API de Zotero directa (pyzotero). MCP es para consumo conversacional. **Parcialmente superseded por ADR 015** en lo que hace al ChromaDB: S2 ahora también escribe directo al índice (sin pasar por `zotero-mcp update-db`), aunque sigue usando pyzotero para Zotero. |
| 010 | Ruta A usa OpenAlex para DOI → metadata (no el translator de Zotero) | Translator de Zotero es API no pública / frágil entre versiones. OpenAlex cubre >98% DOIs académicos con API estable. |
| 011 | ChromaDB compartida via bind mount Docker (no copia, no sync job) | Una sola path canónica `/workspace/chroma_db`, host-side configurable, mount `:rw` para que S2 escriba (amended por ADR 015 — originalmente `:ro`). |
| 012 | APScheduler in-process default; cron / Task Scheduler como alternativa | Default que matchea el caso 80% (dashboard up); cron sirve a usuarios que cierran el dashboard a diario. Misma función `run_fetch_cycle()` desde ambos paths. |
| 013 | Bridge networking + `host.docker.internal` en lugar de `network_mode: host` | `network_mode: host` no funciona en Docker Desktop Mac/Win; bridge + `extra_hosts: host-gateway` es uniforme cross-platform. |
| 014 | Stage 03 dedup: skip attach si el item existente ya tiene PDF | Respeta estado curado del usuario; agrega valor cuando solo había metadata sin PDF. |
| 015 | **S2 es owner del índice de embeddings; S3 es lector puro** | Invierte ADR 006/009 parcialmente. Elimina trigger externo (cron / on-use) y la ventana de staleness. S2 mantiene el invariante via reconciliación por diff en cada ciclo del worker. Ver ADR 015. |
| 016 | Reciprocal Rank Fusion default para `score_composite` de S2 | Promedio ponderado con pesos arbitrarios entierra señales ortogonales (ej. `queries=0.9, tags=0.1, semantic=0.1`). RRF (k=60) es rank-based, sin pesos pre-datos, favorece rank-alto en cualquier criterio. Weighted-mean queda como opt-in. |
| 017 | Hybrid retrieval (BM25 + dense) para `score_queries` de S2 | Queries persistentes son cortas (3-7 tokens); dense puro underperforma por 5-15 recall points. SQLite FTS5 built-in provee BM25 sin nueva dep. α=0.4 literatura default; calibrar con ADR sucesor post-datos. |
| 018 | Stage 04 cascade: agregar substages 04bs (SciELO) + 04bd (DOAJ) entre 04b y 04c | Cierra el gap LATAM/open-access del cascade gratis para corpus CONICET. Default ON con opt-out via `S1_ENABLE_SCIELO`/`S1_ENABLE_DOAJ`. REDIB / RedALyC / La Referencia / ERIH PLUS / Scopus quedan fuera con justificación documentada. **Amended por ADR 019** — 04bs implementa via Crossref Member 530 porque `search.scielo.org` está cerrado a clientes anónimos. |
| 019 | Substage 04bs implementa via Crossref Member 530 (no `search.scielo.org`) | El endpoint Solr público de SciELO devuelve 403 a httpx anónimo en toda variante; ArticleMeta sólo soporta lookup por SciELO PID. Member 530 es el ID Crossref de SciELO; el filter narrows el search space y mejora ranking top-5 LATAM-Spanish vs OpenAlex sin filtro. Amends ADR 018 §"Sources evaluated" SciELO row + §Decision §1 + §"Implementation artefacts" §1. |

---

## 6. Criterios de éxito del producto completo

A 30 días de uso:

- S1: >90% del corpus con metadata completa en biblioteca principal, resto en cuarentena.
- S2: precision del filtro >50%, volumen 10-30 candidatos/semana.
- S3: >3x consultas bibliográficas vs baseline, satisfacción subjetiva >70%.
- A 90 días: >5 papers/mes incorporados vía S2 que no se hubieran encontrado por canales tradicionales.

---

## 7. Stack técnico canónico

Ver `CLAUDE.md` sección "Stack canónico". En resumen:

- Python 3.11 + uv
- Docker multi-stage
- Zotero 7 + Better BibTeX + zotero-mcp
- SQLite + ChromaDB
- FastAPI + HTMX + Jinja
- OpenAI API (LLM + embeddings)
- OpenAlex + Semantic Scholar (metadata enrichment, gratis)

---

## 8. Lo que NO es este proyecto

Listado en `CLAUDE.md`. Resumen: no es PRISMA, no es PKM, no es citation network analysis, no es multi-tenant. Un usuario, una biblioteca, un scope acotado.

---

## 9. Lectura ordenada para onboarding

1. `CLAUDE.md` (reglas operativas)
2. `docs/plan_00_overview.md` (este documento)
3. `docs/plan_glossary.md` (términos canónicos)
4. `docs/plan_taxonomy.md` (clasificación de items)
5. `docs/plan_01_subsystem1.md` (primer subsistema a construir)
6. `docs/plan_03_subsystem3.md` (segundo, tras S1)
7. `docs/plan_02_subsystem2.md` (tercero, tras S1+S3)
8. `docs/decisions/*.md` (contexto de decisiones históricas)
