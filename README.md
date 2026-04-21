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

## Estado del proyecto

| Subsistema | Estado | Plan |
|---|---|---|
| S1 – Retroactive | 🟡 Spec, pendiente implementación | `docs/plan_01_subsystem1.md` |
| S3 – MCP access | 🟡 Spec, pendiente implementación | `docs/plan_03_subsystem3.md` |
| S2 – Prospective | 🟡 Spec, pendiente implementación | `docs/plan_02_subsystem2.md` |

---

## Quickstart (cuando esté implementado)

```bash
git clone <repo>
cd zotero-ai-toolkit
cp .env.example .env
# editar .env con tus credenciales: ZOTERO_API_KEY, OPENAI_API_KEY, PDF_SOURCE_FOLDERS

# Arrancar S1 (carga inicial)
docker compose run onboarding zotai s1 run-all

# Configurar S3 (MCP para Claude Desktop) - guía manual
# ver docs/s3-setup.md

# Arrancar S2 (dashboard + worker)
docker compose up dashboard
# abrir http://localhost:8000
```

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

| Concepto | One-time | Mensual |
|---|---|---|
| S1 (APIs durante carga) | ~$2 | — |
| S2 (APIs scoring) | — | ~$2 |
| S3 (embeddings iniciales) | ~$2 | — |
| Claude Pro (para usar MCP) | — | $20 |

Tiempo humano: ~3h carga inicial + ~20 min/semana de triage.

---

## Licencia

[TBD por el usuario]

---

## Contribución

Proyecto de uso interno de un grupo de investigación. Escenario α: repo compartido, bibliotecas personales.

Ver `CLAUDE.md` para convenciones de código y commits.
