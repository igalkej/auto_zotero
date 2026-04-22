# CLAUDE.md

Este archivo es leído automáticamente por Claude Code al iniciar sesión en este repositorio. Contiene reglas operativas permanentes, convenciones del proyecto, y contexto mínimo para que Claude actúe coherentemente entre sesiones.

---

## Identidad del proyecto

**Nombre**: zotero-ai-toolkit
**Propósito**: pipeline reproducible para que un investigador cargue un corpus de ~1000 PDFs a Zotero (Subsistema 1), mantenga la biblioteca sincronizada con journals de interés (Subsistema 2), y la consulte desde Claude Desktop vía MCP (Subsistema 3).

**Escenario de distribución**: α — repo compartido entre investigadores, cada uno corre el sistema contra su propia biblioteca Zotero personal. No hay servidor central, no hay biblioteca grupal.

**Owner técnico**: investigador único con experiencia Python. Los usuarios finales son investigadores con experiencia técnica media; algunos en Windows, otros en Linux. Todos asumen tener Docker Desktop instalado.

---

## Stack canónico (no negociar sin confirmación explícita)

- **Lenguaje**: Python 3.11
- **Gestor de dependencias**: `uv` (no pip, no poetry, no conda)
- **Distribución**: Docker + docker-compose
- **Storage local**: SQLite para estado del pipeline, ChromaDB para embeddings (via `zotero-mcp`)
- **Target de compatibilidad**: Windows 10/11 + WSL2, Linux (Ubuntu 22.04+), macOS (nice-to-have)
- **Dashboard S2**: FastAPI + HTMX + Jinja (renderizado server-side, nada de SPA)
- **LLM**: OpenAI API. Tagging con gpt-4o-mini, extracción con gpt-4o-mini, embeddings con text-embedding-3-large
- **Zotero**: API local preferida; API web como fallback
- **OCR**: ocrmypdf (wrapper sobre Tesseract 5), idiomas spa+eng
- **RSS**: feedparser
- **Testing**: pytest con fixtures, cobertura mínima 60% en código de lógica (no UI)

---

## Principios de diseño (aplicar en cada decisión)

1. **Idempotencia primero**: cualquier operación debe ser re-ejecutable sin romper estado. Estado persistente en SQLite, no en memoria.
2. **Fail-loud**: errores logueados con detalle, no silenciados. Un item que falla no rompe el pipeline; se reporta al final.
3. **Presupuestos explícitos**: toda operación con costo (API calls, tiempo) tiene límite configurable en `.env`. Al exceder, pausa y requiere confirmación.
4. **Dry-run como ciudadano de primera clase**: todo comando que modifique Zotero o la DB tiene `--dry-run` que imprime qué haría.
5. **Estado observable**: comando `status` retorna en texto plano cuántos items están en cada etapa, costos acumulados, ETA, errores pendientes.
6. **Cero sorpresas para el usuario**: antes de cualquier acción destructiva, confirmar. Antes de cualquier gasto >$1, confirmar.
7. **Cross-platform desde el código, no como parche**: no usar bash scripts, no hardcodear paths con `/`, usar `pathlib` siempre.

---

## Lo que este proyecto NO es (no proponer estas cosas)

- **No es PRISMA-compliant**: descubrimos que esto requeriría dedup, screening doble, trazabilidad. Fuera de alcance. Si el usuario insiste, referirlo a Rayyan o Covidence.
- **No es un sistema de notas / PKM**: no se integra con Obsidian, Roam, Logseq. Si se necesita, es otro proyecto.
- **No incluye análisis de citation networks en v1**: pospuesto.
- **No reemplaza el juicio académico del usuario**: las sugerencias de cita requieren verificación manual obligatoria.
- **No hay autenticación multiusuario, roles, permisos**: cada investigador es dueño único de su instancia.
- **No hay nube propia**: todo corre localmente (o Zotero cloud para sync personal).
- **No procesa PDFs no-académicos**: S1 Etapa 01 filtra explícitamente facturas, DNIs, tickets, manuales, etc. antes de gastar OCR / APIs en ellos. Ver `docs/plan_01_subsystem1.md` §3.1 para el clasificador (híbrido heurística + LLM gate). No remover el filtro sin ADR que lo justifique.

---

## Reglas operativas para Claude Code

### Reglas de código

- **Type hints obligatorios** en toda función pública. `mypy --strict` sobre `src/`.
- **Docstrings estilo Google** en módulos y funciones públicas.
- **Logging estructurado** (JSON) vía `structlog`. Nunca `print()` en código no-CLI.
- **Sin comentarios obvios**. Comentar solo el *por qué*, nunca el *qué*.
- **Funciones puras siempre que sea posible**. I/O aislado en módulos `api/` o `utils/fs.py`.
- **Magic strings en constantes**: nada de `if status == "done"` en el medio del código.
- **Excepciones específicas**: no `except Exception` excepto en handlers top-level.

### Reglas de commits

