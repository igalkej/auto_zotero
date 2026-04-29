# ADR 021 — Cascade de captura de refs: OpenAlex → HTML scraping → PDF (opcional)

**Status**: Accepted
**Date**: 2026-04-28
**Deciders**: project owner
**Related**: ADR 020 (S2 owna citation graph), ADR 018/019 (SciELO + DOAJ substages en Stage 04 S1).

---

## 1. Contexto

ADR 020 establece que S2 captura el grafo de citas. La pregunta de implementación es: **¿de dónde se obtienen las refs de cada paper?**

Tres fuentes con perfiles distintos:

- **OpenAlex** (`referenced_works` en el endpoint `/works/{doi}`). Cobertura ~85-95% para journals modernos anglo. Cae para humanidades, papers <2000, y journals LATAM/SciELO que no depositan refs en Crossref. Gratis, polite pool 10 req/s con header `User-Agent` que incluye email (ya integrado en `api/openalex.py`).
- **HTML scraping** del landing page del artículo. La mayoría de revistas LATAM corren OJS (Public Knowledge Project) y exponen refs directo en HTML — por ejemplo, https://sociedadeconomiacritica.org/ojs/index.php/cec/article/view/377 muestra refs como `<ol>` legible con un parser sencillo. Mismo caso para SciELO HTML view. Esfuerzo modesto (un parser por plataforma + fallback genérico). Gratis.
- **PDF parsing** (anystyle, GROBID, refextract). Fiable pero costoso: GROBID requiere service Java aparte; anystyle es Ruby-only; refextract es Python pero lento (~5-10s por PDF). Sólo justificable si las dos fuentes anteriores fallan sistemáticamente.

La decisión de fondo es priorizar el costo cero (OpenAlex + HTML) y dejar PDF parsing como backup contingente en una decisión sucesora si los datos lo piden.

## 2. Decisión

**Cascade de fuentes en orden, primer hit gana**:

1. **OpenAlex** `referenced_works` vía `api/openalex.py` (ya integrado).
2. **HTML scraping** del landing page del artículo:
   - **OJS** (PKP-based, dominante en LATAM): parser dedicado para la estructura `<ol class="references">` o `<div class="references">` típica de OJS.
   - **SciELO** (HTML view distinto de OJS): parser dedicado.
   - **Genérico** (fallback): heurística sobre JSON-LD `@type=ScholarlyArticle.citation` + listas `<ol>` / `<ul>` con tag `references` / `bibliography`.
3. **PDF parsing**: opt-in detrás de `S2_REFS_PDF_PARSING_ENABLED=true`, default `false`. **No se implementa en v1**; el flag existe para que un sprint sucesor agregue la integración (con su propio ADR para elegir motor) sin romper este contrato.

Cada nivel reporta `source_api` para auditabilidad: `'openalex'` | `'ojs_html'` | `'scielo_html'` | `'generic_html'` | `'pdf'`. El campo se persiste en `Reference.source_api` (ADR 020 §2.1).

### 2.1 Mecánica del cascade

```python
def fetch_refs(doi: str, landing_url: str | None) -> list[ParsedRef]:
    """Cascade de captura. Primer nivel con resultado no vacío gana."""

    if refs := openalex.fetch_refs(doi):
        return refs   # source_api='openalex'

    if not landing_url:
        landing_url = openalex.get_landing_url(doi)   # best-effort

    if landing_url and settings.s2_refs_scraping_enabled:
        if refs := scrape_html(landing_url):
            return refs   # source_api inferido por parser que matcheó

    if settings.s2_refs_pdf_parsing_enabled:
        if pdf := zotero.get_attachment_path(doi):
            return parse_pdf_refs(pdf)   # source_api='pdf'

    return []   # no hay refs disponibles; ADR 020 §2.4 maneja el caso
```

**Idempotencia**: cada nivel devuelve `[]` si no aplica, nunca raise. Errores de red en un nivel se loguean con `structlog` y no bloquean el siguiente. El logger registra el path tomado y el tiempo por nivel para análisis posterior y para que el `/metrics` (ADR 020 §7.2) pueda exponer la distribución.

**Timeouts**: cada llamada HTTP respeta `S2_REFS_FETCH_TIMEOUT_SECONDS` (default 15). Un nivel que timeoutea cuenta como falla y pasa al siguiente.

### 2.2 HTML scraping: alcance v1

Tres parsers, aislados en `src/zotai/api/refs_scrapers/`:

