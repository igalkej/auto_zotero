# ADR 015 — Fase 2 Validation Checklist

**Anexo de**: `docs/decisions/015-s2-owns-embeddings-index.md` (§5).
**Propósito**: validar empíricamente que `zotero-mcp serve` puede
leer correctamente una ChromaDB escrita exclusivamente por código
propio (el script `scripts/validate_chromadb_schema.py` que simula lo
que S2 producirá en Fase 3), **antes** de implementar el indexador de
Fase 3.

---

## Pre-requisitos

- Python 3.11+ en el host.
- Dependencia opcional `s2` instalada: `uv pip install -e '.[s2]'`
  (trae `chromadb` que el script necesita).
- `uv tool install "zotero-mcp-server[semantic]"` instalado y
  configurado — setup interactivo completado al menos una vez
  (`zotero-mcp setup`).
- `OPENAI_API_KEY` exportada en la shell.
- Claude Desktop instalado, con la config `claude_desktop_config.json`
  apuntando al `zotero-mcp serve` que se va a probar (plan_03 §4.2).
- Biblioteca Zotero con ≥5 items reales (no hace falta mucho; el
  script inserta 5 items sintéticos con keys inventadas, pero Claude
  Desktop espera que la biblioteca esté operativa para invocar las
  MCP tools).

---

## Procedimiento

### Paso 1 — Backup de la ChromaDB existente

Si ya corriste `zotero-mcp update-db` alguna vez, el path por default
`~/.config/zotero-mcp/chroma_db/` ya tiene contenido. **Backup primero**:

```bash
mv ~/.config/zotero-mcp/chroma_db ~/.config/zotero-mcp/chroma_db.bak
```

Si es la primera vez (no existe el directorio), saltá este paso.

### Paso 2 — Poblar la ChromaDB con el script

```bash
export OPENAI_API_KEY=sk-...
python scripts/validate_chromadb_schema.py \
    --path ~/.config/zotero-mcp/chroma_db \
    --collection-name zotero_library \
    --num-items 5
```

El script imprime un reporte JSON al final con:
- `path`, `collection_name`, `documents_count`, `embedding_model`,
  `embedding_dimension`, `document_ids`, `sample_metadata`.

**Verificar**: `embedding_dimension == 3072` (text-embedding-3-large),
`documents_count == 5`, `sample_metadata` contiene las claves que ADR
015 §6 prescribe (`title`, `year`, `item_type`, `doi`, `source`,
`indexed_at`, `source_subsystem`).

### Paso 3 — Arrancar `zotero-mcp serve`

En una terminal aparte, con la misma config que usa Claude Desktop:

```bash
zotero-mcp serve
```

Debería arrancar sin errores sobre la ChromaDB recién populada. Si
imprime algo como `collection not found` o `invalid schema`, documentar
el error al pie de este archivo y **detener la validación** — el ADR
015 §5 contempla este caso (reabrir el ADR con addendum).

### Paso 4 — Consultas desde Claude Desktop

Reiniciar Claude Desktop para que levante el MCP server actualizado.
Verificar que el ícono de tools aparece y que las `zotero_*` tools
están disponibles.

Luego, probar en un chat:

#### 4.1. `zotero_semantic_search`

Query:

> *"Usá `zotero_semantic_search` para buscar papers sobre 'moneda y
> política monetaria en América Latina'. Listame los resultados."*

**Esperado**: Claude debería devolver al menos 1-2 items. El más
relevante es "Monetary policy and inflation expectations: evidence
from Argentina". Acepta también "Informalidad laboral..." (tangencial
pero comparte el eje LATAM) y "Fiscal multipliers in emerging
economies" (tangencial — política económica).

**Criterio de éxito**: los IDs de 8-char que aparecen en los
resultados coinciden con los que imprimió el script en el Paso 2
(`document_ids`).

#### 4.2. `zotero_item_details`

Tomar uno de los IDs que devolvió el search anterior y correr:

> *"Usá `zotero_item_details` para el item `{ID}`."*

**Esperado**: metadata completa: `title`, `year`, `item_type`, `doi`,
y — si `zotero-mcp` tolera el campo extra — también `source`,
`indexed_at`, `source_subsystem`.

**Criterio de éxito**: ni crash, ni campos faltantes de los que S2
escribe. Documentar al pie si `zotero-mcp` ignora silenciosamente
alguno de los campos extra (normalmente está OK — chromadb acepta
metadata arbitraria; la pregunta es solo si el cliente MCP los
surface o no).

### Paso 5 — Restaurar backup

Si la validación pasa y querés volver al estado anterior:

```bash
rm -rf ~/.config/zotero-mcp/chroma_db
mv ~/.config/zotero-mcp/chroma_db.bak ~/.config/zotero-mcp/chroma_db
```

Si la ChromaDB sintética te va a quedar como punto de partida
(válido — el backup no tenía nada), simplemente descartar el `.bak`:

```bash
rm -rf ~/.config/zotero-mcp/chroma_db.bak
```

---

## Resultado de la validación

Completar al terminar:

| Campo | Valor |
|---|---|
| Fecha de validación | _YYYY-MM-DD_ |
| Versión de `zotero-mcp` | `zotero-mcp --version` |
| OS (host) | _Linux / macOS / Windows + versión_ |
| Paso 2 (script) | ☐ pasa ☐ falla |
| Paso 3 (`serve`) | ☐ pasa ☐ falla |
| Paso 4.1 (`semantic_search`) | ☐ pasa ☐ falla |
| Paso 4.2 (`item_details`) | ☐ pasa ☐ falla |
| Conclusión general | ☐ OK para Fase 3 ☐ reabrir ADR 015 con addendum |

**Observaciones / issues encontrados** (si los hay):

_Escribir acá._

---

## Qué hacer si falla

Cada fallo implica un addendum a ADR 015 antes de pasar a Fase 3:

- **Paso 2 falla** (script crashea): bug del script. Arreglar y
  re-correr; no requiere addendum del ADR.
- **Paso 3 falla** (`zotero-mcp serve` no arranca o se queja del
  schema): el collection name, el schema, o los campos de metadata
  no coinciden con lo que `zotero-mcp` espera. **Investigar el source
  de `zotero-mcp`** (https://github.com/54yyyu/zotero-mcp), ajustar
  el script + ADR 015 §6, repetir.
- **Paso 4.1 falla** (`semantic_search` devuelve vacío o IDs que no
  coinciden): chromadb está escrita pero `zotero-mcp` no usa la
  collection o usa embeddings distintos. Verificar que el modelo de
  embeddings configurado en `zotero-mcp setup` coincide con el
  `--embedding-model` del script (ambos deben ser
  `text-embedding-3-large` por ADR 004).
- **Paso 4.2 falla** (crash en `zotero-mcp` al devolver detalles): el
  server no tolera algún campo de metadata que el script escribe. Si
  el problema es `source_subsystem` o `source`, evaluar sacarlos del
  schema (ajustar ADR 015 §6) o verificar si `zotero-mcp` los ignora
  silenciosamente (en cuyo caso es OK).

**Regla general**: cualquier divergencia entre lo que ADR 015 §6
prescribe y lo que `zotero-mcp` realmente espera justifica un
addendum al ADR documentando el ajuste antes de implementar Fase 3.
No arreglar al vuelo desde la Fase 3.
