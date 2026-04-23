# zotero-ai-toolkit

Pipeline reproducible para gestionar una biblioteca Zotero con IA:
**carga masiva inicial + captura automática de journals + consulta via Claude Desktop**.

---

## TL;DR

Un investigador con ~1000 PDFs sueltos, que quiere:
1. Cargarlos a Zotero con metadata correcta (one-shot).
2. Que su biblioteca se mantenga al día con journals nuevos (semanal, semi-automático).
3. Consultar todo desde Claude Desktop.

Tres subsistemas. Docker para distribución cross-platform.

---

## Arquitectura

```
      S1: Carga retroactiva       S3: Acceso MCP
      (one-shot CLI)              (Claude Desktop)
              ↓                         ↑
        ┌──────────────────────────────────┐
        │         ZOTERO LIBRARY           │
        └──────────────────────────────────┘
                    ↑
      S2: Captura prospectiva (scheduled worker + dashboard web)
```

Ver `docs/plan_00_overview.md` para el detalle.

---

## Cómo queda tu biblioteca Zotero

El sistema maneja **3 collections** al nivel de Zotero y **2 dimensiones de tags** planas — nada más. Geografía, tipo de documento y año van en campos nativos de Zotero, no se duplican como tags.

### Collections (manejadas automáticamente)
- **Biblioteca principal** (root): items con metadata completa, el corazón de tu biblioteca utilizable.
- **Quarantine**: items que S1 no pudo enriquecer con calidad suficiente. Quedan accesibles pero fuera de las búsquedas por default.
- **Inbox S2**: donde S2 empuja candidatos aceptados por triage (cuando esté implementado).

Si querés colecciones por proyecto de investigación (p.ej. "Tesis doctoral", "Paper inflación 2026"), las creás a mano en Zotero — el sistema no las gestiona.

### Tags (dos dimensiones, 25-40 total)
- **TEMA** (2-4 por paper): sustantivo del paper — `macro-fiscal`, `informalidad`, `mercado-laboral`, etc.
- **METODO** (1-2 por paper): cómo aborda el problema — `empirico-rct`, `empirico-quasi-exp`, `teorico-analitico`, etc.

Definidas en `config/taxonomy.yaml`; taxonomía completa y reglas en `docs/plan_taxonomy.md`. El archivo hoy viene con plantilla de economía / LATAM que el investigador debe customizar antes de correr Etapa 05 (tagging).

### Campos nativos de Zotero (no duplicar como tags)
- `Place` → país / región del estudio.
- `Item Type` → `journalArticle`, `book`, `thesis`, `report`, `preprint`, `conferencePaper`, etc.
- `Date` → año.
- `Publication` → journal / editorial.

### Filtrado de PDFs no-académicos
S1 Etapa 01 incluye un **clasificador upstream** que separa papers de material de descarte (facturas, DNIs, tickets, manuales). Estrategia híbrida: heurística positiva (DOI/arXiv/ISBN/keywords académicos) para accept inmediato, heurística negativa (pocas páginas + keywords de facturación) para reject inmediato, LLM gate (gpt-4o-mini, ~$0.12 por 1000 PDFs) solo para los ambiguos. Los rechazados quedan en un CSV separado y **nunca entran al pipeline** — no se les gasta OCR ni llamadas a APIs.

Ver `docs/plan_01_subsystem1.md` §3.1 para el detalle del clasificador.

---

## Estado del proyecto

| Subsistema | Estado | Plan |
|---|---|---|
| S1 – Retroactive | 🟢 Funcional end-to-end (Stages 01-06 + `run-all` + `status` + Docker + setup docs). Taxonomía pendiente de customizar por cada investigador antes de aplicar tags reales. | `docs/plan_01_subsystem1.md` |
| S3 – MCP access | 🟡 Spec, pendiente implementación ([#11](https://github.com/igalkej/auto_zotero/issues/11)) | `docs/plan_03_subsystem3.md` |
| S2 – Prospective | 🟡 Spec, pendiente implementación ([#12](https://github.com/igalkej/auto_zotero/issues/12)–[#15](https://github.com/igalkej/auto_zotero/issues/15)) | `docs/plan_02_subsystem2.md` |

---

## Quickstart

```bash
git clone https://github.com/igalkej/auto_zotero.git zotero-ai-toolkit
cd zotero-ai-toolkit
cp .env.example .env
# editar .env con tus credenciales: ZOTERO_API_KEY, ZOTERO_LIBRARY_ID,
# OPENAI_API_KEY, PDF_SOURCE_FOLDERS, USER_EMAIL

# Status — siempre seguro; no escribe nada.
docker compose --profile onboarding run --rm onboarding zotai s1 status

# Pipeline completo S1 (interactivo, prompts entre etapas).
./scripts/run-pipeline.sh          # Linux / macOS
.\scripts\run-pipeline.ps1         # Windows / PowerShell

# Variantes útiles:
./scripts/run-pipeline.sh --yes                       # sin prompts
./scripts/run-pipeline.sh --yes --tag-mode preview    # stop antes de Stage 06
./scripts/run-pipeline.sh --allow-template-taxonomy   # testing sin customizar taxonomía

# Configurar S3 (MCP para Claude Desktop) — guía manual
# ver docs/s3-setup.md (pendiente — Phase 10, #11)

# Arrancar S2 dashboard + worker (pendiente — Phases 11-14, #12-#15)
docker compose up dashboard
# abrir http://localhost:8000
```

Setup guiado por OS: [`docs/setup-linux.md`](docs/setup-linux.md) /
[`docs/setup-windows.md`](docs/setup-windows.md). Problemas comunes:
[`docs/troubleshooting.md`](docs/troubleshooting.md).

---

## Prerequisitos

- Docker Desktop (Windows/macOS) o Docker Engine (Linux).
- Zotero 7 instalado, con API local habilitada (Settings → Advanced).
- Cuenta Zotero con API key.
- OpenAI API key.
- Claude Desktop (para S3).

---

## Lectura para desarrollo

1. `CLAUDE.md` — reglas del proyecto (auto-leído por Claude Code).
2. `docs/plan_00_overview.md` — arquitectura.
3. `docs/plan_glossary.md` — vocabulario canónico.
4. `docs/plan_taxonomy.md` — tags (requiere completarse).
5. `docs/plan_01/02/03_subsystem*.md` — specs por subsistema.

---

## Presupuestos estimados

| Concepto | Estimado one-time | Estimado mensual | Hard cap |
|---|---|---|---|
| S1 (APIs durante carga) | ~$2 | — | $5 (alarma $10) |
| S2 (APIs scoring) | — | ~$2 | $5/mes |
| S3 (embeddings iniciales) | ~$2 | — | (compartido con S1) |
| Claude Pro (para usar MCP) | — | $20 | — |

**Estimado** = gasto típico observado en uso real. **Hard cap** = tope duro configurable en `.env` (`MAX_COST_USD_TOTAL`, `MAX_COST_USD_STAGE_*`, `S2_MAX_COST_USD_MONTHLY`); el pipeline aborta al superarlo.

Tiempo humano: ~3h carga inicial + ~20 min/semana de triage.

---

## Licencia

[TBD por el usuario]

---

## Contribución

Proyecto de uso interno de un grupo de investigación. Escenario α: repo compartido, bibliotecas personales.

Ver `CLAUDE.md` para convenciones de código y commits.
