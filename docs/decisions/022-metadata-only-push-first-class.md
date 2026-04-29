# ADR 022 — Push de items metadata-only como flujo de primera clase

**Status**: Accepted
**Date**: 2026-04-28
**Deciders**: project owner
**Related**: ADR 020 (citation graph + bandeja /classics), ADR 015 (S2 owna ChromaDB), plan_02 §10 (push), issue #14 (PDF cascade Sprint 3).

---

## 1. Contexto

El diseño original de S2 (plan_02 §10 + issue #14) asume que cada candidate aceptado tiene PDF: el push intenta una cascade de fuentes (OA → DOI resolver → Anna's → LibGen → Sci-Hub → RSS) y, si falla, deja el item en Zotero con tag `needs-pdf` para reintentar después.

Pero hay dos casos donde no conseguir el PDF **no es accidente**, es la realidad estructural:

- **Papers paywalled estables**: el publisher no expone OA, el paper no aparece en piratería. La señal del paper (abstract + refs + venue + autores) puede ser suficiente para citarlo en un trabajo propio.
- **Classics ausentes** (escenario 3 de la discusión inicial; bandeja `/classics` introducida por ADR 020 §2.4): papers altamente citados por mi corpus que no tengo en Zotero. El usuario quiere incorporarlos por su valor citacional aunque no necesariamente leerlos como PDF.

En ambos casos:

- El item es legítimamente parte del corpus.
- Sus refs alimentan el grafo de citas (ADR 020) y el perfil de interés del usuario.
- S3 (MCP / `zotero-mcp serve`) puede recuperarlo por título / abstract aunque no haya fulltext indexable — ADR 015 §6 ya prevé `source ∈ {s2_fulltext, s2_abstract, s2_title_only}`.
- Para citar en un paper propio basta con metadata.

El tag `needs-pdf` actual asume que el PDF falta **transitoriamente** — implica retry. No comunica "este item es metadata-only por diseño". Aplicar `needs-pdf` a un classic ausente es semánticamente incorrecto: el sistema iba a quedar reintentando indefinidamente algo que estructuralmente no va a aparecer.

## 2. Decisión

**Items metadata-only son ciudadanos de primera clase del corpus**. El push de S2 soporta dos modos:

1. **Push estándar** (default para candidates con `source_kind='rss'` que pasan triage `accepted`):
   - Cascade completa de PDF (issue #14, sprint 3).
   - Si exitoso: item en Zotero sin tags adicionales.
   - Si falla: tag `needs-pdf` (transitorio, retry-able).

2. **Push metadata-only**:
   - Cascade de PDF se intenta **una sola vez** en silencio.
   - Si exitoso: item en Zotero sin tags adicionales (el sistema ganó un PDF gratis).
   - Si falla: tag `metadata-only` permanente. **No retry**.
   - Default para candidates con `source_kind='reference_mining'` (bandeja /classics).
   - Disponible como opción explícita del usuario para candidates RSS via botón "Aceptar como metadata-only" en el dashboard.

### 2.1 Tags reservados por el sistema

Los tres tags relacionados a PDF / origen son **ortogonales**:

| Tag | Significado | Permanencia | Aplicado por |
|---|---|---|---|
| `needs-pdf` | Cascade falló transitoriamente, espera retry | Transitorio (lo remueve sprint 3 si retry exitoso) | Push estándar al fallar la cascade |
| `metadata-only` | Aceptado deliberadamente sin expectativa de PDF | Permanente (el usuario puede removerlo a mano) | Push metadata-only al fallar la cascade silenciosa |
| `discovered-via-refs` | Origen: bandeja /classics (reference mining) | Permanente | Push metadata-only de candidates con `source_kind='reference_mining'` |

Combinaciones legales:
- Item RSS aceptado con PDF: ningún tag.
- Item RSS aceptado sin PDF (cascade falló accidentalmente): `needs-pdf`.
- Item RSS aceptado deliberadamente como metadata-only por el usuario, cascade silenciosa falló: `metadata-only`.
- Item RSS aceptado deliberadamente como metadata-only, cascade silenciosa exitosa: ningún tag (el sistema ganó un PDF gratis).
- Item de /classics aceptado con PDF gratis (cascade silenciosa exitosa): `discovered-via-refs`.
- Item de /classics aceptado sin PDF (cascade silenciosa falló): `discovered-via-refs` + `metadata-only`.

`needs-pdf` y `metadata-only` son **mutuamente excluyentes** — el primero declara intención de retry, el segundo declara aceptación del estado.

### 2.2 Collection

Misma `Inbox S2` que candidates RSS estándar. El tag `metadata-only` distingue; no hace falta una collection separada.

Esta decisión es revisable: si en uso real la mezcla resulta confusa (muchos metadata-only en `Inbox S2` enmascarando los items con PDF), se puede:
- (a) Mover `metadata-only` a una collection aparte (`Inbox S2 — Metadata only`).
- (b) Filtrar por tag en la vista de Zotero del usuario (decisión personal, no del sistema).

V1 va con la opción simple. Cambio futuro requiere ADR sucesor o nota en plan_02.

### 2.3 Comportamiento de la cascade en modo metadata-only

**Una sola vuelta**. Si falla, no hay retry automático.

Esto es deliberado:

- El modo metadata-only es **declarativo** — el usuario está diciendo "aceptá este item incluso sin PDF". Reintentar contradice esa declaración.
- Reintentos silenciosos contra Sci-Hub/LibGen/Anna's son hostiles a esos servicios (terms-of-service grises, rate limits compartidos con otros usuarios). Una vuelta es polite; un loop background no.
- El usuario puede forzar retry manualmente removiendo el tag `metadata-only` y disparando el cascade del sprint 3 con `--re-pdf` o equivalente.

Excepción: si el usuario re-corre `zotai s2 push --retry-metadata-only` explícitamente, la cascade se vuelve a intentar para todos los items con tag `metadata-only`. Comando opt-in, no scheduled.

### 2.4 Selección del modo

- **Default por `source_kind`**:
  - `'rss'` → push estándar.
  - `'reference_mining'` → push metadata-only.
- **Override en triage UI** (sprint 5, issue futuro):
  - Para candidates RSS: botón secundario "Aceptar como metadata-only" además del "Aceptar" estándar. Útil cuando el usuario sabe que el paper es paywalled estable.
  - Para candidates de /classics: el botón "Aceptar" hace metadata-only por default. No hay opción de "aceptar con cascade exhaustiva" en v1 (el modo metadata-only ya intenta cascade silenciosa una vez, que es lo razonable).

## 3. Consecuencias

### 3.1 Positivas

- **Items legítimos no-OA en el corpus**. El usuario no tiene que decidir entre "no incorporo el paper" y "lo incorporo con tag transitorio que se va a quedar mal taggeado para siempre".
- **Bandeja /classics tiene un push coherente**. ADR 020 introduce la bandeja; este ADR provee el flujo de aceptación que esa bandeja necesita. Sin este ADR, /classics no tendría un patrón de push semánticamente correcto.
- **Auditable**. Los tres tags permiten filtros claros en Zotero:
  - "Items que esperan PDF" → `needs-pdf`.
  - "Items metadata-only deliberados" → `metadata-only`.
  - "Items que entraron por discovery, no por journals" → `discovered-via-refs`.
- **Refs como compensación**. Item metadata-only sin refs sería mortifero (un agujero en el corpus); con refs (ADR 020) aporta señal estructural que beneficia al scoring de futuros candidates.
- **Sin loops contra Sci-Hub/LibGen**. La política de "una vuelta y declaración" es respetuosa con esos servicios y predecible para el usuario.

### 3.2 Negativas

- **Más tags reservados en la biblioteca**. Tres tags nuevos: `needs-pdf` (que ya existía en plan), `metadata-only`, `discovered-via-refs`. Mitigación: documentar bien en plan_02 + README + glossary; los tres tienen prefijos / nombres descriptivos no ambiguos.
- **Tags ortogonales son fáciles de confundir**. Mitigación: tabla en este ADR §2.1 + entrada en `plan_glossary.md` que cubra las combinaciones legales.
- **Push tiene dos modos con UX distinto**. El dashboard sprint 5 va a ofrecer dos botones donde antes había uno. Aceptable; el segundo botón aplica sólo a candidates RSS (los de /classics tienen un solo "Aceptar" que es metadata-only por default).

### 3.3 Neutras

- **No cambia ADR 015**. S3 indexa todo lo que está en Zotero, no se hace caso especial para metadata-only. El schema ChromaDB ya tiene `source ∈ {s2_fulltext, s2_abstract, s2_title_only}` — items metadata-only caen en `s2_abstract` o `s2_title_only` según haya abstract disponible o no. El reconcile de ADR 015 los procesa naturalmente.
- **No afecta plan_01 / S1**. S1 sigue importando con cascade propia (Ruta A / C); este ADR vive enteramente en S2 push.

## 4. Alternativas consideradas y descartadas

**A. Sólo `needs-pdf`, retry agresivo permanente.**
Suficiente operacionalmente para casos transitorios. Falla en el caso classics ausentes (un paper de 1985 de un journal cerrado: nunca va a aparecer un PDF; el tag `needs-pdf` persiste con semántica incorrecta y el sistema reintenta indefinidamente). Descartada.

**B. Collection separada `Metadata-only` (sin tag).**
Más visible. Descartada: rompe el modelo "el usuario ve su biblioteca como un corpus único, las collections son organizativas, no semánticas". Tag basta y es ortogonal a la organización por proyecto / tema que el usuario quiera hacer.

**C. Sin marker explícito** (los items sin PDF se ven igual a los con PDF).
Confunde con items de S1 que pueden tener metadata sin PDF transitoriamente (ej. Etapa 03 abortó para ese item). No discrimina origen RSS vs /classics. Descartada por auditabilidad.

**D. Tag único `closed-access` o similar** que reemplaza tanto `metadata-only` como `discovered-via-refs`.
Más simple. Descartada: el origen del item (`/classics` vs RSS) es información valiosa para que el usuario filtre lo descubierto vs lo monitoreado por journals seguidos.

**E. Reintentos silenciosos en background para items `metadata-only`.**
Más completo. Descartada por hostilidad hacia Sci-Hub / LibGen / Anna's (rate limits compartidos), por contradecir la declaración del usuario, y porque el comando manual `zotai s2 push --retry-metadata-only` cubre el caso "ahora sí buscame los PDFs que faltan" sin loops automáticos.

## 5. Cambios requeridos en documentos existentes

- `docs/plan_02_subsystem2.md` §10 (push):
  - Documentar los dos modos (estándar / metadata-only).
  - Documentar los tres tags y su tabla de combinaciones.
  - Ajustar acceptance criteria de issue #14 (sprint 3) para que sea forward-compatible con el modo metadata-only que aterriza en sprint 5.
- `README.md` §"Cómo queda tu biblioteca Zotero":
  - Agregar los tres tags (`needs-pdf`, `metadata-only`, `discovered-via-refs`) a la lista de "tags reservados por el sistema".
  - Una línea sobre cómo el usuario los puede usar como filtros en Zotero.
- `docs/plan_glossary.md`: entradas para los tres tags + "push metadata-only" + "push estándar".

Estos cambios se aplican en el PR derivado que sigue al merge de los tres ADRs.

## 6. Follow-ups

- Si en uso real `metadata-only` se aplica a >50% del corpus que pasa por S2, revisar el modelo: puede ser señal de que la cascade sprint 3 es muy laxa (rinde poco), o de que el corpus estructuralmente tiene pocos OA y conviene revisar fuentes adicionales (institutional repos, preprint servers).
- Si zotero-mcp no maneja bien items con `source=s2_abstract` o `s2_title_only` (recall pobre en queries semánticas desde Claude Desktop), considerar embedding de "título inflado" — incluir autores + venue + year + abstract truncado. Follow-up de ADR 015 §9 ya prevé esto y se aplica acá.
- Si la mezcla en `Inbox S2` resulta confusa (muchos metadata-only enmascarando items con PDF), agregar collection separada `Inbox S2 — Metadata only` con ADR sucesor o decisión documentada en plan_02 §10.

## 7. Relación con ADRs previos

- **ADR 014** (skip attach if existing PDF en S1 dedup): independiente. Aplica al push estándar de S1; no afecta el push de S2 ni el modo metadata-only.
- **ADR 015** (S2 owna ChromaDB): items metadata-only se indexan vía la cascade existente (`s2_abstract` / `s2_title_only`), sin cambios al schema ChromaDB ni al reconcile.
- **ADR 020** (S2 owna citation graph + bandeja /classics): la bandeja /classics introducida por ADR 020 §2.4 usa exclusivamente push metadata-only para candidates con `source_kind='reference_mining'`. Este ADR provee el flujo que esa bandeja consume.
- **ADR 021** (cascade de captura de refs): independiente. La captura de refs alimenta el grafo (ADR 020) y el grafo alimenta /classics (ADR 020); este ADR sólo formaliza el push de los items aceptados desde esa bandeja.