- Convenciones conventional commits: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`.
- Un commit = un cambio atómico. No mezclar refactor con feature.
- Mensaje en inglés (el código también).
- Todo cambio llega a `main` vía Pull Request desde una feature branch. Prohibido commitear o pushear directo a `main` (incluye cherry-pick, rebase-onto, merge manual). Un PR por unidad revisable.

### Reglas de interacción con el usuario

- **Antes de crear un archivo nuevo**, verificar que no existe uno con propósito similar en el repo.
- **Antes de instalar una dependencia**, verificar si la funcionalidad está cubierta por deps existentes.
- **Antes de agregar una feature no planeada en los `plan_*.md`**, preguntar al usuario.
- **Nunca commitear secretos**: `.env` en `.gitignore`, solo `.env.example` versionado.
- **Preservar el contexto entre sesiones**: actualizar `CHANGELOG.md` con cambios significativos; actualizar `docs/decisions/` con ADRs (Architecture Decision Records) cuando se tome decisión no trivial.

### Reglas sobre los documentos `plan_*.md`

- Son la especificación autoritativa del proyecto.
- Si Claude Code detecta una inconsistencia o un gap, **preguntar al usuario**, no resolver autónomamente.
- Si Claude Code propone un cambio al plan, hacerlo en PR separado de la implementación.

### Reglas sobre Docker

- El Dockerfile usa multi-stage build (stage de build con deps de compilación, stage final mínimo).
- No ejecutar como root en el container final.
- Volúmenes persistentes: `state.db`, `staging/`, `chroma_db/`. Todo lo demás es ephemeral.
- `docker-compose.yml` es la interfaz de entrada. No documentar comandos `docker run` directos salvo en troubleshooting.

---

## Estructura canónica del repo

```
zotero-ai-toolkit/
├── CLAUDE.md                    # este archivo
├── README.md                    # quickstart + qué es
├── CHANGELOG.md                 # cambios versión a versión
├── LICENSE
├── pyproject.toml               # deps + config de tools
├── uv.lock
├── .env.example
├── .gitignore
├── .dockerignore
├── docker-compose.yml
├── Dockerfile
│
├── src/
│   └── zotai/
│       ├── __init__.py
│       ├── config.py            # settings vía pydantic-settings
│       ├── cli.py               # typer app entry point
│       ├── state.py             # SQLite models (sqlmodel)
│       │
│       ├── s1/                  # subsistema 1 - retroactive
│       │   ├── __init__.py
│       │   ├── stage_01_inventory.py
│       │   ├── stage_02_ocr.py
│       │   ├── stage_03_import.py
│       │   ├── stage_04_enrich.py
│       │   ├── stage_05_tag.py
│       │   └── stage_06_validate.py
│       │
│       ├── s2/                  # subsistema 2 - prospective
│       │   ├── __init__.py
│       │   ├── feeds.py         # RSS ingestion
│       │   ├── scoring.py       # relevance criteria
│       │   ├── dashboard/       # FastAPI app
│       │   │   ├── __init__.py
│       │   │   ├── main.py
│       │   │   ├── routes.py
│       │   │   └── templates/
│       │   ├── worker.py        # scheduled job
│       │   └── push.py          # accepted → Zotero
│       │
│       ├── api/                 # adapters externos
│       │   ├── __init__.py
│       │   ├── zotero.py
│       │   ├── openalex.py
│       │   ├── semantic_scholar.py
│       │   └── openai_client.py
│       │
│       └── utils/
│           ├── __init__.py
│           ├── pdf.py
│           ├── fs.py
│           ├── logging.py
│           └── http.py          # httpx wrapper con retries
│
├── tests/
│   ├── conftest.py
│   ├── test_s1/
│   ├── test_s2/
│   └── fixtures/
│
├── docs/
│   ├── plan_00_overview.md      # arquitectura general
│   ├── plan_01_subsystem1.md    # captura retroactiva
│   ├── plan_02_subsystem2.md    # captura prospectiva
│   ├── plan_03_subsystem3.md    # acceso MCP
│   ├── plan_taxonomy.md         # taxonomía de tags
│   ├── plan_glossary.md         # glosario
│   ├── setup-windows.md
│   ├── setup-linux.md
│   ├── troubleshooting.md
│   ├── economics.md
│   └── decisions/               # ADRs
│       ├── 001-use-docker.md
│       ├── 002-sqlite-for-state.md
│       └── ...
│
└── scripts/                     # entry points amigables
    ├── run-pipeline.sh
    ├── run-pipeline.ps1
    └── healthcheck.py
```

**Importante**: nunca refactorizar esta estructura sin ADR.

---

## Contratos entre subsistemas

Los tres subsistemas se comunican **solo a través de Zotero**. Zotero es la fuente de verdad única.

```
S1 → Zotero ← S3 (read)
            ↑
S2 (write accepted items)
```

No hay DB compartida entre S1 y S2. No hay API interna entre subsistemas. Si necesitás que S2 "sepa algo" de S1, ese algo tiene que estar en Zotero (como tag, colección, o campo).

**Consecuencia**: S2 y S3 deben poder correr aunque S1 no haya corrido nunca. Degradan gracefully.

---

## Presupuestos de referencia

| Recurso | Budget | Alarm |
|---|---|---|
| Costo total S1 por usuario | $5 | $10 |
| Costo mensual S2 por usuario | $2 | $5 |
| Costo S3 (embeddings iniciales) | $2 | $5 |
| Tiempo humano S1 | 2-3h | 5h |
| Tiempo semanal S2 (triage) | 20 min | 40 min |
| Disk usage por 1500 papers | 8 GB | 15 GB |

---

## Contacto y escalación

Si Claude Code se encuentra con una decisión no cubierta en los plan_*.md:

1. Buscar en `docs/decisions/` si hay un ADR previo.
2. Si no, **preguntar al usuario**. No resolver autónomamente si el impacto es >1 hora de trabajo.
3. Si la respuesta del usuario sienta precedente, crear un ADR en `docs/decisions/NNN-titulo.md`.

---

## Versión de este archivo

v1.0 — setup inicial del proyecto. Actualizar al hacer cambios significativos a reglas o stack.