- **`ojs.py`**. Estructura PKP estándar. Detecta vía URL pattern (`/ojs/`, `/article/view/`) o vía meta tag `generator=Open Journal Systems`. Cobertura estimada: 80%+ de revistas LATAM en OJS.
- **`scielo.py`**. Detecta vía URL host (`.scielo.org`, `.scielo.br`, mirrors). HTML view de SciELO tiene su propio layout distinto al PDF view.
- **`generic.py`**. Best-effort sobre JSON-LD + heurística DOM. Si nada matchea, devuelve `[]`.

Cada parser está aislado para que agregar uno nuevo (ej. `redalyc.py` si I1 lo justifica) no afecte los existentes. Tests con **fixtures HTML capturadas (snapshot)** — no live network en tests, no flakiness.

### 2.3 Refs sin DOI

Cuando un parser identifica una cita pero no logra resolverla a DOI (libros viejos, working papers, tesis, fuentes primarias):

- Se persiste con `cited_doi=null` y `cited_text` con el texto crudo de la cita (typically la string completa del item en la `<ol>`).
- Estos no contribuyen a `score_refs` (no podés intersectar contra `cited_text`), pero quedan auditables.
- Pasada futura puede fuzz-matchear `cited_text` contra títulos en Zotero o `ExternalPaper` para resolver retroactivamente.

OpenAlex resuelve refs a OpenAlex IDs (mapeables a DOI cuando existe). HTML scrapers extraen DOI cuando lo encuentran explícito en el item de la lista; cuando no, dejan `cited_text`.

### 2.4 Variables de configuración

Nuevas en `.env.example`:

```bash
# ── S2 refs collection (ADR 021) ────────────────────────────
S2_REFS_SCRAPING_ENABLED=true        # apaga HTML scraping si querés sólo OpenAlex
S2_REFS_PDF_PARSING_ENABLED=false    # opt-in (no implementado en v1, slot reservado)
S2_REFS_FETCH_TIMEOUT_SECONDS=15     # timeout por fuente
```

## 3. Consecuencias

### 3.1 Positivas

- **Cobertura LATAM ampliada**. OpenAlex + HTML cubre >90% en corpora mixtos (expectativa pre-I1). Resuelve el gap principal del scoring por refs en corpora CONICET / humanidades / ciencias sociales LATAM.
- **Costo cero adicional**. Las tres fuentes no-PDF son gratis. PDF queda detrás de flag y de un ADR sucesor.
- **Modular**. Agregar plataforma N+1 es un archivo nuevo en `refs_scrapers/`, sin tocar lógica existente. No requiere ADR salvo que cambie el orden de la cascade.
- **Auditable**. `source_api` en `Reference` permite al usuario / al desarrollador ver de dónde vino cada arista. Fundamental para el follow-up "qué fuentes son las que más rinden en mi corpus".
- **Fail-soft uniforme**. Cada nivel falla sin romper el siguiente, ni el ciclo del worker.

### 3.2 Negativas

