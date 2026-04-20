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

**S1 → S3 → S2**

Razones:

- S1 produce la biblioteca sobre la que opera todo lo demás. Bloqueante.
- S3 es barato de implementar (~4h) y **cierra un ciclo de valor mínimo**: apenas terminado S1+S3, el usuario ya tiene producto funcional para descubrimiento y cita. Posponerlo al final deja al usuario con 3-4 semanas de biblioteca sin forma de consultar.
- S3 funciona como **banco de validación para S2**: los mismos embeddings que expone S3 son los que usa S2 en su criterio de "similitud con corpus". Testear el scoring del S2 sin S3 operativo es a ciegas.
- S2 es el más complejo (~2-3 semanas de desarrollo incremental). Construirlo sobre base ya validada reduce riesgo.

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
| 007 | FastAPI + HTMX para dashboard S2 | HTMX evita SPA, renderizado server-side, más simple de mantener. Un único investigador hace cambios. |
| 008 | Cuarentena en S1 en vez de "todo o nada" | Resuelve tensión completitud vs calidad. Lo dudoso queda accesible pero marcado. |
| 009 | zotero-mcp usado por S3 **pero no por S1/S2** | S1/S2 usan la API de Zotero directa (pyzotero). MCP es para consumo conversacional. |

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
2. `plan_00_overview.md` (este documento)
3. `plan_glossary.md` (términos canónicos)
4. `plan_taxonomy.md` (clasificación de items)
5. `plan_01_subsystem1.md` (primer subsistema a construir)
6. `plan_03_subsystem3.md` (segundo, tras S1)
7. `plan_02_subsystem2.md` (tercero, tras S1+S3)
8. `docs/decisions/*.md` (contexto de decisiones históricas)
