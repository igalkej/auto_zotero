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
| S2 – Prospective | 🟡 Spec, pendiente implementación ([#12](https://github.com/igalkej/auto_zotero/issues/12)–[#15](https://github.com/igalkej/auto_zotero/issues/15)) | `docs/plan_02_subsystem2.md` |
| S3 – MCP access | 🟡 Spec, pendiente implementación ([#11](https://github.com/igalkej/auto_zotero/issues/11)) | `docs/plan_03_subsystem3.md` |

Orden de implementación (plan_00 §4): **S1 → S2 → S3**. S2 es owner del
índice de embeddings (ADR 015); S3 es lector puro y arranca a darle
valor al modo descubrimiento una vez que el primer `zotai s2
backfill-index` haya corrido.

---

## Quickstart (TL;DR)

```bash
git clone https://github.com/igalkej/auto_zotero.git zotero-ai-toolkit
cd zotero-ai-toolkit
cp .env.example .env   # completar credenciales
./scripts/run-pipeline.sh --yes --tag-mode preview --allow-template-taxonomy
```

Si es la primera vez, leé la [**Guía de uso S1**](#guía-de-uso--s1-de-punta-a-punta)
abajo antes de correr. Instalación detallada por OS:
[`docs/setup-linux.md`](docs/setup-linux.md) /
[`docs/setup-windows.md`](docs/setup-windows.md). Problemas comunes:
[`docs/troubleshooting.md`](docs/troubleshooting.md). Costos esperados:
[`docs/economics.md`](docs/economics.md).

---

## Guía de uso — S1 de punta a punta

Esta guía asume que ya instalaste Docker y Zotero según
[`docs/setup-linux.md`](docs/setup-linux.md) o
[`docs/setup-windows.md`](docs/setup-windows.md). Cubre cada paso
necesario para llevar tu corpus de PDFs a Zotero con metadata + tags
completos. La corrida típica demora **2-3 horas de wall-clock** para
~1000 PDFs (de las cuales apenas ~20 min requieren tu atención
— el resto es OCR + llamadas a APIs corriendo en background).

### Checklist previo

Antes de arrancar, asegurate de tener:

- [ ] **Docker Desktop o Docker Engine** corriendo (`docker --version && docker compose version`).
- [ ] **Zotero 7** instalado y **abierto**, con Settings → Advanced → *Allow other applications on this computer to communicate with Zotero* marcado.
- [ ] **API key de Zotero** con permisos read+write (https://www.zotero.org/settings/keys).
- [ ] **`library_id` de Zotero** — el userID numérico que aparece en la misma página de keys.
- [ ] **API key de OpenAI** (https://platform.openai.com/api-keys). Presupuesto esperado S1: <$2 por corpus de 1000 PDFs.
- [ ] **Corpus de PDFs** accesible desde tu máquina. Puede ser una carpeta, varias, o un árbol anidado.
- [ ] **Backup de tu biblioteca Zotero actual** (File → Export Library → Zotero RDF) — el pipeline es conservador y tiene `--dry-run`, pero backup nunca está de más.

### Paso 1 — Clonar el repositorio

```bash
git clone https://github.com/igalkej/auto_zotero.git zotero-ai-toolkit
cd zotero-ai-toolkit
```

Todos los comandos siguientes se corren desde la raíz del repo.

### Paso 2 — Crear y rellenar el archivo `.env`

```bash
cp .env.example .env
```

Editá `.env` y completá **como mínimo** estos campos:

```bash
# ── Zotero ────────────────────────────────────────────────────
ZOTERO_API_KEY=<tu-api-key>             # de zotero.org/settings/keys
ZOTERO_LIBRARY_ID=<tu-user-id>          # el número, no el nombre
ZOTERO_LIBRARY_TYPE=user                # o "group" si vas a migrar a un grupo
ZOTERO_LOCAL_API=true                   # usar la API local (requiere Zotero abierto)

# ── OpenAI ────────────────────────────────────────────────────
OPENAI_API_KEY=sk-...

# ── Paths (dentro del container) ──────────────────────────────
PDF_SOURCE_FOLDERS=/data/sandbox        # ajustar en el paso 3

# ── OpenAlex polite pool (10× rate limit) ─────────────────────
USER_EMAIL=tu@email.com                 # tu email real; OpenAlex lo usa
                                        # sólo para User-Agent
```

El resto de los campos pueden quedar en sus defaults. Revisá
[`.env.example`](.env.example) para ver todo lo configurable
(budgets por etapa, OCR parallelism, cascade de PDF para S2, etc.).

**Regla de seguridad**: `.env` está en `.gitignore`. Nunca lo
commiteés. Sólo `.env.example` va a git.

### Paso 3 — Organizar los PDFs de origen

El container monta `./data/` (relativo a la raíz del repo) en `/data`
read-only. La variable `PDF_SOURCE_FOLDERS` en `.env` lista las rutas
**dentro del container** que el scanner va a recorrer recursivamente.

Tres maneras de hacer que tu corpus quede bajo `./data/`:

**Opción A — Symlinks** (Linux / macOS, recomendado):
```bash
ln -s ~/Downloads/papers       data/downloads
ln -s ~/Dropbox/Investigacion  data/dropbox
```
Y en `.env`:
```bash
PDF_SOURCE_FOLDERS=/data/downloads,/data/dropbox
```

**Opción B — Copia** (Windows, o si no confiás en symlinks):
```bash
mkdir -p data/sandbox
cp ~/Downloads/papers/*.pdf data/sandbox/
```
Y en `.env`:
```bash
PDF_SOURCE_FOLDERS=/data/sandbox
```

**Opción C — Bind mount adicional** (si el corpus vive en un path
fijo afuera del repo): editá `docker-compose.yml` y agregá una línea
a los `volumes:` del servicio `onboarding`:
```yaml
volumes:
  - ./workspace:/workspace
  - ./config:/app/config
  - ./data:/data:ro
  - /ruta/absoluta/a/papers:/data/externa:ro
```
Y en `.env`: `PDF_SOURCE_FOLDERS=/data/externa`.

Verificá que los PDFs efectivamente son PDFs: el Stage 01 clasificador
filtra por magic bytes, pero es más rápido para vos si descartás
antes `.doc`, `.djvu`, imágenes, etc.

### Paso 4 — (Opcional) Customizar la taxonomía de tags

`config/taxonomy.yaml` viene con una plantilla de ~30 tags para
economía / ciencias sociales LATAM. Hay **dos caminos**:

**Si tu dominio es economía / LATAM**: podés usar la plantilla tal
cual para una primera corrida. Editá `config/taxonomy.yaml` y
cambiá `status: template` a `status: customized`. Agregás / sacás
tags según te parezca después.

**Si tu dominio es otro** (biomedicina, CS, derecho, física, etc.):
**reemplazá** las listas `tema:` y `metodo:` por tu vocabulario. Cada
entrada necesita un `id` (kebab-case, sin acentos), una `description`
de una línea, y una lista `synonyms` para orientar al LLM. Apuntá a
25-40 tags totales en las dos dimensiones combinadas — más que eso
genera vocabulario inflado, menos que eso fuerza tags demasiado
amplios.

**Si preferís no customizar ahora**: podés correr con la plantilla
tal cual en modo `--preview` + `--allow-template-taxonomy`. El pipeline
va a generar sugerencias de tags pero no las aplicará a Zotero hasta
que re-corras con la taxonomía real. Esto es útil para la primera
vuelta — ver el flujo entero sin comprometer tags que no te sirven.

La guía de diseño detallada está en
[`docs/plan_taxonomy.md`](docs/plan_taxonomy.md).

### Paso 5 — Verificar que todo está en pie

```bash
# Verificar que el .env se parsea sin errores y que state.db va a
# aterrizar donde esperás. Siempre seguro de correr.
docker compose --profile onboarding run --rm onboarding zotai s1 status
```

La primera vez tarda ~2 min porque `docker compose build` baja ~800 MB
de imagen base + deps (tesseract + ocrmypdf + pyzotero + pydantic …).
Corridas siguientes son instantáneas.

Output esperado en la primera corrida:

```
zotai s1 status — 2026-04-23 12:00:00 UTC

state.db: /workspace/state.db (not created yet)
credentials: openai=yes  zotero=yes
total items: 0
  in quarantine: 0    needs review: 0
  with zotero key: 0    tagged: 0
  with last_error: 0

items by stage_completed:
  0  not started            0
  1  inventory              0
  ...
  6  validate               0

last run: (none)
```

Si `credentials: openai=no` o `zotero=no`, volvé al Paso 2 —
el `.env` no se está leyendo.

### Paso 6 — Corrida de prueba con un corpus chico (recomendado)

**Antes de apuntar al corpus completo**, copiá 5-20 PDFs a
`./data/sandbox/` y corré una pasada en modo preview. Esto ejercita
el pipeline entero sin comprometer nada a Zotero todavía.

```bash
# Apuntar .env al sandbox:
# PDF_SOURCE_FOLDERS=/data/sandbox

# Correr:
./scripts/run-pipeline.sh --yes --tag-mode preview --allow-template-taxonomy
```

Qué hace cada flag:

- `--yes` — no pide confirmación entre etapas; corre las 6 seguidas.
- `--tag-mode preview` — Stage 05 genera `tag_report_<ts>.csv` con
  las tags sugeridas pero **no** las aplica a Zotero. `run-all`
  termina después de Stage 05 (no corre Stage 06 hasta que haya
  tags reales).
- `--allow-template-taxonomy` — permite correr contra la plantilla
  sin customizar. Sin este flag, Stage 05 aborta con un mensaje
  claro pidiendo `status: customized`.

Durante la corrida verás un log tipo:

```
[01/06] inventory: processed=12 duplicates=0 invalid=1 excluded=2 cost=$0.0004
[02/06] ocr: processed=5 failed=0 applied=5 resumed=0
[03/06] import: processed=10 failed=0 route_a=6 route_c=4 deduped=0
[04/06] enrich: processed=4 failed=0 04a=2 04b=1 04c=0 04d=1 quarantined=0
[05/06] tag: processed=10 failed=0 tagged=0 previewed=10 llm_failed=0 cost=$0.0042
```

### Paso 7 — Revisar los reportes

Todos los reportes aterrizan en `./workspace/reports/`. Los que más
interesan tras la corrida de prueba:

- **`inventory_report_<ts>.csv`** — cada PDF scaneado con su clasificación.
  Verificar que no se estén filtrando papers reales por la rama
  "reject" del clasificador (columna `classification=excluded` con
  `rejection_reason` explícito).
- **`excluded_report_<ts>.csv`** — lo que se rechazó. Si incluye
  papers académicos reales, ajustá: el clasificador usa keywords en
  páginas 1-3; un paper con keyword `factura` en un footnote puede
  caer acá. Issue tracker si pasa mucho.
- **`import_report_<ts>.csv`** — qué ruta tomó cada item (A = DOI a
  OpenAlex, C = attachment huérfano pendiente de enrichment).
- **`enrich_report_<ts>.csv`** — cómo el cascade resolvió los Route-C.
  `substage_resolved=04a/b/c/d` = éxito; `status=quarantined_04e` =
  no se pudo recuperar metadata (quedó en la collection Quarantine).
- **`quarantine_report_<ts>.csv`** — columna `text_snippet` con
  primeros 200 chars del PDF, para decidir a mano si vale la pena
  chasearlo. Típicamente <10% para corpus anglo, <25% para LATAM-pesado.
- **`tag_report_<ts>.csv`** — sugerencias del LLM, columna
  `tema_rejected` / `metodo_rejected` muestra ids que inventó fuera
  de la taxonomía (señal de que el dominio no matchea).

Correr también el reporte consolidado:

```bash
docker compose --profile onboarding run --rm onboarding \
    zotai s1 validate --open-report
```

Abre `s1_validation_<ts>.html` en el browser — navegable, con links
directos a Zotero para cada item flagged. **Lee las 7 secciones** y
decidí si la calidad es suficiente antes de correr con el corpus
completo.

### Paso 8 — Corrida final con todo el corpus

Una vez que la prueba con sandbox salió bien:

1. Customizá `config/taxonomy.yaml` si no lo habías hecho.
   Cambiá `status: template` → `status: customized`.
2. Apuntá `.env` al corpus real:
   ```bash
   PDF_SOURCE_FOLDERS=/data/downloads,/data/dropbox
   ```
3. (Opcional) Ajustá budgets si tu corpus es LATAM-heavy:
   ```bash
   MAX_COST_USD_STAGE_04=4.00   # default 2.00 (ver docs/economics.md)
   ```
4. **Corrida final**:
   ```bash
   ./scripts/run-pipeline.sh --yes
   ```
   Sin flags adicionales = Stage 05 aplica tags a Zotero y Stage 06
   genera el validation report final.

Durante la corrida tené Zotero Desktop **abierto** — el pipeline
habla con la API local en `localhost:23119`. Si lo cerrás mid-run,
Stage 03 aborta limpio con un mensaje claro; volvés a abrir Zotero y
re-corrés `./scripts/run-pipeline.sh --yes` — resume desde donde quedó.

### Paso 9 — Revisar el validation report final

Al terminar, abrí:

```bash
open workspace/reports/s1_validation_<ts>.html   # macOS
xdg-open workspace/reports/s1_validation_<ts>.html  # Linux
start workspace\reports\s1_validation_<ts>.html  # Windows
```

Qué chequear:

- **§1 Completeness**: ≥90% con Zotero key, ≥90% con metadata,
  ≥80% con tags, ≥95% con fulltext. Si muy por debajo, algo falló
  en etapas anteriores — mirar `zotai s1 status` para ver dónde.
- **§2 Tag distribution**: ver orphan tags (<3 usos, candidatos a
  sacar de la taxonomía) y dominant tags (>30% del corpus, candidatos
  a subdividir).
- **§3 Consistency issues**: papers sin título, sin autores, o con
  año fuera de rango. Siempre revisar manualmente — links a Zotero
  incluidos.
- **§4 Potential duplicates**: pares con título casi-idéntico y
  mismo año. Decisión humana: a veces son preprint + published del
  mismo paper (quedate con la versión published); a veces son falsos
  positivos (fuzz >90 sobre títulos similares).
- **§5 Stage 01 filtering**: cuántos PDFs se descartaron + razón.
  Validar que no se filtró nada real.
- **§6 Costs**: total gastado. Contrastar con `docs/economics.md`.
- **§7 Timings**: wall-clock por etapa. Útil para la próxima corrida.

### Paso 10 — Qué hacer si algo falla

**La regla general**: cada etapa commitea por-item. Si el proceso
se corta, re-ejecutar el comando exacto resume sin duplicar trabajo.

Los tres comandos que siempre te orientan:

```bash
# Dónde quedaron los items:
docker compose --profile onboarding run --rm onboarding zotai s1 status

# Qué pasó en detalle:
docker compose --profile onboarding run --rm onboarding zotai s1 validate --open-report

# Re-correr una etapa específica (todas son idempotentes):
docker compose --profile onboarding run --rm onboarding zotai s1 ocr
docker compose --profile onboarding run --rm onboarding zotai s1 import
docker compose --profile onboarding run --rm onboarding zotai s1 enrich --substage all
docker compose --profile onboarding run --rm onboarding zotai s1 tag --apply
```

Problemas comunes (más en [`docs/troubleshooting.md`](docs/troubleshooting.md)):

| Síntoma | Causa probable | Fix |
|---|---|---|
| "Cannot reach Zotero: local API requires Zotero Desktop to be open" | Zotero cerrado o Settings → Advanced → Allow other applications desmarcado | Abrir Zotero + marcar la opción |
| Stage 01 aborta con "Budget exceeded" | Corpus más grande o más ambiguo que lo esperado | `MAX_COST_USD_STAGE_01=2.00` en `.env` y re-correr |
| Stage 04 pasa muchos items a 04e (quarantine) con ratio >25% | Corpus LATAM-heavy | Subir `MAX_COST_USD_STAGE_04` y chequear issue #46 para cobertura futura |
| `attachment_simple: Request Entity Too Large` | PDF >20 MB (límite Zotero API) | Importar ese PDF a mano + re-correr Stage 03 (issue #39 trackea el handling automático) |
| OCR se clava | Poco RAM | Bajar `OCR_PARALLEL_PROCESSES=2` en `.env` |
| Stage 05 rechaza muchos tags como "hallucinated" | Taxonomía no matchea dominio del corpus | Revisar `tema_rejected` / `metodo_rejected` en el CSV, ajustar taxonomía, re-correr con `--re-tag` |

### Paso 11 — Re-correr todo con configuración nueva

Si querés vaciar todo y empezar desde cero (por ejemplo para probar
con una taxonomía rearmada):

```bash
# 1. Borrar state + reports (mantiene .env y config/).
rm -rf workspace/state.db workspace/staging/* workspace/reports/*

# 2. Recrear schema vía alembic:
docker compose --profile onboarding run --rm onboarding \
    alembic -c /app/alembic.ini upgrade head

# 3. Si querés borrar también lo que ya importaste a Zotero,
#    tenés que hacerlo a mano en Zotero Desktop. El pipeline NO
#    borra items de Zotero — es estrictamente additive.

# 4. Re-correr:
./scripts/run-pipeline.sh --yes
```

Alternativamente, si sólo querés **re-tagear** sin tocar nada más
(taxonomía nueva, metadata intacta):

```bash
docker compose --profile onboarding run --rm onboarding \
    zotai s1 tag --apply --re-tag
```

---

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