- **Fragility de scrapers**. Cambios de DOM en OJS/SciELO rompen el parser. Mitigación: tests con snapshots de HTML real; falla del parser devuelve `[]` y la cascade sigue. La fragility se monitorea vía la métrica `refs_source_api_breakdown` — caída brusca de `ojs_html` cuenta es señal.
- **Cobertura no garantizada en v1**. El experimento I1 (en ADR 020 §5) valida empíricamente. Si el experimento contradice la asunción de cobertura ≥80%, se reabre la decisión sobre PDF parsing.
- **Sin PDF parsing en v1**. Es deliberado, pero significa que cierto residual queda sin captura (papers viejos digitalizados sin OpenAlex ni HTML accesible). El residual se mide en I1; si es <20%, se acepta.
- **Per-platform parsers son trabajo continuo**. Cada agregado nuevo (REDIB, RedALyC, La Referencia si aplica — ver issue #46) es un archivo más. Aceptable: cada parser <100 líneas con tests.

### 3.3 Neutras

- No cambia ADR 020. Sólo formaliza cómo se captan las refs que ADR 020 promete.
- No conflictúa con ADR 018/019: el cliente SciELO de ADR 019 (Crossref Member 530) sigue siendo el path para metadata bibliográfica del candidato; el parser SciELO HTML de este ADR es path para refs salientes. Son módulos distintos con responsabilidades distintas.

## 4. Alternativas consideradas y descartadas

**A. PDF parsing como fuente primaria.**
Costoso (GROBID dep, ~5-10s/PDF, service Java aparte). Innecesario si OpenAlex+HTML cubren ≥80%. Descartado para v1.

**B. Sólo OpenAlex.**
Asumir que OpenAlex alcanza. Descartado: corpora LATAM dejarían refs no capturadas sistemáticamente y el escenario 2 de la discusión inicial (papers no-OA con bibliografía como señal estructural) quedaría sin sustento.

**C. Crossref directo como segundo nivel** (antes que HTML).
A veces tiene refs cuando OpenAlex aún no las indexó (delay típico de 2-4 semanas). Descartado para v1: el delay es chico relativo al ciclo del worker (días/semanas), y agregar un cliente más es overhead. Reconsiderable si I1 muestra que es un gap real.

**D. Per-publisher API clients en lugar de HTML scraping.**
Algunos publishers exponen API de refs (Elsevier, Springer). Descartado por: (1) requieren credenciales/keys que el usuario no tiene; (2) cobertura LATAM nula; (3) no agregan sobre lo que OpenAlex ya tiene para esos casos.

**E. Una sola fuente con merge** (consultar todas, deduplicar, persistir todas las refs).
Más completo pero re-llama a OpenAlex/HTML cuando ya tenemos resultado. Descartado para v1 por costo. La cascade "primer hit gana" es estrictamente más barata; si una fuente da incompleto (parser HTML pierde 2 refs de 30), el incremento marginal no justifica el costo. Reconsiderable si I1 muestra gaps específicos.

## 5. Validación empírica

Misma I1 que ADR 020 §5. La cascade se valida en bloque: si la combinación OpenAlex+HTML da cobertura combinada **≥80%** y % intra-corpus **≥5%**, este ADR queda firme.

Si cobertura **<60%**, reabrir decisión sobre PDF parsing (potencialmente con ADR sucesor que elija motor: anystyle vs GROBID vs refextract, con pros/contras de instalación y precisión).

Si **60% ≤ cobertura < 80%**, este ADR sigue en pie; el deficit residual se documenta como limitación conocida hasta que el corpus crezca o aparezca demanda de agregar PDF parsing.

## 6. Cambios requeridos en documentos existentes

- `docs/plan_02_subsystem2.md` §5 (modelo de datos): mencionar `Reference.source_api` y los valores legales (referenciar este ADR §2).
- `docs/plan_02_subsystem2.md` §11 (Sprint 5): la captura de refs se implementa según la cascade de este ADR.
- `.env.example`: agregar las tres variables del §2.4.
- `docs/plan_glossary.md`: entradas para "refs cascade", "OJS scraper", "SciELO HTML scraper".

Estos cambios se aplican en el PR derivado que sigue al merge de los tres ADRs.

## 7. Presupuesto y métricas

### 7.1 Costo

Cero adicional en USD. Costo en wall-clock:

- OpenAlex: ~100ms/call con polite pool.
- HTML scraping: ~500ms-2s/call (varía por sitio).
- Fallback completo (OpenAlex miss → HTML miss): ~3s antes de devolver `[]`.

Para 1000 papers en backfill, peor caso (toda la biblioteca cae en HTML por SciELO-heavy): ~30 min wall-clock. Caso típico (OpenAlex-dominant): ~5-10 min.

### 7.2 Métricas

`/metrics` expone (vía ADR 020 §7.2):

- `refs_source_api_breakdown`: distribución por valor de `source_api` (qué fracción del grafo viene de cada fuente).
- `refs_html_scrape_failures_last_24h`: contador de errores DOM (ej. parser OJS no encontró `<ol>`). Caída brusca detecta DOM changes.

## 8. Follow-ups

- Plataformas adicionales según corpus real: si I1 muestra que un publisher específico aparece mucho y no es ni OJS ni SciELO, agregar parser dedicado en `refs_scrapers/` con un PR puntual (sin ADR salvo que cambie el orden de la cascade).
- Si la cobertura es <60%, reabrir PDF parsing y elegir entre anystyle (Ruby), GROBID (Java service), refextract (Python). Cada motor con su ADR sucesor; comparativa de precisión, latencia y deps.
- Si OpenAlex empieza a indexar refs LATAM (no es imposible — es proyecto activo), la cascade naturalmente migra hacia "OpenAlex domina y HTML es tail" sin cambios de código. Sólo cambia el `refs_source_api_breakdown`.

## 9. Relación con ADRs previos

- **ADR 020** (S2 owna citation graph): este ADR es la implementación de "cómo se captan refs" referenciada en ADR 020 §2.4 y §5.
- **ADR 018/019** (SciELO + DOAJ en Stage 04 S1): SciELO queda en ambos contextos pero con responsabilidades distintas — Crossref Member 530 para metadata bibliográfica del candidato (ADR 019), parser HTML para refs salientes (este ADR). Sin conflicto.
- **ADR 015** (S2 owna ChromaDB): independiente. La captura de refs y el embedding son operaciones paralelas en el step 0/0.5 del worker.
